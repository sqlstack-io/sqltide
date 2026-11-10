//! Sequential sensor dispatch: derive events, evaluate handlers, persist ticks.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use super::support::{
    ADAPTER_MODELS, EVENT_MODELS, SENSOR_MODELS, SENSOR_TYPES, hex_uuid, sensor_error,
    shift_timestamp_text, text, timestamp_is_before,
};
use crate::dev_server::support::attr;
use crate::py_kwargs;

const NODE_RESULTS_EVENT_SOURCE: &str = "node_results";
const INVOCATIONS_EVENT_SOURCE: &str = "invocations";
const EPOCH_COMPLETED_AT: &str = "1970-01-01 00:00:00.000";
const LEASE_TIMEOUT_BUFFER_SECONDS: f64 = 10.0;
const DEFAULT_EVENT_BATCH_LIMIT: i64 = 50;
const DEFAULT_DISPATCH_LEASE_TTL_SECONDS: f64 = 60.0;
const EVALUATION: &str = "sqltide.sensors._helpers.evaluation";
const EVENT_CONTEXT: &str = "sqltide.sensors.classes.event_sensor_context";
const POLLING_CONTEXT: &str = "sqltide.sensors.classes.polling_sensor_context";

type Any<'py> = Bound<'py, PyAny>;

fn member<'py>(py: Python<'py>, class: &str, name: &str) -> PyResult<Any<'py>> {
    attr(py, SENSOR_TYPES, class)?.getattr(name)
}

/// The persisted observation stream one catalog event type is derived from.
pub(crate) fn event_source_for(event_type: &Any<'_>) -> PyResult<&'static str> {
    let py: Python<'_> = event_type.py();
    if event_type.is(&attr(py, EVENT_MODELS, "AuditCompleted")?) {
        return Ok(NODE_RESULTS_EVENT_SOURCE);
    }
    if event_type.is(&attr(py, EVENT_MODELS, "RunCompleted")?) {
        return Ok(INVOCATIONS_EVENT_SOURCE);
    }
    let name: Any<'_> = match event_type.getattr("__name__") {
        Ok(name) => name,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
            event_type.clone()
        }
        Err(error) => return Err(error),
    };
    Err(sensor_error(
        py,
        format!(
            "Unsupported sensor event type '{}'; event sensors react to sqltide.events catalog \
             events",
            text(&name)?
        ),
    ))
}

/// The runtime status: an operator override, else the status declared in code.
#[pyfunction]
#[pyo3(signature = (*, sensor, overrides))]
pub(crate) fn effective_sensor_status<'py>(
    sensor: &Any<'py>,
    overrides: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = sensor.py();
    let override_status: Any<'py> = overrides.call_method1("get", (sensor.getattr("name")?,))?;
    if !override_status.is_none()
        && !override_status.is(&member(py, "SensorOverrideStatus", "DECLARED_IN_CODE")?)
    {
        return Ok(override_status);
    }
    if sensor
        .getattr("default_status")?
        .is(&member(py, "DefaultSensorStatus", "RUNNING")?)
    {
        return member(py, "SensorOverrideStatus", "RUNNING");
    }
    member(py, "SensorOverrideStatus", "STOPPED")
}

/// Whether one derived event passes the sensor's type, target and trigger filters.
fn matches_sensor(declaration: &Any<'_>, event: &Any<'_>) -> PyResult<bool> {
    if !event.get_type().is(&declaration.getattr("event_type")?) {
        return Ok(false);
    }
    let targets: Any<'_> = declaration.getattr("targets")?;
    if !targets.is_none() && !targets.contains(event.getattr("target")?)? {
        return Ok(false);
    }
    let triggers: Any<'_> = declaration.getattr("triggers")?;
    if !triggers.is_none() {
        let trigger: Option<Any<'_>> = event.getattr_opt("trigger")?;
        match trigger {
            Some(trigger) if !trigger.is_none() => {
                if !triggers.contains(trigger.str()?)? {
                    return Ok(false);
                }
            }
            _ => return Ok(false),
        }
    }
    Ok(true)
}

