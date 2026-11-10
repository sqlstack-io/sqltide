//! Calls into Python collaborators and the request parameter parsing FastAPI applied.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use sqltide_json::{JsonKey, JsonValue};

use super::http::{Failure, HttpRequest, Outcome, integer, object, py_json, text};

/// Keyword arguments from `(name, value)` pairs.
pub(crate) fn kwargs<'py>(
    py: Python<'py>,
    entries: Vec<(&str, Bound<'py, PyAny>)>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in entries {
        dict.set_item(name, value)?;
    }
    Ok(dict)
}

/// `name=value` keyword pairs whose values convert into Python.
#[macro_export]
macro_rules! py_kwargs {
    ($py:expr; $($name:ident = $value:expr),* $(,)?) => {
        $crate::dev_server::support::kwargs($py, vec![
            $((stringify!($name), pyo3::IntoPyObjectExt::into_bound_py_any($value, $py)?)),*
        ])
    };
}

/// The attribute `name` of module `module`, looked up when called so tests can patch it.
pub(crate) fn attr<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)
}

/// `pydantic.TypeAdapter(annotation)` as module-level code builds it. The adapter reads its
/// caller's frame two levels up, and request handlers run on threads with no Python frames, so
/// it is built inside an evaluated frame whose namespace holds only the annotation.
pub(crate) fn type_adapter<'py>(annotation: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = annotation.py();
    let namespace: Bound<'py, PyDict> = PyDict::new(py);
    namespace.set_item("__name__", "sqltide._native")?;
    namespace.set_item("TypeAdapter", attr(py, "pydantic", "TypeAdapter")?)?;
    namespace.set_item("annotation", annotation)?;
    py.eval(c"TypeAdapter(annotation)", Some(&namespace), None)
}

/// Whether `error` is an instance of `module.name`.
pub(crate) fn is_instance(
    py: Python<'_>,
    error: &PyErr,
    module: &str,
    name: &str,
) -> PyResult<bool> {
    let class: Bound<'_, PyAny> = attr(py, module, name)?;
    error.value(py).is_instance(&class)
}

/// `except A, B as error: raise HTTPException(status, str(error))` over ordered clauses; an
/// exception no clause names passes through unchanged.
pub(crate) fn translate(py: Python<'_>, error: PyErr, clauses: &[(&str, &str, u16)]) -> Failure {
    for (module, name, status) in clauses {
        match is_instance(py, &error, module, name) {
            Ok(true) => {
                return match error.value(py).str() {
                    Ok(message) => Failure::status(*status, message.to_string()),
                    Err(failure) => Failure::Python(failure),
                };
            }
            Ok(false) => {}
            Err(failure) => return Failure::Python(failure),
        }
    }
    Failure::Python(error)
}

/// The adapter failure every warehouse-backed route answers with 502.
pub(crate) const ADAPTER_ERROR: (&str, &str, u16) =
    ("sqltide.adapter.exceptions", "AdapterError", 502);

/// `str(error)` of a Python exception.
pub(crate) fn message(py: Python<'_>, error: &PyErr) -> PyResult<String> {
    Ok(error.value(py).str()?.to_string())
}

/// One FastAPI validation error.
pub(crate) fn validation_error(
    kind: &str,
    loc: Vec<JsonValue>,
    msg: &str,
    input: JsonValue,
    ctx: Option<JsonValue>,
) -> JsonValue {
    let mut entries: Vec<(&str, JsonValue)> = vec![
        ("type", text(kind)),
        ("loc", JsonValue::Array(loc)),
        ("msg", text(msg)),
        ("input", input),
    ];
    if let Some(ctx) = ctx {
        entries.push(("ctx", ctx));
    }
    object(entries)
}

/// The kinds of path and query parameters the routes declare.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Scalar {
    Text { min_length: Option<usize> },
    Integer { ge: Option<i64>, le: Option<i64> },
    Boolean,
    Uuid,
}

/// A parsed scalar parameter.
#[derive(Clone, Debug)]
pub(crate) enum Value {
    Text(String),
    Integer(i128),
    Boolean(bool),
    Uuid(String),
    List(Vec<String>),
}

