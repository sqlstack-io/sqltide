//! Durable sensor state persistence and observation stream reads.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDateTime, PyDict, PySlice, PyTuple};

use super::queries;
use super::support::{
    ADAPTER_MODELS, EVENT_MODELS, QUALITY_TYPES, RESULT_TYPES, SENSOR_MODELS, SENSOR_TYPES,
    hex_uuid, int_of, int_of_text, shift_timestamp_text, text, text_or_none,
};
use crate::dev_server::support::attr;
use crate::py_kwargs;

const DISPATCH_LEASE_NAME: &str = "sensor_dispatch";
const TERMINAL_TICK_STATUSES: [&str; 4] = ["skipped", "succeeded", "failed", "dead_lettered"];
const RESOLVED_TICK_STATUSES: [&str; 3] = ["skipped", "succeeded", "dead_lettered"];

type Any<'py> = Bound<'py, PyAny>;

/// A warehouse timestamp value as millisecond text: `datetime` values render as
/// `%Y-%m-%d %H:%M:%S.%f` without the last three digits, anything else through `str()`.
pub(crate) fn timestamp_text(value: &Any<'_>) -> PyResult<String> {
    if value.is_instance_of::<PyDateTime>() {
        let field = |name: &str| -> PyResult<i64> { value.getattr(name)?.extract() };
        return Ok(format!(
            "{}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}",
            field("year")?,
            field("month")?,
            field("day")?,
            field("hour")?,
            field("minute")?,
            field("second")?,
            field("microsecond")? / 1_000
        ));
    }
    text(value)
}

fn timestamp_text_or_none(value: &Any<'_>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    timestamp_text(value).map(Some)
}

fn column<'py>(row: &Any<'py>, name: &str) -> PyResult<Any<'py>> {
    row.get_item(name)
}

fn position<'py>(py: Python<'py>, completed_at: String, result_id: String) -> PyResult<Any<'py>> {
    attr(py, SENSOR_MODELS, "SensorStreamPosition")?.call(
        (),
        Some(&py_kwargs!(py; completed_at = completed_at, result_id = result_id)?),
    )
}

/// One persisted tick row decoded, with the fields the reductions read.
struct Tick<'py> {
    tick_id: String,
    status: String,
    started_at: String,
    completed_at: Option<String>,
    error_message: Option<String>,
    view: Any<'py>,
}

fn decode_tick<'py>(row: &Any<'py>) -> PyResult<Tick<'py>> {
    let py: Python<'py> = row.py();
    let tick_id: String = text(&column(row, "tick_id")?)?;
    let sensor_name: String = text(&column(row, "sensor_name")?)?;
    let definition_fingerprint: String = text(&column(row, "definition_fingerprint")?)?;
    let kind: String = text(&column(row, "kind")?)?;
    let event_id: Option<String> = text_or_none(&column(row, "event_id")?)?;
    let event_kind: Option<String> = text_or_none(&column(row, "event_kind")?)?;
    let attempt: Any<'py> = int_of_text(&column(row, "attempt")?)?;
    let status: String = text(&column(row, "status")?)?;
    let started_at: String = timestamp_text(&column(row, "started_at")?)?;
    let completed_at: Option<String> = timestamp_text_or_none(&column(row, "completed_at")?)?;
    let error_message: Option<String> = text_or_none(&column(row, "error_message")?)?;
    let skip_reason: Option<String> = text_or_none(&column(row, "skip_reason")?)?;
    let cursor: Option<String> = text_or_none(&column(row, "cursor")?)?;
    let view: Any<'py> = attr(py, SENSOR_MODELS, "SensorTickView")?.call(
        (),
        Some(&py_kwargs!(py;
            tick_id = &tick_id,
            sensor_name = sensor_name,
            definition_fingerprint = definition_fingerprint,
            kind = kind,
            event_id = event_id,
            event_kind = event_kind,
            attempt = attempt,
            status = &status,
            started_at = &started_at,
            completed_at = &completed_at,
            error_message = &error_message,
            skip_reason = skip_reason,
            cursor = cursor,
        )?),
    )?;
    Ok(Tick {
        tick_id,
        status,
        started_at,
        completed_at,
        error_message,
        view,
    })
}