/// The sensor's event declaration, or a `SensorError` when it is not event-driven.
fn event_declaration_of<'py>(sensor: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = sensor.py();
    let declaration: Any<'py> = sensor.getattr("declaration")?;
    if !declaration.is_instance(&attr(py, SENSOR_MODELS, "EventSensorDeclaration")?)? {
        return Err(sensor_error(
            py,
            format!(
                "Sensor '{}' is not an event sensor",
                text(&sensor.getattr("name")?)?
            ),
        ));
    }
    Ok(declaration)
}

/// The resolution of one matched delivery within a dispatch pass.
struct Outcome<'py> {
    resolved: bool,
    evaluation: Option<Any<'py>>,
    dead_lettered: bool,
}

impl<'py> Outcome<'py> {
    fn resolved(resolved: bool) -> Self {
        Self {
            resolved,
            evaluation: None,
            dead_lettered: false,
        }
    }
}

/// Fold per-delivery outcomes into one `SensorDispatchSummary`.
fn summarize<'py>(
    py: Python<'py>,
    outcomes: &[Outcome<'py>],
    lease_acquired: bool,
) -> PyResult<Any<'py>> {
    let mut statuses: Vec<Any<'py>> = Vec::new();
    for outcome in outcomes {
        if let Some(evaluation) = &outcome.evaluation {
            statuses.push(evaluation.getattr("status")?);
        }
    }
    let count = |name: &str| -> PyResult<usize> {
        let wanted: Any<'py> = member(py, "SensorTickStatus", name)?;
        Ok(statuses.iter().filter(|status| status.is(&wanted)).count())
    };
    attr(py, SENSOR_MODELS, "SensorDispatchSummary")?.call(
        (),
        Some(&py_kwargs!(py;
            lease_acquired = lease_acquired,
            evaluated = statuses.len(),
            succeeded = count("SUCCEEDED")?,
            skipped = count("SKIPPED")?,
            failed = count("FAILED")?,
            dead_lettered = outcomes.iter().filter(|outcome| outcome.dead_lettered).count(),
        )?),
    )
}

/// One positioned observation row and the events derived from it.
struct Delivery<'py> {
    position: Any<'py>,
    events: Vec<Any<'py>>,
}

/// One sequential dispatch pass over all running sensors.
#[pyclass(module = "sqltide._native", subclass)]
pub(crate) struct SensorDispatcher {
    repository: Py<PyAny>,
    event_target: Py<PyAny>,
    providers: Py<PyAny>,
    dispatcher_id: Py<PyAny>,
    batch_limit: Py<PyAny>,
    lease_ttl_seconds: f64,
    maximum_event_age_seconds: Option<f64>,
}