impl Value {
    pub(crate) fn text(&self) -> &str {
        match self {
            Self::Text(value) | Self::Uuid(value) => value,
            _ => "",
        }
    }

    pub(crate) fn integer(&self) -> i128 {
        match self {
            Self::Integer(value) => *value,
            _ => 0,
        }
    }

    pub(crate) fn boolean(&self) -> bool {
        matches!(self, Self::Boolean(true))
    }
}

/// Parse one string as pydantic's lax mode reads a str for `scalar`.
pub(crate) fn parse_scalar(
    py: Python<'_>,
    scalar: Scalar,
    raw: &str,
    loc: Vec<JsonValue>,
) -> PyResult<Result<Value, JsonValue>> {
    if matches!(scalar, Scalar::Uuid) {
        return parse_uuid(py, raw, loc);
    }
    Ok(parse_plain(scalar, raw, loc))
}

fn parse_plain(scalar: Scalar, raw: &str, loc: Vec<JsonValue>) -> Result<Value, JsonValue> {
    let input: JsonValue = text(raw);
    match scalar {
        Scalar::Text { min_length } => {
            if let Some(minimum) = min_length
                && raw.chars().count() < minimum
            {
                let plural: &str = if minimum == 1 {
                    "character"
                } else {
                    "characters"
                };
                return Err(validation_error(
                    "string_too_short",
                    loc,
                    &format!("String should have at least {minimum} {plural}"),
                    input,
                    Some(object(vec![("min_length", integer(minimum as i128))])),
                ));
            }
            Ok(Value::Text(raw.to_owned()))
        }
        Scalar::Integer { ge, le } => {
            let Some(value) = str_as_int(raw) else {
                return Err(validation_error(
                    "int_parsing",
                    loc,
                    "Input should be a valid integer, unable to parse string as an integer",
                    input,
                    None,
                ));
            };
            if let Some(bound) = ge
                && value < i128::from(bound)
            {
                return Err(validation_error(
                    "greater_than_equal",
                    loc,
                    &format!("Input should be greater than or equal to {bound}"),
                    input,
                    Some(object(vec![("ge", integer(i128::from(bound)))])),
                ));
            }
            if let Some(bound) = le
                && value > i128::from(bound)
            {
                return Err(validation_error(
                    "less_than_equal",
                    loc,
                    &format!("Input should be less than or equal to {bound}"),
                    input,
                    Some(object(vec![("le", integer(i128::from(bound)))])),
                ));
            }
            Ok(Value::Integer(value))
        }
        Scalar::Boolean => match raw.to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => Ok(Value::Boolean(true)),
            "0" | "off" | "f" | "false" | "n" | "no" => Ok(Value::Boolean(false)),
            _ => Err(validation_error(
                "bool_parsing",
                loc,
                "Input should be a valid boolean, unable to interpret input",
                input,
                None,
            )),
        },
        Scalar::Uuid => unreachable!("UUID parameters are validated by pydantic"),
    }
}

/// The installed pydantic validated UUID parameters, so its version decides which spellings
/// parse and how a failure reads.
fn parse_uuid(
    py: Python<'_>,
    raw: &str,
    loc: Vec<JsonValue>,
) -> PyResult<Result<Value, JsonValue>> {
    static ADAPTER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    let adapter: &Py<PyAny> = ADAPTER.get_or_try_init(py, || {
        Ok::<_, PyErr>(type_adapter(&attr(py, "uuid", "UUID")?)?.unbind())
    })?;
    match adapter.bind(py).call_method(
        "validate_python",
        (raw,),
        Some(&py_kwargs!(py; from_attributes = true)?),
    ) {
        Ok(parsed) => Ok(Ok(Value::Uuid(parsed.str()?.to_string()))),
        Err(error) if is_instance(py, &error, "pydantic", "ValidationError")? => {
            let first: Bound<'_, PyAny> = error.value(py).call_method0("errors")?.get_item(0)?;
            let kind: String = first.get_item("type")?.extract()?;
            let message: String = first.get_item("msg")?.extract()?;
            let context: Bound<'_, PyAny> = first.call_method1("get", ("ctx",))?;
            let context: Option<JsonValue> = if context.is_none() {
                None
            } else {
                Some(py_json(&context)?)
            };
            Ok(Err(validation_error(
                &kind,
                loc,
                &message,
                text(raw),
                context,
            )))
        }
        Err(error) => Err(error),
    }
}