/// Collapse started and terminal rows into one tick each, in start order.
fn reduce_ticks<'py>(rows: &[Any<'py>]) -> PyResult<Vec<Tick<'py>>> {
    let mut by_tick: HashMap<String, Tick<'py>> = HashMap::new();
    let mut order: Vec<String> = Vec::new();
    for row in rows {
        let tick: Tick<'py> = decode_tick(row)?;
        match by_tick.get(&tick.tick_id) {
            None => {
                order.push(tick.tick_id.clone());
                by_tick.insert(tick.tick_id.clone(), tick);
            }
            Some(existing) if !TERMINAL_TICK_STATUSES.contains(&existing.status.as_str()) => {
                by_tick.insert(tick.tick_id.clone(), tick);
            }
            Some(_) => {}
        }
    }
    let mut ordered: Vec<Tick<'py>> = order
        .iter()
        .filter_map(|tick_id| by_tick.remove(tick_id))
        .collect();
    ordered.sort_by(|left, right| {
        (&left.started_at, &left.tick_id).cmp(&(&right.started_at, &right.tick_id))
    });
    Ok(ordered)
}

fn decode_node_result<'py>(row: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = row.py();
    let scheduled_for: Any<'py> = column(row, "scheduled_for")?;
    let error_message: Any<'py> = column(row, "error_message")?;
    let severity: Any<'py> = column(row, "severity")?;
    attr(py, EVENT_MODELS, "NodeResultObservation")?.call(
        (),
        Some(&py_kwargs!(py;
            result_id = text(&column(row, "result_id")?)?,
            invocation_id = text(&column(row, "invocation_id")?)?,
            node_kind = attr(py, QUALITY_TYPES, "QualityNodeKind")?
                .call1((text(&column(row, "node_kind")?)?,))?,
            node_name = text(&column(row, "node_name")?)?,
            binding_key = text(&column(row, "binding_key")?)?,
            target_identity = text(&column(row, "target_identity")?)?,
            trigger = text(&column(row, "trigger")?)?,
            status = attr(py, RESULT_TYPES, "QualityResultStatus")?
                .call1((text(&column(row, "status")?)?,))?,
            severity = text_or_none(&severity)?,
            failure_count = int_of_text(&column(row, "failure_count")?)?,
            completed_at = timestamp_text(&column(row, "completed_at")?)?,
            scheduled_for = timestamp_text_or_none(&scheduled_for)?,
            payload_json = text(&column(row, "payload_json")?)?,
            error_message = text_or_none(&error_message)?,
        )?),
    )
}

fn decode_invocation<'py>(row: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = row.py();
    let mode: Any<'py> = column(row, "mode")?;
    let deployment_id: Any<'py> = column(row, "deployment_id")?;
    let error_message: Any<'py> = column(row, "error_message")?;
    attr(py, EVENT_MODELS, "InvocationObservation")?.call(
        (),
        Some(&py_kwargs!(py;
            invocation_id = text(&column(row, "invocation_id")?)?,
            command = text(&column(row, "command")?)?,
            mode = text_or_none(&mode)?,
            outcome = text(&column(row, "outcome")?)?,
            exit_code = int_of_text(&column(row, "exit_code")?)?,
            target_identity = text(&column(row, "target_identity")?)?,
            deployment_id = text_or_none(&deployment_id)?,
            selected_node_count = int_of_text(&column(row, "selected_node_count")?)?,
            error_message = text_or_none(&error_message)?,
            completed_at = timestamp_text(&column(row, "completed_at")?)?,
        )?),
    )
}

/// All sensor reads and writes against one metadata database.
#[pyclass(module = "sqltide._native", subclass)]
pub(crate) struct SensorStateRepository {
    connection: Py<PyAny>,
    database: String,
    migrated: bool,
}

