//! Python bindings for compile artifact serialization and staging.

use std::path::PathBuf;

use pyo3::exceptions::{PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use sqltide_compile::staging::{StagingError, stage_files};
use sqltide_json::{
    JsonDialect, JsonEmitError, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions, dumps,
};

/// A Python value as JSON, accepting exactly what `json.dumps` accepts without a `default`.
fn json_value(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(match text.to_str() {
            Ok(text) => JsonValue::String(text.to_owned()),
            Err(_) => JsonValue::Utf16(utf16_units(text)?),
        });
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(JsonValue::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        let decimal: String = value
            .py()
            .get_type::<PyInt>()
            .call_method1("__repr__", (value,))?
            .extract()?;
        return JsonInteger::parse(&decimal)
            .map(JsonValue::Integer)
            .ok_or_else(|| PyValueError::new_err(format!("Invalid integer: {decimal}")));
    }
    if let Ok(number) = value.cast::<PyFloat>() {
        return Ok(JsonValue::Float(number.value()));
    }
    if let Ok(entries) = value.cast::<PyDict>() {
        let mut object: Vec<(JsonKey, JsonValue)> = Vec::with_capacity(entries.len());
        for (key, item) in entries.iter() {
            let Ok(key) = key.cast::<PyString>() else {
                return Err(PyTypeError::new_err(format!(
                    "keys must be str, int, float, bool or None, not {}",
                    key.get_type().name()?
                )));
            };
            let key: JsonKey = match key.to_str() {
                Ok(text) => JsonKey::Text(text.to_owned()),
                Err(_) => JsonKey::CodePoints(code_points(key)?),
            };
            object.push((key, json_value(&item)?));
        }
        return Ok(JsonValue::Object(object));
    }
    if let Ok(items) = value.cast::<PyList>() {
        return items
            .iter()
            .map(|item| json_value(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    if let Ok(items) = value.cast::<PyTuple>() {
        return items
            .iter()
            .map(|item| json_value(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    Err(PyTypeError::new_err(format!(
        "Object of type {} is not JSON serializable",
        value.get_type().name()?
    )))
}

/// A `str` holding lone surrogates, as the UTF-16 code units Python stores.
fn utf16_units(text: &Bound<'_, PyString>) -> PyResult<Vec<u16>> {
    let encoded: Vec<u8> = text
        .call_method1("encode", ("utf-16-le", "surrogatepass"))?
        .extract()?;
    Ok(encoded
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| u16::from_le_bytes(*pair))
        .collect())
}

/// A `str` holding surrogates, as its exact code points.
fn code_points(text: &Bound<'_, PyString>) -> PyResult<Vec<u32>> {
    let encoded: Vec<u8> = text
        .call_method1("encode", ("utf-32-le", "surrogatepass"))?
        .extract()?;
    Ok(encoded
        .as_chunks::<4>()
        .0
        .iter()
        .map(|unit| u32::from_le_bytes(*unit))
        .collect())
}

/// The text `json.dumps(value, indent=indent, sort_keys=sort_keys)` returns.
#[pyfunction]
#[pyo3(signature = (value, indent = None, sort_keys = false))]
fn json_dumps(
    value: &Bound<'_, PyAny>,
    indent: Option<usize>,
    sort_keys: bool,
) -> PyResult<String> {
    let value: JsonValue = json_value(value)?;
    let dialect: JsonDialect = JsonDialect::Stdlib(StdlibJsonOptions {
        sort_keys,
        ..StdlibJsonOptions::new(indent)
    });
    dumps(&value, &dialect).map_err(|error| match error {
        JsonEmitError::NestingTooDeep => PyRecursionError::new_err(
            "maximum recursion depth exceeded while encoding a JSON object",
        ),
        other => PyValueError::new_err(format!("Value is not JSON serializable: {other:?}")),
    })
}

/// Write `(relative path, contents)` files under a fresh staging root. An escaping path raises
/// `ValueError` with the compiler's message before anything is written.
#[pyfunction]
fn stage_artifact_files(
    py: Python<'_>,
    staging_root: PathBuf,
    files: Vec<(String, String)>,
) -> PyResult<()> {
    py.detach(|| stage_files(&staging_root, &files))
        .map_err(|error| match error {
            StagingError::Escapes(_) => PyValueError::new_err(error.to_string()),
            StagingError::Io(error) => PyErr::from(error),
        })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(json_dumps, module)?)?;
    module.add_function(wrap_pyfunction!(stage_artifact_files, module)?)?;
    Ok(())
}
