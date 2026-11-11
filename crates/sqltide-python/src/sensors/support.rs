//! Shared lookups and conversions for the native sensor runtime.

use pyo3::prelude::*;
use pyo3::types::PyInt;
use sqltide_executor::timestamps::IsoDateTime;

use crate::dev_server::support::attr;
use crate::timestamps::timestamp_error;

pub(crate) const ADAPTER_MODELS: &str = "sqltide.adapter.models";
pub(crate) const EVENT_MODELS: &str = "sqltide.events.models";
pub(crate) const QUALITY_TYPES: &str = "sqltide.compiler.quality.types";
pub(crate) const RESULT_TYPES: &str = "sqltide.executor.auditing.types";
pub(crate) const SENSOR_MODELS: &str = "sqltide.sensors.models";
pub(crate) const SENSOR_TYPES: &str = "sqltide.sensors.types";
pub(crate) const SENSOR_EXCEPTIONS: &str = "sqltide.sensors.exceptions";

/// Chain `error` to the exception being handled, as a Python `raise` inside an `except` does.
pub(crate) fn chained_to_handled(py: Python<'_>, error: PyErr) -> PyErr {
    let handled: Bound<'_, PyAny> = match py
        .import("sys")
        .and_then(|sys| sys.call_method0("exception"))
    {
        Ok(handled) => handled,
        Err(failed) => return failed,
    };
    if !handled.is_none()
        && let Err(failed) = error.value(py).setattr("__context__", handled)
    {
        return failed;
    }
    error
}

/// `str(value)`.
pub(crate) fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(value.str()?.to_string())
}

/// `str(value) if value is not None else None`.
pub(crate) fn text_or_none(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    text(value).map(Some)
}

/// `int(str(value))`.
pub(crate) fn int_of_text<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value.py().get_type::<PyInt>().call1((value.str()?,))
}

/// `int(value)`.
pub(crate) fn int_of<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value.py().get_type::<PyInt>().call1((value,))
}

/// `uuid4().hex`.
pub(crate) fn hex_uuid(py: Python<'_>) -> PyResult<String> {
    attr(py, "uuid", "uuid4")?
        .call0()?
        .getattr("hex")?
        .extract()
}

/// `SensorError(message)`.
pub(crate) fn sensor_error(py: Python<'_>, message: String) -> PyErr {
    match attr(py, SENSOR_EXCEPTIONS, "SensorError").and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// `(datetime.fromisoformat(value) + timedelta(seconds=seconds))` as millisecond text.
pub(crate) fn shift_timestamp_text(value: &str, seconds: f64) -> PyResult<String> {
    IsoDateTime::parse(value)
        .and_then(|moment| moment.plus_seconds(seconds))
        .map(IsoDateTime::render_millis)
        .map_err(timestamp_error)
}

/// `datetime.fromisoformat(value) < datetime.fromisoformat(other)`.
pub(crate) fn timestamp_is_before(value: &str, other: &str) -> PyResult<bool> {
    IsoDateTime::parse(value)
        .and_then(|value| IsoDateTime::parse(other).and_then(|other| value.is_before(other)))
        .map_err(timestamp_error)
}