impl SensorDispatcher {
    fn repository<'py>(&self, py: Python<'py>) -> &Bound<'py, PyAny> {
        self.repository.bind(py)
    }

    fn call<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        arguments: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Any<'py>> {
        self.repository(py)
            .call_method(method, (), arguments.as_ref())
    }

    fn warehouse_now<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        self.call(py, "warehouse_now", None)
    }

    fn position<'py>(
        py: Python<'py>,
        completed_at: Any<'py>,
        result_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        attr(py, SENSOR_MODELS, "SensorStreamPosition")?.call(
            (),
            Some(&py_kwargs!(py; completed_at = completed_at, result_id = result_id)?),
        )
    }

    fn newest_position<'py>(
        &self,
        py: Python<'py>,
        source: &str,
        target: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let method: &str = if source == NODE_RESULTS_EVENT_SOURCE {
            "newest_node_result_position"
        } else {
            "newest_invocation_position"
        };
        self.call(py, method, Some(py_kwargs!(py; target = target)?))
    }

    fn advance(
        &self,
        py: Python<'_>,
        sensor: &Any<'_>,
        source: &str,
        position: &Any<'_>,
    ) -> PyResult<()> {
        self.call(
            py,
            "advance_checkpoint",
            Some(py_kwargs!(py;
                sensor_name = sensor.getattr("name")?,
                source = source,
                position = position,
            )?),
        )?;
        Ok(())
    }

    fn initialize_checkpoint(
        &self,
        py: Python<'_>,
        sensor: &Any<'_>,
        source: &str,
        target: &Any<'_>,
    ) -> PyResult<()> {
        let newest: Any<'_> = self.newest_position(py, source, target)?;
        let position: Any<'_> = if newest.is_none() {
            Self::position(
                py,
                EPOCH_COMPLETED_AT.into_pyobject(py)?.into_any(),
                "".into_pyobject(py)?.into_any(),
            )?
        } else {
            newest
        };
        self.advance(py, sensor, source, &position)
    }

    fn previous_status<'py>(&self, py: Python<'py>, row: &Any<'py>) -> PyResult<Any<'py>> {
        let position: Any<'py> =
            Self::position(py, row.getattr("completed_at")?, row.getattr("result_id")?)?;
        self.call(
            py,
            "previous_node_status",
            Some(py_kwargs!(py;
                binding_key = row.getattr("binding_key")?,
                target = row.getattr("target_identity")?,
                position = position,
            )?),
        )
    }

    fn node_result_events<'py>(&self, py: Python<'py>, row: &Any<'py>) -> PyResult<Any<'py>> {
        let previous_status: Any<'py> = self.previous_status(py, row)?;
        attr(py, "sqltide._native", "events_from_node_result")?.call(
            (),
            Some(&py_kwargs!(py;
                row = row,
                previous_status = previous_status,
                target = self.event_target.bind(py),
            )?),
        )
    }

    fn invocation_events<'py>(&self, py: Python<'py>, row: &Any<'py>) -> PyResult<Any<'py>> {
        attr(py, "sqltide._native", "events_from_invocation")?.call(
            (),
            Some(&py_kwargs!(py; row = row, target = self.event_target.bind(py))?),
        )
    }

    fn deliveries_after<'py>(
        &self,
        py: Python<'py>,
        source: &str,
        position: &Any<'py>,
        target: &Any<'py>,
    ) -> PyResult<Vec<Delivery<'py>>> {
        let node_results: bool = source == NODE_RESULTS_EVENT_SOURCE;
        let rows: Any<'py> = self.call(
            py,
            if node_results {
                "fetch_node_results_after"
            } else {
                "fetch_invocations_after"
            },
            Some(py_kwargs!(py;
                position = position,
                target = target,
                limit = self.batch_limit.bind(py),
            )?),
        )?;
        let mut deliveries: Vec<Delivery<'py>> = Vec::new();
        for row in rows.try_iter()? {
            let row: Any<'py> = row?;
            let identifier: &str = if node_results {
                "result_id"
            } else {
                "invocation_id"
            };
            let position: Any<'py> =
                Self::position(py, row.getattr("completed_at")?, row.getattr(identifier)?)?;
            let events: Any<'py> = if node_results {
                self.node_result_events(py, &row)?
            } else {
                self.invocation_events(py, &row)?
            };
            deliveries.push(Delivery {
                position,
                events: events.try_iter()?.collect::<PyResult<_>>()?,
            });
        }
        Ok(deliveries)
    }

    fn event_age_limit(&self, declaration: &Any<'_>) -> PyResult<Option<f64>> {
        let maximum: Any<'_> = declaration.getattr("maximum_event_age_seconds")?;
        if maximum.is_none() {
            return Ok(self.maximum_event_age_seconds);
        }
        maximum.extract().map(Some)
    }

    fn renew_lease(&self, py: Python<'_>, sensor: &Any<'_>) -> PyResult<bool> {
        let timeout: f64 = sensor.getattr("timeout_seconds")?.extract()?;
        // `max(lease_ttl, timeout + buffer)` keeps the first argument unless the second is larger.
        let candidate: f64 = timeout + LEASE_TIMEOUT_BUFFER_SECONDS;
        let ttl: f64 = if candidate > self.lease_ttl_seconds {
            candidate
        } else {
            self.lease_ttl_seconds
        };
        self.call(
            py,
            "acquire_dispatch_lease",
            Some(py_kwargs!(py; owner_id = self.dispatcher_id.bind(py), ttl_seconds = ttl)?),
        )?
        .is_truthy()
    }

    #[allow(clippy::too_many_arguments)]
    fn record_tick_row(
        &self,
        py: Python<'_>,
        sensor: &Any<'_>,
        tick_id: &str,
        event: Option<&Any<'_>>,
        attempt: &Any<'_>,
        status: &Any<'_>,
        started_at: &Any<'_>,
        completed_at: Option<&Any<'_>>,
        evaluation: Option<&Any<'_>>,
    ) -> PyResult<()> {
        let evaluation_field = |name: &str| -> PyResult<Option<Any<'_>>> {
            evaluation
                .map(|evaluation| evaluation.getattr(name))
                .transpose()
        };
        let event_kind: Option<Any<'_>> = event
            .map(|event| event.get_type().getattr("__name__"))
            .transpose()?;
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorTickRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                tick_id = tick_id,
                sensor_name = sensor.getattr("name")?,
                definition_fingerprint = sensor.getattr("identity_fingerprint")?,
                kind = sensor.getattr("kind")?.str()?,
                event_id = event.map(|event| event.getattr("id")).transpose()?,
                event_kind = event_kind,
                attempt = attempt,
                status = status.str()?,
                started_at = started_at,
                completed_at = completed_at,
                error_message = evaluation_field("error_message")?,
                skip_reason = evaluation_field("skip_reason")?,
                cursor = evaluation_field("cursor")?,
            )?),
        )?;
        self.call(
            py,
            "record_ticks",
            Some(py_kwargs!(py; ticks = PyTuple::new(py, [record])?)?),
        )?;
        Ok(())
    }

    fn evaluate<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
        context: Any<'py>,
    ) -> PyResult<Any<'py>> {
        attr(py, EVALUATION, "evaluate_sensor_handler")?.call(
            (),
            Some(&py_kwargs!(py;
                sensor = sensor,
                context = context,
                providers = self.providers.bind(py),
            )?),
        )
    }

    fn evaluate_event<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
        event: &Any<'py>,
        attempt: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let tick_id: String = hex_uuid(py)?;
        let started_at: Any<'py> = self.warehouse_now(py)?;
        let started: Any<'py> = member(py, "SensorTickStatus", "STARTED")?;
        self.record_tick_row(
            py,
            sensor,
            &tick_id,
            Some(event),
            attempt,
            &started,
            &started_at,
            None,
            None,
        )?;
        let store: Any<'py> = attr(py, "sqltide._native", "RepositoryStepStore")?.call(
            (),
            Some(&py_kwargs!(py;
                repository = self.repository(py),
                sensor_name = sensor.getattr("name")?,
                event_id = event.getattr("id")?,
            )?),
        )?;
        let steps: Any<'py> = attr(py, "sqltide._native", "DurableStepRunner")?
            .call((), Some(&py_kwargs!(py; store = store)?))?;
        let context: Any<'py> = attr(py, EVENT_CONTEXT, "EventSensorContext")?.call(
            (),
            Some(&py_kwargs!(py;
                event = event,
                target = self.event_target.bind(py),
                steps = steps,
            )?),
        )?;
        let evaluation: Any<'py> = self.evaluate(py, sensor, context)?;
        let completed_at: Any<'py> = self.warehouse_now(py)?;
        self.record_tick_row(
            py,
            sensor,
            &tick_id,
            Some(event),
            attempt,
            &evaluation.getattr("status")?,
            &started_at,
            Some(&completed_at),
            Some(&evaluation),
        )?;
        Ok(evaluation)
    }

    fn record_dead_letter(
        &self,
        py: Python<'_>,
        sensor: &Any<'_>,
        event: &Any<'_>,
        state: &Any<'_>,
    ) -> PyResult<()> {
        let now: Any<'_> = self.warehouse_now(py)?;
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorTickRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                tick_id = hex_uuid(py)?,
                sensor_name = sensor.getattr("name")?,
                definition_fingerprint = sensor.getattr("identity_fingerprint")?,
                kind = sensor.getattr("kind")?.str()?,
                event_id = event.getattr("id")?,
                event_kind = event.get_type().getattr("__name__")?,
                attempt = state.getattr("failed_attempts")?,
                status = member(py, "SensorTickStatus", "DEAD_LETTERED")?.str()?,
                started_at = &now,
                completed_at = &now,
                error_message = state.getattr("last_error_message")?,
                skip_reason = py.None(),
                cursor = py.None(),
            )?),
        )?;
        self.call(
            py,
            "record_ticks",
            Some(py_kwargs!(py; ticks = PyTuple::new(py, [record])?)?),
        )?;
        Ok(())
    }

    fn attempt_state<'py>(
        &self,
        py: Python<'py>,
        sensor_name: &Any<'py>,
        event_id: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        self.call(
            py,
            "tick_attempt_state",
            Some(py_kwargs!(py; sensor_name = sensor_name, event_id = event_id)?),
        )
    }

    /// Deliver one matched event; an unresolved outcome blocks the stream here.
    fn deliver_event<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
        event: &Any<'py>,
    ) -> PyResult<Outcome<'py>> {
        let state: Any<'py> =
            self.attempt_state(py, &sensor.getattr("name")?, &event.getattr("id")?)?;
        if state.getattr("resolved")?.is_truthy()? {
            return Ok(Outcome::resolved(true));
        }
        let failed_attempts: Any<'py> = state.getattr("failed_attempts")?;
        let retry_policy: Any<'py> = sensor.getattr("retry_policy")?;
        if failed_attempts.ge(retry_policy.getattr("max_attempts")?)? {
            self.record_dead_letter(py, sensor, event, &state)?;
            return Ok(Outcome {
                resolved: true,
                evaluation: None,
                dead_lettered: true,
            });
        }
        let last_failed_at: Any<'py> = state.getattr("last_failed_at")?;
        if failed_attempts.gt(0)? && !last_failed_at.is_none() {
            let ready_at: String = shift_timestamp_text(
                &last_failed_at.extract::<String>()?,
                retry_policy.getattr("backoff_seconds")?.extract()?,
            )?;
            if timestamp_is_before(&self.warehouse_now(py)?.extract::<String>()?, &ready_at)? {
                return Ok(Outcome::resolved(false));
            }
        }
        let evaluation: Any<'py> =
            self.evaluate_event(py, sensor, event, &failed_attempts.add(1)?)?;
        let failed: bool =
            evaluation
                .getattr("status")?
                .is(&member(py, "SensorTickStatus", "FAILED")?);
        Ok(Outcome {
            resolved: !failed,
            evaluation: Some(evaluation),
            dead_lettered: false,
        })
    }

    fn event_is_expired(
        &self,
        declaration: &Any<'_>,
        event: &Any<'_>,
        warehouse_now: &str,
    ) -> PyResult<bool> {
        let Some(maximum_age) = self.event_age_limit(declaration)? else {
            return Ok(false);
        };
        let expires_at: String = shift_timestamp_text(
            &event.getattr("completed_at")?.extract::<String>()?,
            maximum_age,
        )?;
        timestamp_is_before(&expires_at, warehouse_now)
    }

    /// Deliver new events to one event sensor; the flag is whether the dispatch lease held.
    fn dispatch_event_sensor<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
        target: &Any<'py>,
    ) -> PyResult<(Vec<Outcome<'py>>, bool)> {
        let declaration: Any<'py> = event_declaration_of(sensor)?;
        let source: &str = event_source_for(&declaration.getattr("event_type")?)?;
        let checkpoint: Any<'py> = self.call(
            py,
            "read_checkpoint",
            Some(py_kwargs!(py; sensor_name = sensor.getattr("name")?, source = source)?),
        )?;
        if checkpoint.is_none() {
            self.initialize_checkpoint(py, sensor, source, target)?;
            return Ok((Vec::new(), true));
        }
        let stream_head: Any<'py> = self.newest_position(py, source, target)?;
        if stream_head.is_none() {
            return Ok((Vec::new(), true));
        }
        let head_key: Bound<'py, PyTuple> = position_key(&stream_head)?;
        let mut outcomes: Vec<Outcome<'py>> = Vec::new();
        let mut current_position: Any<'py> = checkpoint;
        let mut advanced_position: Option<Any<'py>> = None;
        loop {
            let warehouse_now: Option<String> = if self.event_age_limit(&declaration)?.is_some() {
                Some(self.warehouse_now(py)?.extract()?)
            } else {
                None
            };
            let mut deliveries: Vec<Delivery<'py>> = Vec::new();
            for delivery in self.deliveries_after(py, source, &current_position, target)? {
                if position_key(&delivery.position)?.le(&head_key)? {
                    deliveries.push(delivery);
                }
            }
            let Some(last) = deliveries.last() else {
                break;
            };
            let last_position: Any<'py> = last.position.clone();
            let delivered: usize = deliveries.len();
            for delivery in deliveries {
                let mut blocked: bool = false;
                for event in &delivery.events {
                    if !matches_sensor(&declaration, event)? {
                        continue;
                    }
                    if let Some(now) = &warehouse_now
                        && self.event_is_expired(&declaration, event, now)?
                    {
                        continue;
                    }
                    if !self.renew_lease(py, sensor)? {
                        if let Some(position) = &advanced_position {
                            self.advance(py, sensor, source, position)?;
                        }
                        return Ok((outcomes, false));
                    }
                    let outcome: Outcome<'py> = self.deliver_event(py, sensor, event)?;
                    let resolved: bool = outcome.resolved;
                    outcomes.push(outcome);
                    if !resolved {
                        blocked = true;
                        break;
                    }
                }
                if blocked {
                    if let Some(position) = &advanced_position {
                        self.advance(py, sensor, source, position)?;
                    }
                    return Ok((outcomes, true));
                }
                advanced_position = Some(delivery.position);
            }
            current_position = last_position;
            if self.batch_limit.bind(py).gt(delivered)? {
                break;
            }
        }
        if let Some(position) = &advanced_position {
            self.advance(py, sensor, source, position)?;
        }
        Ok((outcomes, true))
    }

    fn dispatch_polling_sensor<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
    ) -> PyResult<Option<Outcome<'py>>> {
        let declaration: Any<'py> = sensor.getattr("declaration")?;
        let interval_seconds: f64 =
            if declaration.is_instance(&attr(py, SENSOR_MODELS, "PollingSensorDeclaration")?)? {
                declaration.getattr("minimum_interval_seconds")?.extract()?
            } else {
                0.0
            };
        let state: Any<'py> = self.call(
            py,
            "polling_tick_state",
            Some(py_kwargs!(py; sensor_name = sensor.getattr("name")?)?),
        )?;
        let now: Any<'py> = self.warehouse_now(py)?;
        let last_started_at: Any<'py> = state.getattr("last_started_at")?;
        if !last_started_at.is_none()
            && timestamp_is_before(
                &now.extract::<String>()?,
                &shift_timestamp_text(&last_started_at.extract::<String>()?, interval_seconds)?,
            )?
        {
            return Ok(None);
        }
        let tick_id: String = hex_uuid(py)?;
        let attempt: Any<'py> = 1_i64.into_pyobject(py)?.into_any();
        let started: Any<'py> = member(py, "SensorTickStatus", "STARTED")?;
        self.record_tick_row(
            py, sensor, &tick_id, None, &attempt, &started, &now, None, None,
        )?;
        let context: Any<'py> = attr(py, POLLING_CONTEXT, "PollingSensorContext")?.call(
            (),
            Some(&py_kwargs!(py;
                cursor = state.getattr("cursor")?,
                last_success_at = state.getattr("last_success_at")?,
                target = self.event_target.bind(py),
            )?),
        )?;
        let evaluation: Any<'py> = self.evaluate(py, sensor, context)?;
        let completed_at: Any<'py> = self.warehouse_now(py)?;
        self.record_tick_row(
            py,
            sensor,
            &tick_id,
            None,
            &attempt,
            &evaluation.getattr("status")?,
            &now,
            Some(&completed_at),
            Some(&evaluation),
        )?;
        Ok(Some(Outcome {
            resolved: true,
            evaluation: Some(evaluation),
            dead_lettered: false,
        }))
    }

    fn require_event_sensor<'py>(
        &self,
        py: Python<'py>,
        registry: &Any<'py>,
        sensor_name: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let sensor: Any<'py> = registry
            .getattr("sensors")?
            .call_method1("get", (sensor_name,))?;
        if sensor.is_none() {
            return Err(sensor_error(
                py,
                format!("Unknown sensor '{}'", text(sensor_name)?),
            ));
        }
        if !sensor
            .getattr("kind")?
            .is(&member(py, "SensorKind", "EVENT")?)
        {
            return Err(sensor_error(
                py,
                format!("Sensor '{}' is not an event sensor", text(sensor_name)?),
            ));
        }
        Ok(sensor)
    }

    fn rebuild_event<'py>(
        &self,
        py: Python<'py>,
        sensor: &Any<'py>,
        event_id: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let declaration: Any<'py> = event_declaration_of(sensor)?;
        let source: &str = event_source_for(&declaration.getattr("event_type")?)?;
        let mut events: Any<'py> = PyTuple::empty(py).into_any();
        if source == NODE_RESULTS_EVENT_SOURCE {
            let row: Any<'py> = self.call(
                py,
                "fetch_node_result",
                Some(py_kwargs!(py; result_id = event_id)?),
            )?;
            if !row.is_none() {
                events = self.node_result_events(py, &row)?;
            }
        } else {
            let row: Any<'py> = self.call(
                py,
                "fetch_invocation",
                Some(py_kwargs!(py; invocation_id = event_id)?),
            )?;
            if !row.is_none() {
                events = self.invocation_events(py, &row)?;
            }
        }
        for event in events.try_iter()? {
            let event: Any<'py> = event?;
            if event.getattr("id")?.eq(event_id)? {
                return Ok(event);
            }
        }
        Err(sensor_error(
            py,
            format!(
                "Event '{}' for sensor '{}' cannot be rebuilt from persisted observations",
                text(event_id)?,
                text(&sensor.getattr("name")?)?
            ),
        ))
    }

    fn running<'py>(py: Python<'py>, sensor: &Any<'py>, overrides: &Any<'py>) -> PyResult<bool> {
        Ok(effective_sensor_status(sensor, overrides)?.is(&member(
            py,
            "SensorOverrideStatus",
            "RUNNING",
        )?))
    }
}

