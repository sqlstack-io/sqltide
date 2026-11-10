//! Command output helpers: `json.dumps` of command payloads, prompts and Python formatting.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use sqltide_json::{JsonDialect, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions, dumps};

use crate::dev_server::support::attr;

/// A Python `str` as JSON text, keeping lone surrogates as UTF-16 units.
fn text_value(text: &Bound<'_, PyString>) -> PyResult<JsonValue> {
    match text.to_str() {
        Ok(value) => Ok(JsonValue::String(value.to_owned())),
        Err(_) => {
            let bytes: Vec<u8> = text
                .call_method1("encode", ("utf-16-le", "surrogatepass"))?
                .extract()?;
            Ok(JsonValue::Utf16(
                bytes
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .map(|pair| u16::from_le_bytes(*pair))
                    .collect(),
            ))
        }
    }
}

fn integer(value: &Bound<'_, PyAny>) -> PyResult<JsonInteger> {
    let decimal: String = value
        .py()
        .get_type::<PyInt>()
        .call_method1("__repr__", (value,))?
        .extract()?;
    JsonInteger::parse(&decimal)
        .ok_or_else(|| PyTypeError::new_err(format!("Invalid integer: {decimal}")))
}

/// The object key `json.dumps` writes for `key`.
fn key(key: &Bound<'_, PyAny>) -> PyResult<JsonKey> {
    if let Ok(text) = key.cast::<PyString>() {
        return Ok(match text_value(text)? {
            JsonValue::String(value) => JsonKey::Text(value),
            _ => JsonKey::CodePoints(
                text.call_method0("__iter__")?
                    .try_iter()?
                    .map(|character| -> PyResult<u32> {
                        attr(key.py(), "builtins", "ord")?
                            .call1((character?,))?
                            .extract()
                    })
                    .collect::<PyResult<_>>()?,
            ),
        });
    }
    let text: String = if key.is_none() {
        "null".to_owned()
    } else if let Ok(flag) = key.cast::<PyBool>() {
        if flag.is_true() { "true" } else { "false" }.to_owned()
    } else if key.is_instance_of::<PyInt>() {
        integer(key)?.as_str().to_owned()
    } else if key.is_instance_of::<PyFloat>() {
        key.py()
            .get_type::<PyFloat>()
            .call_method1("__repr__", (key,))?
            .extract()?
    } else {
        return Err(PyTypeError::new_err(format!(
            "keys must be str, int, float, bool or None, not {}",
            key.get_type().name()?
        )));
    };
    Ok(JsonKey::Text(text))
}

/// `value` as the standard library's `json.dumps` encodes it.
pub(crate) fn json_value(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if let Ok(text) = value.cast::<PyString>() {
        return text_value(text);
    }
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(JsonValue::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        return integer(value).map(JsonValue::Integer);
    }
    if let Ok(number) = value.cast::<PyFloat>() {
        return Ok(JsonValue::Float(number.value()));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        return value
            .try_iter()?
            .map(|item| json_value(&item?))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    if let Ok(entries) = value.cast::<PyDict>() {
        let mut items: Vec<(JsonKey, JsonValue)> = Vec::with_capacity(entries.len());
        for (name, item) in entries.iter() {
            items.push((key(&name)?, json_value(&item)?));
        }
        return Ok(JsonValue::Object(items));
    }
    Err(PyTypeError::new_err(format!(
        "Object of type {} is not JSON serializable",
        value.get_type().name()?
    )))
}

/// `json.dumps(value, indent=indent)` of an already built JSON value.
pub(crate) fn json_text(value: &JsonValue, indent: Option<usize>) -> PyResult<String> {
    dumps(value, &JsonDialect::Stdlib(StdlibJsonOptions::new(indent)))
        .map_err(|error| PyTypeError::new_err(format!("{error:?}")))
}

/// A JSON object of named values, in order.
pub(crate) fn object(entries: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(name, value)| (JsonKey::Text(name.to_owned()), value))
            .collect(),
    )
}

/// A JSON object of `value`'s named attributes, in order.
pub(crate) fn fields(value: &Bound<'_, PyAny>, names: &[&str]) -> PyResult<JsonValue> {
    let mut entries: Vec<(&str, JsonValue)> = Vec::with_capacity(names.len());
    for name in names {
        entries.push((name, json_value(&value.getattr(*name)?)?));
    }
    Ok(object(entries))
}

/// `f"{value}"`.
pub(crate) fn shown(value: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(value.str()?.to_str()?.to_owned())
}

/// `str(value)` of `value.<name>`.
pub(crate) fn shown_field(value: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    shown(&value.getattr(name)?)
}

/// `", ".join(value.<name>)`.
pub(crate) fn joined(value: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    Ok(value
        .getattr(name)?
        .try_iter()?
        .map(|item| item?.extract::<String>())
        .collect::<PyResult<Vec<String>>>()?
        .join(", "))
}

/// `input(prompt).strip().lower() in AFFIRMATIVE_RESPONSES`.
pub(crate) fn confirmed(py: Python<'_>, prompt: &str) -> PyResult<bool> {
    let response: Bound<'_, PyAny> = attr(py, "builtins", "input")?
        .call1((prompt,))?
        .call_method0("strip")?
        .call_method0("lower")?;
    attr(py, "sqltide.cli.entry.constants", "AFFIRMATIVE_RESPONSES")?.contains(response)
}