impl SensorStateRepository {
    fn query<'py>(&self, py: Python<'py>, statement: String) -> PyResult<Vec<Any<'py>>> {
        self.connection
            .bind(py)
            .call_method1("query", (statement,))?
            .call_method0("named_rows")?
            .try_iter()?
            .collect()
    }

    fn execute(&self, py: Python<'_>, rendered: &Any<'_>) -> PyResult<()> {
        if !rendered.is_truthy()? {
            return Ok(());
        }
        let statement_class: Any<'_> =
            attr(py, "sqltide.executor.workflow.models", "WarehouseStatement")?;
        let finalization: Any<'_> = attr(py, "sqltide.executor.workflow.types", "WorkflowPhase")?
            .getattr("FINALIZATION")?;
        let mutation: Any<'_> =
            attr(py, "sqltide.executor.workflow.types", "StatementIntent")?.getattr("MUTATION")?;
        let mut statements: Vec<Any<'_>> = Vec::new();
        for (index, sql) in rendered.try_iter()?.enumerate() {
            let sequence: usize = index + 1;
            statements.push(statement_class.call(
                (),
                Some(&py_kwargs!(py;
                    sequence = sequence,
                    step_id = format!("record_sensor_state_{sequence}"),
                    phase = &finalization,
                    intent = &mutation,
                    sql = sql?,
                )?),
            )?);
        }
        attr(
            py,
            "sqltide.executor.workflow.main._execute_warehouse_workflow",
            "execute_warehouse_workflow",
        )?
        .call(
            (),
            Some(&py_kwargs!(py;
                statements = PyTuple::new(py, statements)?,
                connection = self.connection.bind(py),
            )?),
        )?;
        Ok(())
    }

    fn persist(&mut self, py: Python<'_>, state: Any<'_>) -> PyResult<()> {
        let rendered: Any<'_> = self.connection.bind(py).call_method(
            "render_sensor_state",
            (),
            Some(&py_kwargs!(py;
                database = &self.database,
                state = state,
                include_migration = !self.migrated,
            )?),
        )?;
        self.execute(py, &rendered)?;
        self.migrated = true;
        Ok(())
    }

    fn state<'py>(py: Python<'py>, field: &str, records: Vec<Any<'py>>) -> PyResult<Any<'py>> {
        let arguments: Bound<'py, PyDict> = PyDict::new(py);
        arguments.set_item(field, PyTuple::new(py, records)?)?;
        attr(py, ADAPTER_MODELS, "AdapterSensorState")?.call((), Some(&arguments))
    }

    fn first_position<'py>(
        &self,
        py: Python<'py>,
        statement: String,
        completed_at: &str,
        identifier: &str,
    ) -> PyResult<Option<Any<'py>>> {
        let rows: Vec<Any<'py>> = self.query(py, statement)?;
        let Some(row) = rows.first() else {
            return Ok(None);
        };
        position(
            py,
            timestamp_text(&column(row, completed_at)?)?,
            text(&column(row, identifier)?)?,
        )
        .map(Some)
    }

    fn current_lease_owner(&self, py: Python<'_>, now: &str) -> PyResult<Option<String>> {
        let rows: Vec<Any<'_>> = self.query(
            py,
            queries::current_lease(&self.database, DISPATCH_LEASE_NAME, now),
        )?;
        rows.first()
            .map(|row| text(&column(row, "owner_id")?))
            .transpose()
    }
}

/// `int(limit)` as SQL text.
fn limit_text(limit: &Any<'_>) -> PyResult<String> {
    text(&int_of(limit)?)
}

fn position_fields(position: &Any<'_>) -> PyResult<(String, String)> {
    Ok((
        position.getattr("completed_at")?.extract()?,
        position.getattr("result_id")?.extract()?,
    ))
}

#[pymethods]
impl SensorStateRepository {
    #[new]
    #[pyo3(signature = (*, connection, database))]
    fn new(connection: Py<PyAny>, database: String) -> Self {
        Self {
            connection,
            database,
            migrated: false,
        }
    }

    /// Idempotently create sensor state tables before the first read.
    fn ensure_ready(&mut self, py: Python<'_>) -> PyResult<()> {
        let state: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorState")?.call0()?;
        self.persist(py, state)
    }