/// pydantic's str-to-int: surrounding whitespace, a sign, digits with single underscores
/// between them, and an optional all-zero fraction.
pub(crate) fn str_as_int(raw: &str) -> Option<i128> {
    let trimmed: &str = raw.trim_matches(|character: char| character.is_ascii_whitespace());
    let (negative, unsigned) = match trimmed.as_bytes().first()? {
        b'-' => (true, &trimmed[1..]),
        b'+' => (false, &trimmed[1..]),
        _ => (false, trimmed),
    };
    let whole: &str = match unsigned.split_once('.') {
        Some((whole, fraction)) => {
            if fraction.is_empty() || !fraction.bytes().all(|byte| byte == b'0') {
                return None;
            }
            whole
        }
        None => unsigned,
    };
    if whole.is_empty()
        || whole.starts_with('_')
        || whole.ends_with('_')
        || whole.contains("__")
        || !whole
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'_')
    {
        return None;
    }
    let digits: String = whole.replace('_', "");
    let magnitude: i128 = digits.parse().ok()?;
    Some(if negative { -magnitude } else { magnitude })
}

/// FastAPI's request body handling for one body parameter: JSON when the content type says
/// so, raw bytes otherwise, and nothing for an empty body.
pub(crate) enum Body<'py> {
    Missing,
    Parsed(Bound<'py, PyAny>),
}

/// Read the request body as FastAPI's route handler reads it.
pub(crate) fn read_body<'py>(py: Python<'py>, request: &HttpRequest) -> Outcome<Body<'py>> {
    if request.body.is_empty() {
        return Ok(Body::Missing);
    }
    let raw: Bound<'py, PyBytes> = PyBytes::new(py, &request.body);
    let is_json: bool = request
        .header("content-type")
        .is_some_and(json_content_type);
    if !is_json {
        return Ok(Body::Parsed(raw.into_any()));
    }
    let json = py.import("json")?;
    match json.call_method1("loads", (&raw,)) {
        Ok(value) => Ok(Body::Parsed(value)),
        Err(error) => {
            let decode_error = json.getattr("JSONDecodeError")?;
            if error.value(py).is_instance(&decode_error)? {
                let value = error.value(py);
                let position: i128 = value.getattr("pos")?.extract()?;
                let reason: String = value.getattr("msg")?.extract()?;
                return Err(validation_failure(vec![validation_error(
                    "json_invalid",
                    vec![text("body"), integer(position)],
                    "JSON decode error",
                    JsonValue::Object(Vec::new()),
                    Some(object(vec![("error", text(reason))])),
                )]));
            }
            Err(Failure::status(400, "There was an error parsing the body"))
        }
    }
}

/// `email.message.Message` content-type parsing reduced to "application/json" or a
/// "+json" application subtype.
fn json_content_type(value: &str) -> bool {
    let media: String = value
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let Some((main, subtype)) = media.split_once('/') else {
        return false;
    };
    main == "application" && (subtype == "json" || subtype.ends_with("+json"))
}

/// A 422 response from accumulated validation errors.
pub(crate) fn validation_failure(errors: Vec<JsonValue>) -> Failure {
    Failure::Validation(errors)
}

