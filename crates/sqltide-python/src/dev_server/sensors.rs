//! Sensor listing, tick history, dead-letter and override routes.

use pyo3::prelude::*;

use super::app::Call;
use super::auth::authenticated;
use super::authz;
use super::http::{Failure, HttpReply, Outcome, object, py_json, text};
use super::routes::{catching, invoke, locked, reading, servable};
use super::support::{ADAPTER_ERROR, attr};
use crate::py_kwargs;

const SENSORS_PAYLOAD: &str = "sqltide._native";
const SENSOR_ERROR: (&str, &str, u16) = ("sqltide.sensors.exceptions", "SensorError", 400);

fn scheduler<'py>(call: &Call<'py, '_>) -> Bound<'py, PyAny> {
    call.service(|services| &services.sensor_scheduler)
}

/// The compiled sensor registry, empty when the project declares no sensors.
fn registry<'py>(call: &Call<'py, '_>, analysis: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let sensors = analysis.getattr("sensors")?;
    if sensors.is_none() {
        return attr(call.py, "sqltide.sensors.models", "SensorRegistry")?.call0();
    }
    sensors.getattr("registry")
}

fn repository<'py>(call: &Call<'py, '_>, connection: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    invoke(
        py,
        "sqltide.sensors.classes.sensor_state_repository",
        "SensorStateRepository",
        py_kwargs!(py; connection = connection, database = call.app.services.database.clone())?,
    )
}

/// Run `body` over a read repository, or over none when no sensor state can exist.
fn with_read_repository<'py, T>(
    call: &Call<'py, '_>,
    analysis: &Bound<'py, PyAny>,
    body: impl FnOnce(Bound<'py, PyAny>) -> Outcome<T>,
) -> Outcome<T> {
    let declared: bool = registry(call, analysis)?.getattr("sensors")?.is_truthy()?;
    if call.app.services.database.is_none() || !declared {
        return body(call.py.None().into_bound(call.py));
    }
    reading(call, |connection| body(repository(call, connection)?))
}

fn require_sensor(call: &Call<'_, '_>, analysis: &Bound<'_, PyAny>, sensor_name: &str) -> Outcome<()> {
    if !registry(call, analysis)?.getattr("sensors")?.contains(sensor_name)? {
        return Err(Failure::status(404, format!("Unknown sensor '{sensor_name}'")));
    }
    Ok(())
}

fn required_repository(call: &Call<'_, '_>) -> Outcome<()> {
    if scheduler(call).getattr("repository")?.is_none() {
        return Err(Failure::status(503, "no warehouse connection"));
    }
    Ok(())
}

