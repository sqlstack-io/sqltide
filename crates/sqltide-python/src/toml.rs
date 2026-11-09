//! TOML 1.0 documents loaded natively into the values `tomllib.loads` returns.

use std::path::PathBuf;

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sqltide_config::errors::ConfigError;
use sqltide_config::models::{ConfigDate, ConfigTime, ConfigValue};
use sqltide_config::pipelines;
use sqltide_config::toml::main::load_toml::load_toml as load_document;

/// The failure as `tomllib.TOMLDecodeError`, with `tomllib`'s location suffix.
pub(crate) fn decode_error(py: Python<'_>, error: &ConfigError) -> PyErr {
    match py
        .import("tomllib")
        .and_then(|module| module.getattr("TOMLDecodeError"))
        .and_then(|class| class.call1((error.tomllib_text(),)))
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(failure) => failure,
    }
}

fn date<'py>(datetime: &Bound<'py, PyModule>, value: ConfigDate) -> PyResult<Bound<'py, PyAny>> {
    datetime
        .getattr("date")?
        .call1((value.year, value.month, value.day))
}

fn time_arguments(value: ConfigTime) -> (u8, u8, u8, u32) {
    (value.hour, value.minute, value.second, value.microsecond)
}

fn python_value<'py>(
    py: Python<'py>,
    datetime: &Bound<'py, PyModule>,
    value: ConfigValue,
) -> PyResult<Bound<'py, PyAny>> {
    match value {
        ConfigValue::Bool(flag) => flag.into_bound_py_any(py),
        ConfigValue::Integer(number) => number.into_bound_py_any(py),
        ConfigValue::Float(number) => number.into_bound_py_any(py),
        ConfigValue::String(text) => text.into_bound_py_any(py),
        ConfigValue::Date(value) => date(datetime, value),
        ConfigValue::Time(value) => datetime.getattr("time")?.call1(time_arguments(value)),
        ConfigValue::DateTime(value) => {
            let (hour, minute, second, microsecond) = time_arguments(value.time);
            let tzinfo: Option<Bound<'py, PyAny>> = value
                .utc_offset_seconds
                .map(|seconds| -> PyResult<Bound<'py, PyAny>> {
                    let delta = datetime.getattr("timedelta")?.call1((0, seconds))?;
                    datetime.getattr("timezone")?.call1((delta,))
                })
                .transpose()?;
            datetime.getattr("datetime")?.call1((
                value.date.year,
                value.date.month,
                value.date.day,
                hour,
                minute,
                second,
                microsecond,
                tzinfo,
            ))
        }
        ConfigValue::List(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(python_value(py, datetime, item)?)?;
            }
            list.into_bound_py_any(py)
        }
        ConfigValue::Map(entries) => {
            let dict = PyDict::new(py);
            for (key, item) in entries {
                dict.set_item(key, python_value(py, datetime, item)?)?;
            }
            dict.into_bound_py_any(py)
        }
    }
}

/// A loaded TOML document as the `dict` `tomllib.loads` returns.
pub(crate) fn python_document<'py>(
    py: Python<'py>,
    document: ConfigValue,
) -> PyResult<Bound<'py, PyAny>> {
    python_value(py, &py.import("datetime")?, document)
}

/// `tomllib.loads(text)`, raising `tomllib.TOMLDecodeError` for an invalid document.
#[pyfunction]
fn load_toml<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    let document: ConfigValue = load_document(text).map_err(|error| decode_error(py, &error))?;
    python_document(py, document)
}

/// Whether any `<pipelines_root>/*/pipeline.toml` sets `mode = "virtual"`, skipping unreadable
/// and invalid files, which discovery reports.
#[pyfunction]
fn declares_virtual_pipeline(pipelines_root: PathBuf) -> bool {
    pipelines::declares_virtual_pipeline(&pipelines_root)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(load_toml, module)?)?;
    module.add_function(wrap_pyfunction!(declares_virtual_pipeline, module)?)
}