/// Validate a parsed body against a request model, as FastAPI validates a body parameter
/// (`validate_python(value, from_attributes=True)`); errors gain the `body` location prefix.
pub(crate) fn validate_body<'py>(
    py: Python<'py>,
    body: &Body<'py>,
    module: &str,
    model: &str,
    errors: &mut Vec<JsonValue>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Body::Parsed(value) = body else {
        errors.push(validation_error(
            "missing",
            vec![text("body")],
            "Field required",
            JsonValue::Null,
            None,
        ));
        return Ok(None);
    };
    let class: Bound<'py, PyAny> = attr(py, module, model)?;
    let adapter = type_adapter(&class)?;
    match adapter.call_method(
        "validate_python",
        (value,),
        Some(&py_kwargs!(py; from_attributes = true)?),
    ) {
        Ok(validated) => Ok(Some(validated)),
        Err(error) => {
            let validation_type = attr(py, "pydantic", "ValidationError")?;
            if !error.value(py).is_instance(&validation_type)? {
                return Err(error);
            }
            let raised = error.value(py);
            let details =
                raised.call_method("errors", (), Some(&py_kwargs!(py; include_url = false)?))?;
            for detail in details.cast::<PyList>()?.iter() {
                let detail = detail.cast_into::<PyDict>()?;
                let loc = detail.get_item("loc")?.map_or(Ok(Vec::new()), |loc| {
                    loc.cast::<PyTuple>()?
                        .iter()
                        .map(|part| encodable(&part))
                        .collect::<PyResult<Vec<JsonValue>>>()
                })?;
                let mut full_loc: Vec<JsonValue> = vec![text("body")];
                full_loc.extend(loc);
                let mut entries: Vec<(JsonKey, JsonValue)> = Vec::new();
                for (key, item) in detail.iter() {
                    let key: String = key.extract()?;
                    let item: JsonValue = if key == "loc" {
                        JsonValue::Array(full_loc.clone())
                    } else {
                        encodable(&item)?
                    };
                    entries.push((JsonKey::Text(key), item));
                }
                errors.push(JsonValue::Object(entries));
            }
            Ok(None)
        }
    }
}

/// `jsonable_encoder` for the values a validation error carries: JSON values, tuples, and
/// bytes decoded as UTF-8.
pub(crate) fn encodable(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if let Ok(bytes) = value.cast::<PyBytes>() {
        let decoded: Bound<'_, PyAny> = bytes.call_method0("decode")?;
        return Ok(JsonValue::String(
            decoded.cast::<PyString>()?.to_str()?.to_owned(),
        ));
    }
    py_json(value)
}

/// A body typed `dict[str, str]`.
pub(crate) fn validate_string_mapping<'py>(
    py: Python<'py>,
    body: &Body<'py>,
    errors: &mut Vec<JsonValue>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Body::Parsed(value) = body else {
        errors.push(validation_error(
            "missing",
            vec![text("body")],
            "Field required",
            JsonValue::Null,
            None,
        ));
        return Ok(None);
    };
    let annotation = py.eval(c"dict[str, str]", None, None)?;
    let adapter = type_adapter(&annotation)?;
    match adapter.call_method(
        "validate_python",
        (value,),
        Some(&py_kwargs!(py; from_attributes = true)?),
    ) {
        Ok(validated) => Ok(Some(validated)),
        Err(error) => {
            let validation_type = attr(py, "pydantic", "ValidationError")?;
            if !error.value(py).is_instance(&validation_type)? {
                return Err(error);
            }
            let details = error.value(py).call_method(
                "errors",
                (),
                Some(&py_kwargs!(py; include_url = false)?),
            )?;
            for detail in details.cast::<PyList>()?.iter() {
                let detail = detail.cast_into::<PyDict>()?;
                let mut entries: Vec<(JsonKey, JsonValue)> = Vec::new();
                for (key, item) in detail.iter() {
                    let key: String = key.extract()?;
                    let item: JsonValue = if key == "loc" {
                        let mut loc: Vec<JsonValue> = vec![text("body")];
                        for part in item.cast::<PyTuple>()?.iter() {
                            loc.push(encodable(&part)?);
                        }
                        JsonValue::Array(loc)
                    } else {
                        encodable(&item)?
                    };
                    entries.push((JsonKey::Text(key), item));
                }
                errors.push(JsonValue::Object(entries));
            }
            Ok(None)
        }
    }
}

/// `a, b, ... = value`: the items of an `count`-element unpacking, raising Python's `ValueError`
/// for any other length.
pub(crate) fn unpack<'py>(
    value: &Bound<'py, PyAny>,
    count: usize,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(count);
    for item in value.try_iter()? {
        if items.len() == count {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "too many values to unpack (expected {count})"
            )));
        }
        items.push(item?);
    }
    if items.len() < count {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "not enough values to unpack (expected {count}, got {})",
            items.len()
        )));
    }
    Ok(items)
}