pub(crate) fn read_sensors(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let result = with_read_repository(call, &analysis, |repository| {
        Ok(invoke(
            py,
            SENSORS_PAYLOAD,
            "build_sensors_payload",
            py_kwargs!(py; analysis = &analysis, repository = repository, health = scheduler(call).call_method0("health")?)?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

/// An ISO timestamp window bound, passed on unchanged once it parses.
fn window_bound(py: Python<'_>, name: &str, value: Option<String>) -> Outcome<Option<String>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let datetime = attr(py, "datetime", "datetime")?;
    if let Err(error) = datetime.call_method1("fromisoformat", (value.as_str(),)) {
        if !error.is_instance_of::<pyo3::exceptions::PyValueError>(py) {
            return Err(error.into());
        }
        return Err(Failure::status(400, format!("{name} must be an ISO timestamp, received '{value}'")));
    }
    Ok(Some(value))
}

pub(crate) fn read_sensor_ticks(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let sensor_name: &str = call.path[0].text();
    let analysis = servable(call)?;
    require_sensor(call, &analysis, sensor_name)?;
    let result = with_read_repository(call, &analysis, |repository| {
        let after = window_bound(py, "after", call.query_text(1))?;
        let before = window_bound(py, "before", call.query_text(2))?;
        Ok(invoke(
            py,
            SENSORS_PAYLOAD,
            "build_sensor_ticks_payload",
            py_kwargs!(py;
                repository = repository,
                sensor_name = sensor_name,
                limit = call.query_int(0, 50),
                after = after,
                before = before,
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

pub(crate) fn read_dead_letters(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let result = with_read_repository(call, &analysis, |repository| {
        Ok(invoke(py, SENSORS_PAYLOAD, "build_dead_letters_payload", py_kwargs!(py; repository = repository)?)?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

pub(crate) fn set_sensor_status(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let sensor_name: &str = call.path[0].text();
    let analysis = servable(call)?;
    require_sensor(call, &analysis, sensor_name)?;
    let requested = call.body()?.getattr("status")?;
    let status = match attr(py, "sqltide.sensors.types", "SensorOverrideStatus")?.call1((&requested,)) {
        Ok(status) => status,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
            return Err(Failure::status(400, format!("Unknown sensor status '{}'", requested.str()?)));
        }
        Err(error) => return Err(error.into()),
    };
    authz::require_automation(call, Some(&analysis))?;
    let result: Outcome<()> = (|| {
        required_repository(call)?;
        if call.app.services.database.is_none() {
            return Err(Failure::status(503, "no warehouse connection"));
        }
        reading(call, |connection| {
            let repository = repository(call, connection)?;
            repository.call_method0("ensure_ready")?;
            let actor = authenticated(call)?.getattr("principal")?.getattr("username")?;
            repository.call_method(
                "record_override",
                (),
                Some(&py_kwargs!(py; sensor_name = sensor_name, status = &status, actor = actor)?),
            )?;
            Ok(())
        })
    })();
    catching(py, result, &[ADAPTER_ERROR])?;
    call.payload_json(&object(vec![
        ("sensorName", text(sensor_name)),
        ("status", text(status.str()?.to_string())),
    ]))
}

/// The dispatcher for dead-letter actions, or 503 without a warehouse.
fn dispatcher<'py>(call: &Call<'py, '_>, analysis: &Bound<'py, PyAny>) -> Outcome<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    let dispatcher = scheduler(call).call_method("build_dispatcher", (), Some(&py_kwargs!(py; analysis = analysis)?))?;
    if dispatcher.is_none() {
        required_repository(call)?;
        return Err(Failure::status(503, "no warehouse connection"));
    }
    Ok(dispatcher)
}

pub(crate) fn retry_dead_letter(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let request = call.body()?;
    let sensor_name: String = request.getattr("sensorName")?.extract()?;
    let event_id = request.getattr("eventId")?;
    let analysis = servable(call)?;
    require_sensor(call, &analysis, &sensor_name)?;
    authz::require_automation(call, Some(&analysis))?;
    let dispatcher = dispatcher(call, &analysis)?;
    let result = locked(call, || {
        Ok(dispatcher.call_method(
            "retry_dead_letter",
            (),
            Some(&py_kwargs!(py;
                registry = registry(call, &analysis)?,
                sensor_name = sensor_name.as_str(),
                event_id = &event_id,
            )?),
        )?)
    });
    let status = catching(py, result, &[SENSOR_ERROR, ADAPTER_ERROR])?;
    call.payload_json(&object(vec![
        ("sensorName", text(sensor_name)),
        ("eventId", py_json(&event_id)?),
        ("status", text(status.str()?.to_string())),
    ]))
}

pub(crate) fn skip_dead_letter(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let request = call.body()?;
    let sensor_name: String = request.getattr("sensorName")?.extract()?;
    let event_id = request.getattr("eventId")?;
    let analysis = servable(call)?;
    require_sensor(call, &analysis, &sensor_name)?;
    let supplied = request.getattr("reason")?;
    let reason: String = if supplied.is_truthy()? {
        supplied.call_method0("strip")?.extract()?
    } else {
        String::new()
    };
    if reason.is_empty() {
        return Err(Failure::status(400, "Skipping a dead letter requires a reason"));
    }
    authz::require_automation(call, Some(&analysis))?;
    let dispatcher = dispatcher(call, &analysis)?;
    let result = locked(call, || {
        Ok(dispatcher.call_method(
            "skip_dead_letter",
            (),
            Some(&py_kwargs!(py;
                registry = registry(call, &analysis)?,
                sensor_name = sensor_name.as_str(),
                event_id = &event_id,
                reason = reason.as_str(),
            )?),
        )?)
    });
    catching(py, result, &[SENSOR_ERROR, ADAPTER_ERROR])?;
    let skipped = attr(py, "sqltide.sensors.types", "SensorTickStatus")?.getattr("SKIPPED")?;
    call.payload_json(&object(vec![
        ("sensorName", text(sensor_name)),
        ("eventId", py_json(&event_id)?),
        ("status", text(skipped.str()?.to_string())),
    ]))
}