    /// Capture the warehouse's UTC millisecond timestamp.
    fn warehouse_now<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        self.connection
            .bind(py)
            .call_method0("capture_warehouse_timestamp")
    }

    #[pyo3(signature = (*, sensor_name, source))]
    fn read_checkpoint<'py>(
        &self,
        py: Python<'py>,
        sensor_name: &str,
        source: &str,
    ) -> PyResult<Option<Any<'py>>> {
        self.first_position(
            py,
            queries::checkpoint(&self.database, sensor_name, source),
            "position_completed_at",
            "position_result_id",
        )
    }

    #[pyo3(signature = (*, sensor_name, source, position))]
    fn advance_checkpoint(
        &mut self,
        py: Python<'_>,
        sensor_name: Any<'_>,
        source: Any<'_>,
        position: Any<'_>,
    ) -> PyResult<()> {
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorCheckpointRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                sensor_name = sensor_name,
                source = source,
                position_completed_at = position.getattr("completed_at")?,
                position_result_id = position.getattr("result_id")?,
            )?),
        )?;
        let state: Any<'_> = Self::state(py, "checkpoints", vec![record])?;
        self.persist(py, state)
    }

    #[pyo3(signature = (*, target))]
    fn newest_node_result_position<'py>(
        &self,
        py: Python<'py>,
        target: &str,
    ) -> PyResult<Option<Any<'py>>> {
        self.first_position(
            py,
            queries::newest_node_result_position(&self.database, target),
            "completed_at",
            "result_id",
        )
    }

    #[pyo3(signature = (*, target))]
    fn newest_invocation_position<'py>(
        &self,
        py: Python<'py>,
        target: &str,
    ) -> PyResult<Option<Any<'py>>> {
        self.first_position(
            py,
            queries::newest_invocation_position(&self.database, target),
            "completed_at",
            "invocation_id",
        )
    }

    #[pyo3(signature = (*, position, target, limit))]
    fn fetch_node_results_after<'py>(
        &self,
        py: Python<'py>,
        position: Any<'py>,
        target: &str,
        limit: Any<'py>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let (completed_at, result_id) = position_fields(&position)?;
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::node_results_after(
                &self.database,
                target,
                &completed_at,
                &result_id,
                &limit_text(&limit)?,
            ),
        )?;
        let decoded: Vec<Any<'py>> = rows
            .iter()
            .map(decode_node_result)
            .collect::<PyResult<_>>()?;
        PyTuple::new(py, decoded)
    }

    #[pyo3(signature = (*, result_id))]
    fn fetch_node_result<'py>(
        &self,
        py: Python<'py>,
        result_id: &str,
    ) -> PyResult<Option<Any<'py>>> {
        let rows: Vec<Any<'py>> =
            self.query(py, queries::node_result_by_id(&self.database, result_id))?;
        rows.first().map(decode_node_result).transpose()
    }

    #[pyo3(signature = (*, position, target, limit))]
    fn fetch_invocations_after<'py>(
        &self,
        py: Python<'py>,
        position: Any<'py>,
        target: &str,
        limit: Any<'py>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let (completed_at, invocation_id) = position_fields(&position)?;
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::invocations_after(
                &self.database,
                target,
                &completed_at,
                &invocation_id,
                &limit_text(&limit)?,
            ),
        )?;
        let decoded: Vec<Any<'py>> = rows
            .iter()
            .map(decode_invocation)
            .collect::<PyResult<_>>()?;
        PyTuple::new(py, decoded)
    }

    #[pyo3(signature = (*, invocation_id))]
    fn fetch_invocation<'py>(
        &self,
        py: Python<'py>,
        invocation_id: &str,
    ) -> PyResult<Option<Any<'py>>> {
        let rows: Vec<Any<'py>> =
            self.query(py, queries::invocation_by_id(&self.database, invocation_id))?;
        rows.first().map(decode_invocation).transpose()
    }

    #[pyo3(signature = (*, binding_key, target, position))]
    fn previous_node_status<'py>(
        &self,
        py: Python<'py>,
        binding_key: &str,
        target: &str,
        position: Any<'py>,
    ) -> PyResult<Option<Any<'py>>> {
        let (completed_at, result_id) = position_fields(&position)?;
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::previous_node_status(
                &self.database,
                target,
                binding_key,
                &completed_at,
                &result_id,
            ),
        )?;
        rows.first()
            .map(|row| {
                attr(py, RESULT_TYPES, "QualityResultStatus")?
                    .call1((text(&column(row, "status")?)?,))
            })
            .transpose()
    }

    #[pyo3(signature = (*, ticks))]
    fn record_ticks(&mut self, py: Python<'_>, ticks: Any<'_>) -> PyResult<()> {
        let arguments: Bound<'_, PyDict> = PyDict::new(py);
        arguments.set_item("ticks", ticks)?;
        let state: Any<'_> =
            attr(py, ADAPTER_MODELS, "AdapterSensorState")?.call((), Some(&arguments))?;
        self.persist(py, state)
    }

    #[pyo3(signature = (*, sensor_name, event_id))]
    fn tick_attempt_state<'py>(
        &self,
        py: Python<'py>,
        sensor_name: &str,
        event_id: &str,
    ) -> PyResult<Any<'py>> {
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::event_ticks(&self.database, sensor_name, event_id),
        )?;
        let ticks: Vec<Tick<'py>> = reduce_ticks(&rows)?;
        let failed: Vec<&Tick<'py>> = ticks
            .iter()
            .filter(|tick| tick.status == "failed")
            .collect();
        let last_failed: Option<&&Tick<'py>> = failed.last();
        let last_failed_at: Option<String> = last_failed.map(|tick| match &tick.completed_at {
            Some(completed_at) if !completed_at.is_empty() => completed_at.clone(),
            _ => tick.started_at.clone(),
        });
        attr(py, SENSOR_MODELS, "TickAttemptState")?.call(
            (),
            Some(&py_kwargs!(py;
                failed_attempts = failed.len(),
                last_failed_at = last_failed_at,
                last_error_message = last_failed.and_then(|tick| tick.error_message.clone()),
                resolved = ticks
                    .iter()
                    .any(|tick| RESOLVED_TICK_STATUSES.contains(&tick.status.as_str())),
            )?),
        )
    }

    #[pyo3(signature = (*, sensor_name, limit, after=None, before=None))]
    fn list_ticks<'py>(
        &self,
        py: Python<'py>,
        sensor_name: &str,
        limit: Any<'py>,
        after: Option<String>,
        before: Option<String>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::sensor_ticks(
                &self.database,
                sensor_name,
                &limit_text(&limit.mul(2)?)?,
                after.as_deref(),
                before.as_deref(),
            ),
        )?;
        let views: Vec<Any<'py>> = reduce_ticks(&rows)?
            .into_iter()
            .rev()
            .map(|tick| tick.view)
            .collect();
        let end: isize = limit.extract()?;
        PyTuple::new(py, views)?
            .as_any()
            .get_item(PySlice::new(py, 0, end, 1))?
            .cast_into::<PyTuple>()
            .map_err(Into::into)
    }

    fn list_dead_letters<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let rows: Vec<Any<'py>> =
            self.query(py, queries::dead_letter_candidate_ticks(&self.database))?;
        let mut groups: Vec<((String, String), Vec<Any<'py>>)> = Vec::new();
        for row in rows {
            let event_id: Any<'py> = column(&row, "event_id")?;
            let key: (String, String) = (
                text(&column(&row, "sensor_name")?)?,
                if event_id.is_truthy()? {
                    text(&event_id)?
                } else {
                    String::new()
                },
            );
            match groups.iter_mut().find(|(existing, _)| *existing == key) {
                Some((_, members)) => members.push(row),
                None => groups.push((key, vec![row])),
            }
        }
        let mut dead_letters: Vec<Tick<'py>> = Vec::new();
        for (_, members) in groups {
            let ticks: Vec<Tick<'py>> = reduce_ticks(&members)?;
            let unresolved: bool = !ticks
                .iter()
                .any(|tick| tick.status == "succeeded" || tick.status == "skipped");
            if !unresolved {
                continue;
            }
            if let Some(dead) = ticks
                .into_iter()
                .filter(|tick| tick.status == "dead_lettered")
                .last()
            {
                dead_letters.push(dead);
            }
        }
        dead_letters.sort_by(|left, right| {
            (&left.started_at, &left.tick_id).cmp(&(&right.started_at, &right.tick_id))
        });
        PyTuple::new(py, dead_letters.into_iter().map(|tick| tick.view))
    }

    #[pyo3(signature = (*, sensor_name))]
    fn polling_tick_state<'py>(&self, py: Python<'py>, sensor_name: &str) -> PyResult<Any<'py>> {
        let start_rows: Vec<Any<'py>> = self.query(
            py,
            queries::latest_polling_tick_start(&self.database, sensor_name),
        )?;
        let success_rows: Vec<Any<'py>> = self.query(
            py,
            queries::latest_polling_success(&self.database, sensor_name),
        )?;
        let cursor: Option<String> = match success_rows.first() {
            Some(row) => text_or_none(&column(row, "cursor")?)?,
            None => None,
        };
        let last_started_at: Option<String> = start_rows
            .first()
            .map(|row| timestamp_text(&column(row, "started_at")?))
            .transpose()?;
        let last_success_at: Option<String> = match success_rows.first() {
            Some(row) => timestamp_text_or_none(&column(row, "completed_at")?)?,
            None => None,
        };
        attr(py, SENSOR_MODELS, "PollingTickState")?.call(
            (),
            Some(&py_kwargs!(py;
                last_started_at = last_started_at,
                last_success_at = last_success_at,
                cursor = cursor,
            )?),
        )
    }

    #[pyo3(signature = (*, sensor_name, event_id, step_key))]
    fn read_step<'py>(
        &self,
        py: Python<'py>,
        sensor_name: &str,
        event_id: &str,
        step_key: &str,
    ) -> PyResult<Option<Any<'py>>> {
        let rows: Vec<Any<'py>> = self.query(
            py,
            queries::step_markers(&self.database, sensor_name, event_id, step_key),
        )?;
        if rows.is_empty() {
            return Ok(None);
        }
        let mut attempt: Option<Any<'py>> = None;
        for row in &rows {
            let value: Any<'py> = int_of_text(&column(row, "attempt")?)?;
            attempt = match attempt {
                Some(current) if !value.gt(&current)? => Some(current),
                _ => Some(value),
            };
        }
        let mut statuses: Vec<String> = Vec::with_capacity(rows.len());
        for row in &rows {
            statuses.push(text(&column(row, "status")?)?);
        }
        let marker = |status: &str, result_json: Option<String>| -> PyResult<Any<'py>> {
            attr(py, SENSOR_MODELS, "StepMarker")?.call(
                (),
                Some(&py_kwargs!(py;
                    status = status,
                    result_json = result_json,
                    attempt = attempt.clone(),
                )?),
            )
        };
        if let Some(index) = statuses.iter().rposition(|status| status == "succeeded") {
            let result_json: Option<String> = text_or_none(&column(&rows[index], "result_json")?)?;
            return marker("succeeded", result_json).map(Some);
        }
        let status: &str = if statuses.iter().any(|status| status == "failed") {
            "failed"
        } else {
            "started"
        };
        marker(status, None).map(Some)
    }

    #[pyo3(signature = (*, step))]
    fn record_step(&mut self, py: Python<'_>, step: Any<'_>) -> PyResult<()> {
        let state: Any<'_> = Self::state(py, "steps", vec![step])?;
        self.persist(py, state)
    }

    fn override_statuses<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let rows: Vec<Any<'py>> = self.query(py, queries::override_statuses(&self.database))?;
        let status_class: Any<'py> = attr(py, SENSOR_TYPES, "SensorOverrideStatus")?;
        let statuses: Bound<'py, PyDict> = PyDict::new(py);
        for row in rows {
            let name: String = text(&column(&row, "sensor_name")?)?;
            let status: Any<'py> = status_class.call1((text(&column(&row, "status")?)?,))?;
            statuses.set_item(name, status)?;
        }
        Ok(statuses)
    }

    #[pyo3(signature = (*, sensor_name, status, actor))]
    fn record_override(
        &mut self,
        py: Python<'_>,
        sensor_name: Any<'_>,
        status: Any<'_>,
        actor: Any<'_>,
    ) -> PyResult<()> {
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorOverrideRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                override_id = hex_uuid(py)?,
                sensor_name = sensor_name,
                status = status.str()?,
                actor = actor,
            )?),
        )?;
        let state: Any<'_> = Self::state(py, "overrides", vec![record])?;
        self.persist(py, state)
    }

    /// Delete aged ticks and steps; a zero retention keeps everything.
    #[pyo3(signature = (*, retention_days))]
    fn apply_tick_retention(&self, py: Python<'_>, retention_days: Any<'_>) -> PyResult<()> {
        let rendered: Any<'_> = self.connection.bind(py).call_method(
            "render_sensor_retention_cleanup",
            (),
            Some(&py_kwargs!(py; database = &self.database, retention_days = retention_days)?),
        )?;
        self.execute(py, &rendered)
    }

    /// Best-effort warehouse-time lease so parallel dispatchers do not double-fire.
    #[pyo3(signature = (*, owner_id, ttl_seconds))]
    fn acquire_dispatch_lease(
        slf: &Bound<'_, Self>,
        owner_id: &str,
        ttl_seconds: f64,
    ) -> PyResult<bool> {
        let py: Python<'_> = slf.py();
        let now: String = slf.call_method0("warehouse_now")?.extract()?;
        let current: Option<String> = slf.borrow().current_lease_owner(py, &now)?;
        if current
            .as_deref()
            .is_some_and(|current| current != owner_id)
        {
            return Ok(false);
        }
        let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorLeaseRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                lease_name = DISPATCH_LEASE_NAME,
                owner_id = owner_id,
                acquired_at = &now,
                expires_at = shift_timestamp_text(&now, ttl_seconds)?,
            )?),
        )?;
        let state: Any<'_> = Self::state(py, "leases", vec![record])?;
        slf.borrow_mut().persist(py, state)?;
        Ok(slf.borrow().current_lease_owner(py, &now)?.as_deref() == Some(owner_id))
    }
}