/// `(position.completed_at, position.result_id)`, compared as Python compares tuples.
fn position_key<'py>(position: &Any<'py>) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(
        position.py(),
        [
            position.getattr("completed_at")?,
            position.getattr("result_id")?,
        ],
    )
}

#[pymethods]
impl SensorDispatcher {
    #[new]
    #[pyo3(signature = (
        *,
        repository,
        event_target,
        providers=None,
        dispatcher_id=None,
        batch_limit=None,
        lease_ttl_seconds=DEFAULT_DISPATCH_LEASE_TTL_SECONDS,
        maximum_event_age_seconds=None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        repository: Py<PyAny>,
        event_target: Py<PyAny>,
        providers: Option<Py<PyAny>>,
        dispatcher_id: Option<Py<PyAny>>,
        batch_limit: Option<Py<PyAny>>,
        lease_ttl_seconds: f64,
        maximum_event_age_seconds: Option<f64>,
    ) -> PyResult<Self> {
        let dispatcher_id: Py<PyAny> = match dispatcher_id {
            Some(dispatcher_id) if !dispatcher_id.is_none(py) => dispatcher_id,
            _ => hex_uuid(py)?.into_pyobject(py)?.into_any().unbind(),
        };
        Ok(Self {
            repository,
            event_target,
            providers: providers.unwrap_or_else(|| PyTuple::empty(py).into_any().unbind()),
            dispatcher_id,
            batch_limit: batch_limit.unwrap_or_else(|| {
                DEFAULT_EVENT_BATCH_LIMIT
                    .into_pyobject(py)
                    .map(|limit| limit.into_any().unbind())
                    .unwrap_or_else(|_| py.None())
            }),
            lease_ttl_seconds,
            maximum_event_age_seconds,
        })
    }

    /// Evaluate every running sensor once against new observations.
    #[pyo3(signature = (*, registry, target))]
    fn dispatch_once<'py>(
        &self,
        py: Python<'py>,
        registry: Any<'py>,
        target: Any<'py>,
    ) -> PyResult<Any<'py>> {
        self.call(py, "ensure_ready", None)?;
        let acquired: bool = self
            .call(
                py,
                "acquire_dispatch_lease",
                Some(py_kwargs!(py;
                    owner_id = self.dispatcher_id.bind(py),
                    ttl_seconds = self.lease_ttl_seconds,
                )?),
            )?
            .is_truthy()?;
        if !acquired {
            return summarize(py, &[], false);
        }
        let overrides: Any<'py> = self.call(py, "override_statuses", None)?;
        let event_kind: Any<'py> = member(py, "SensorKind", "EVENT")?;
        let mut outcomes: Vec<Outcome<'py>> = Vec::new();
        for sensor in registry.call_method0("ordered")?.try_iter()? {
            let sensor: Any<'py> = sensor?;
            if !Self::running(py, &sensor, &overrides)? {
                continue;
            }
            if sensor.getattr("kind")?.is(&event_kind) {
                let (event_outcomes, lease_held) =
                    self.dispatch_event_sensor(py, &sensor, &target)?;
                outcomes.extend(event_outcomes);
                if !lease_held {
                    return summarize(py, &outcomes, false);
                }
                continue;
            }
            if !self.renew_lease(py, &sensor)? {
                return summarize(py, &outcomes, false);
            }
            if let Some(outcome) = self.dispatch_polling_sensor(py, &sensor)? {
                outcomes.push(outcome);
            }
        }
        summarize(py, &outcomes, true)
    }

    /// Checkpoint newly enabled event sensors without evaluating their handlers.
    #[pyo3(signature = (*, registry, target))]
    fn initialize_event_checkpoints(
        &self,
        py: Python<'_>,
        registry: Any<'_>,
        target: Any<'_>,
    ) -> PyResult<()> {
        self.call(py, "ensure_ready", None)?;
        let overrides: Any<'_> = self.call(py, "override_statuses", None)?;
        let event_kind: Any<'_> = member(py, "SensorKind", "EVENT")?;
        for sensor in registry.call_method0("ordered")?.try_iter()? {
            let sensor: Any<'_> = sensor?;
            if !sensor.getattr("kind")?.is(&event_kind) || !Self::running(py, &sensor, &overrides)?
            {
                continue;
            }
            let declaration: Any<'_> = event_declaration_of(&sensor)?;
            let source: &str = event_source_for(&declaration.getattr("event_type")?)?;
            let checkpoint: Any<'_> = self.call(
                py,
                "read_checkpoint",
                Some(py_kwargs!(py; sensor_name = sensor.getattr("name")?, source = source)?),
            )?;
            if checkpoint.is_none() {
                self.initialize_checkpoint(py, &sensor, source, &target)?;
            }
        }
        Ok(())
    }

    /// Re-evaluate one dead-lettered event immediately; the outcome is recorded.
    #[pyo3(signature = (*, registry, sensor_name, event_id))]
    fn retry_dead_letter<'py>(
        &self,
        py: Python<'py>,
        registry: Any<'py>,
        sensor_name: Any<'py>,
        event_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let sensor: Any<'py> = self.require_event_sensor(py, &registry, &sensor_name)?;
        let event: Any<'py> = self.rebuild_event(py, &sensor, &event_id)?;
        let state: Any<'py> = self.attempt_state(py, &sensor_name, &event_id)?;
        let attempt: Any<'py> = state.getattr("failed_attempts")?.add(1)?;
        self.evaluate_event(py, &sensor, &event, &attempt)?
            .getattr("status")
    }

    /// Resolve one dead-lettered event with an explicit recorded reason.
    #[pyo3(signature = (*, registry, sensor_name, event_id, reason))]
    fn skip_dead_letter(
        &self,
        py: Python<'_>,
        registry: Any<'_>,
        sensor_name: Any<'_>,
        event_id: Any<'_>,
        reason: Any<'_>,
    ) -> PyResult<()> {
        let sensor: Any<'_> = self.require_event_sensor(py, &registry, &sensor_name)?;
        let state: Any<'_> = self.attempt_state(py, &sensor_name, &event_id)?;
        let now: Any<'_> = self.warehouse_now(py)?;
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorTickRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                tick_id = hex_uuid(py)?,
                sensor_name = sensor.getattr("name")?,
                definition_fingerprint = sensor.getattr("identity_fingerprint")?,
                kind = sensor.getattr("kind")?.str()?,
                event_id = &event_id,
                event_kind = py.None(),
                attempt = state.getattr("failed_attempts")?,
                status = member(py, "SensorTickStatus", "SKIPPED")?.str()?,
                started_at = &now,
                completed_at = &now,
                error_message = py.None(),
                skip_reason = reason,
                cursor = py.None(),
            )?),
        )?;
        self.call(
            py,
            "record_ticks",
            Some(py_kwargs!(py; ticks = PyTuple::new(py, [record])?)?),
        )?;
        Ok(())
    }
}
