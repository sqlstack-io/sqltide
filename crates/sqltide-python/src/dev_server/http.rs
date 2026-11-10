//! The HTTP contract the dev server keeps from its earlier ASGI stack: request parsing, route
//! matching with its 404, 405 and trailing-slash redirects, JSON error bodies, gzip, cookies and
//! packaged static files.

use std::io::Write;
use std::time::{SystemTime, UNIX_EPOCH};

use flate2::Compression;
use flate2::write::GzEncoder;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple};
use sqltide_json::{JsonDialect, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions, dumps};

const GZIP_MINIMUM_SIZE: usize = 1000;
const JSON_CONTENT_TYPE: &str = "application/json";

/// One HTTP request as the handlers see it.
#[derive(Clone, Debug)]
pub(crate) struct HttpRequest {
    /// Upper-case method.
    pub(crate) method: String,
    /// Percent-decoded path.
    pub(crate) path: String,
    /// Raw query string, without `?`.
    pub(crate) query_string: String,
    /// Header names in lower case, values decoded as Latin-1, in request order.
    pub(crate) headers: Vec<(String, String)>,
    pub(crate) body: Vec<u8>,
    pub(crate) client_host: Option<String>,
    pub(crate) scheme: String,
}

impl HttpRequest {
    /// The first value of a header, compared case-insensitively.
    pub(crate) fn header(&self, name: &str) -> Option<&str> {
        let wanted: String = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(key, _)| *key == wanted)
            .map(|(_, value)| value.as_str())
    }

    /// The cookies of the first `Cookie` header; a later duplicate name wins.
    pub(crate) fn cookie(&self, name: &str) -> Option<String> {
        let header: &str = self.header("cookie")?;
        let mut found: Option<String> = None;
        for chunk in header.split(';') {
            let (key, value) = chunk.split_once('=').unwrap_or(("", chunk));
            let (key, value) = (python_strip(key), python_strip(value));
            if (!key.is_empty() || !value.is_empty()) && key == name {
                found = Some(cookie_unquote(value));
            }
        }
        found
    }

    /// Query parameters as `parse_qsl(..., keep_blank_values=True)` reads them.
    pub(crate) fn query(&self) -> Vec<(String, String)> {
        self.query_string
            .split('&')
            .filter(|piece| !piece.is_empty())
            .map(|piece| {
                let (name, value) = piece.split_once('=').unwrap_or((piece, ""));
                (
                    percent_decode(&name.replace('+', " ")),
                    percent_decode(&value.replace('+', " ")),
                )
            })
            .collect()
    }
}

/// Python's `str.strip()` for the whitespace a cookie header can hold.
fn python_strip(text: &str) -> &str {
    text.trim_matches(|character: char| character.is_whitespace())
}

/// `http.cookies._unquote`: a double-quoted value loses its quotes and backslash escapes.
fn cookie_unquote(value: &str) -> String {
    if value.len() < 2 || !value.starts_with('"') || !value.ends_with('"') {
        return value.to_owned();
    }
    let inner: &[u8] = &value.as_bytes()[1..value.len() - 1];
    let mut out: Vec<u8> = Vec::with_capacity(inner.len());
    let mut index: usize = 0;
    while index < inner.len() {
        if inner[index] == b'\\' && index + 1 < inner.len() {
            let octal: &[u8] = &inner[index + 1..inner.len().min(index + 4)];
            if octal.len() == 3
                && octal
                    .iter()
                    .all(|byte| (b'0'..=b'3').contains(&octal[0]) && (b'0'..=b'7').contains(byte))
            {
                let code: u32 = octal
                    .iter()
                    .fold(0, |total, byte| total * 8 + u32::from(byte - b'0'));
                let mut buffer: [u8; 4] = [0; 4];
                let character: char = char::from_u32(code).unwrap_or('\u{fffd}');
                out.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
                index += 4;
                continue;
            }
            out.push(inner[index + 1]);
            index += 2;
            continue;
        }
        out.push(inner[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `urllib.parse.unquote`: valid `%XX` escapes decode as UTF-8 with replacement.
pub(crate) fn percent_decode(text: &str) -> String {
    if !text.contains('%') {
        return text.to_owned();
    }
    let bytes: &[u8] = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index: usize = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let high = (bytes[index + 1] as char).to_digit(16);
            let low = (bytes[index + 2] as char).to_digit(16);
            if let (Some(high), Some(low)) = (high, low) {
                out.push(u8::try_from(high * 16 + low).unwrap_or(0));
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// One response before transport.
#[derive(Clone, Debug)]
pub(crate) struct HttpReply {
    pub(crate) status: u16,
    pub(crate) headers: Vec<(String, String)>,
    pub(crate) body: Vec<u8>,
}

impl HttpReply {
    /// A `Response(body, media_type=...)`: `Content-Length` (unless the status has no body)
    /// and then `Content-Type`, the order Starlette initializes them in.
    pub(crate) fn new(status: u16, content_type: &str, body: Vec<u8>) -> Self {
        let mut headers: Vec<(String, String)> = Vec::new();
        if !bodiless(status) {
            headers.push(("content-length".to_owned(), body.len().to_string()));
        }
        headers.push(("content-type".to_owned(), content_type.to_owned()));
        Self {
            status,
            headers,
            body,
        }
    }

    pub(crate) fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// `MutableHeaders.__setitem__`: replace the first occurrence in place and drop the rest,
    /// or append a header that is absent.
    pub(crate) fn set_header(&mut self, name: &str, value: String) {
        let mut slot: Option<usize> = None;
        let mut index: usize = 0;
        self.headers.retain(|(key, _)| {
            let matched: bool = key.eq_ignore_ascii_case(name);
            let keep: bool = !matched || slot.is_none();
            if matched && slot.is_none() {
                slot = Some(index);
            }
            if keep {
                index += 1;
            }
            keep
        });
        match slot {
            Some(position) => self.headers[position].1 = value,
            None => self.headers.push((name.to_ascii_lowercase(), value)),
        }
    }

    /// A handler's JSON result, serialized as the response model serializer writes it.
    pub(crate) fn payload_with_status(status: u16, value: &JsonValue) -> PyResult<Self> {
        let text: String = dumps(value, &JsonDialect::Pydantic)
            .map_err(|error| PyValueError::new_err(format!("{error:?}")))?;
        Ok(Self::new(status, JSON_CONTENT_TYPE, text.into_bytes()))
    }

    /// A `JSONResponse(content)`, which the standard library serializes.
    pub(crate) fn json(status: u16, value: &JsonValue) -> PyResult<Self> {
        Ok(Self::new(
            status,
            JSON_CONTENT_TYPE,
            stdlib_json(value)?.into_bytes(),
        ))
    }
}

/// `json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))`.
pub(crate) fn stdlib_json(value: &JsonValue) -> PyResult<String> {
    let options: StdlibJsonOptions = StdlibJsonOptions {
        indent: None,
        item_separator: ",".to_owned(),
        key_separator: ":".to_owned(),
        ensure_ascii: false,
        sort_keys: false,
        allow_nan: false,
    };
    dumps(value, &JsonDialect::Stdlib(options))
        .map_err(|error| PyValueError::new_err(format!("{error:?}")))
}

/// A failure that ends one request.
#[derive(Debug)]
pub(crate) enum Failure {
    /// `HTTPException(status_code, detail, headers)`.
    Http {
        status: u16,
        detail: JsonValue,
        headers: Vec<(String, String)>,
    },
    /// Request validation errors, answered with 422.
    Validation(Vec<JsonValue>),
    /// An unhandled Python exception: a 500 for clients, re-raised for in-process callers.
    Python(PyErr),
}

impl From<PyErr> for Failure {
    fn from(error: PyErr) -> Self {
        Self::Python(error)
    }
}

pub(crate) type Outcome<T> = Result<T, Failure>;

impl Failure {
    pub(crate) fn status(status: u16, detail: impl Into<String>) -> Self {
        Self::Http {
            status,
            detail: JsonValue::String(detail.into()),
            headers: Vec::new(),
        }
    }

    pub(crate) fn detail(status: u16, detail: JsonValue) -> Self {
        Self::Http {
            status,
            detail,
            headers: Vec::new(),
        }
    }

    /// The reply FastAPI's HTTP exception handler writes.
    pub(crate) fn reply(
        status: u16,
        detail: &JsonValue,
        headers: &[(String, String)],
    ) -> PyResult<HttpReply> {
        let mut reply: HttpReply = if bodiless(status) {
            HttpReply {
                status,
                headers: Vec::new(),
                body: Vec::new(),
            }
        } else {
            HttpReply::json(status, &object(vec![("detail", detail.clone())]))?
        };
        // Headers passed to the response come before the ones it initializes.
        reply.headers.splice(
            0..0,
            headers
                .iter()
                .map(|(name, value)| (name.to_ascii_lowercase(), value.clone())),
        );
        Ok(reply)
    }
}

/// A status whose response carries no body, so Starlette sets no `Content-Length`.
pub(crate) fn bodiless(status: u16) -> bool {
    status < 200 || status == 204 || status == 304
}

/// A JSON object from string keys.
pub(crate) fn object(entries: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (JsonKey::Text(key.to_owned()), value))
            .collect(),
    )
}

pub(crate) fn text(value: impl Into<String>) -> JsonValue {
    JsonValue::String(value.into())
}

pub(crate) fn integer(value: i128) -> JsonValue {
    JsonValue::Integer(
        JsonInteger::parse(&value.to_string()).unwrap_or_else(|| {
            JsonInteger::parse("0").unwrap_or_else(|| unreachable!("zero parses"))
        }),
    )
}

/// A handler's Python result as the response serializer writes a value typed `object`: enum
/// members as their values, tuples and sets as arrays and dataclasses as objects.
pub(crate) fn py_json(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(JsonValue::Bool(flag.is_true()));
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(JsonValue::String(text.to_str()?.to_owned()));
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
        let mut items: Vec<(JsonKey, JsonValue)> = Vec::with_capacity(entries.len());
        for (key, item) in entries.iter() {
            let key: String = match key.cast::<PyString>() {
                Ok(text) => text.to_str()?.to_owned(),
                Err(_) => key.str()?.to_str()?.to_owned(),
            };
            items.push((JsonKey::Text(key), py_json(&item)?));
        }
        return Ok(JsonValue::Object(items));
    }
    if let Ok(items) = value.cast::<PyList>() {
        return items
            .iter()
            .map(|item| py_json(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    if let Ok(items) = value.cast::<PyTuple>() {
        return items
            .iter()
            .map(|item| py_json(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    if let Ok(items) = value.cast::<PySet>() {
        return items
            .iter()
            .map(|item| py_json(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    if let Ok(items) = value.cast::<PyFrozenSet>() {
        return items
            .iter()
            .map(|item| py_json(&item))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array);
    }
    // Enum members, dataclasses, paths, UUIDs, datetimes and the rest: as pydantic writes a
    // value typed `object`.
    let jsonable = value
        .py()
        .import("pydantic_core")?
        .call_method1("to_jsonable_python", (value,))?;
    if jsonable.is(value) {
        return Err(PyTypeError::new_err(format!(
            "Unable to serialize unknown type: {}",
            value.get_type().name()?
        )));
    }
    py_json(&jsonable)
}

/// One route: its methods and path template.
pub(crate) struct Route<H> {
    pub(crate) methods: &'static [&'static str],
    pub(crate) template: String,
    segments: Vec<Segment>,
    pub(crate) handler: H,
}

enum Segment {
    Literal(String),
    Parameter(String),
    Rest(String),
}

impl<H> Route<H> {
    pub(crate) fn new(methods: &'static [&'static str], template: &str, handler: H) -> Self {
        let segments: Vec<Segment> = template
            .trim_start_matches('/')
            .split('/')
            .map(|segment| {
                match segment
                    .strip_prefix('{')
                    .and_then(|rest| rest.strip_suffix('}'))
                {
                    Some(name) => match name.strip_suffix(":path") {
                        Some(name) => Segment::Rest(name.to_owned()),
                        None => Segment::Parameter(name.to_owned()),
                    },
                    None => Segment::Literal(segment.to_owned()),
                }
            })
            .collect();
        Self {
            methods,
            template: template.to_owned(),
            segments,
            handler,
        }
    }

    fn allows(&self, method: &str) -> bool {
        self.methods.contains(&method)
    }

    /// Path parameters when the path matches this template.
    fn matches(&self, path: &str) -> Option<Vec<(String, String)>> {
        let rest: &str = path.strip_prefix('/')?;
        let mut parts: std::str::Split<'_, char> = rest.split('/');
        let mut remaining: &str = rest;
        let mut parameters: Vec<(String, String)> = Vec::new();
        for segment in &self.segments {
            match segment {
                Segment::Rest(name) => {
                    parameters.push((name.clone(), remaining.to_owned()));
                    return Some(parameters);
                }
                Segment::Literal(literal) => {
                    let part: &str = parts.next()?;
                    if part != literal {
                        return None;
                    }
                    remaining = remaining.get(part.len() + 1..).unwrap_or("");
                }
                Segment::Parameter(name) => {
                    let part: &str = parts.next()?;
                    if part.is_empty() {
                        return None;
                    }
                    parameters.push((name.clone(), part.to_owned()));
                    remaining = remaining.get(part.len() + 1..).unwrap_or("");
                }
            }
        }
        parts.next().is_none().then_some(parameters)
    }
}

/// What routing decided for one request.
pub(crate) enum Routed<'a, H> {
    Matched(&'a H, Vec<(String, String)>),
    MethodNotAllowed(Vec<&'static str>),
    Redirect(String),
    NotFound,
}

/// Starlette's routing: the first full match runs; otherwise the first path-only match answers
/// 405; otherwise a path that matches with its trailing slash toggled redirects; otherwise 404.
pub(crate) fn route<'a, H>(routes: &'a [Route<H>], request: &HttpRequest) -> Routed<'a, H> {
    let mut partial: Option<&Route<H>> = None;
    for candidate in routes {
        if let Some(parameters) = candidate.matches(&request.path) {
            if candidate.allows(&request.method) {
                return Routed::Matched(&candidate.handler, parameters);
            }
            partial = partial.or(Some(candidate));
        }
    }
    if let Some(candidate) = partial {
        return Routed::MethodNotAllowed(candidate.methods.to_vec());
    }
    if request.path != "/" {
        let toggled: String = match request.path.strip_suffix('/') {
            Some(stripped) => stripped.to_owned(),
            None => format!("{}/", request.path),
        };
        if routes
            .iter()
            .any(|candidate| candidate.matches(&toggled).is_some())
        {
            let host: &str = request.header("host").unwrap_or("");
            let mut location: String = format!("{}://{host}{toggled}", request.scheme);
            if !request.query_string.is_empty() {
                location.push('?');
                location.push_str(&request.query_string);
            }
            return Routed::Redirect(location);
        }
    }
    Routed::NotFound
}

const GZIP_EXCLUDED_CONTENT_TYPES: [&str; 14] = [
    "application/grpc",
    "application/gzip",
    "application/x-gzip",
    "application/zip",
    "audio/*",
    "font/woff",
    "font/woff2",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/event-stream",
    "video/*",
];

/// The GZip middleware: a body of at least the minimum size gains `Vary: Accept-Encoding`,
/// and is compressed at level 9 when the client accepts gzip.
pub(crate) fn gzip(request: &HttpRequest, mut reply: HttpReply) -> HttpReply {
    let media: String = reply
        .header("content-type")
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase();
    let family: String = format!("{}/*", media.split('/').next().unwrap_or(""));
    let excluded: bool = GZIP_EXCLUDED_CONTENT_TYPES.contains(&media.as_str())
        || GZIP_EXCLUDED_CONTENT_TYPES.contains(&family.as_str())
        || media.starts_with("application/grpc+");
    if excluded
        || reply.status == 206
        || reply.header("content-encoding").is_some()
        || reply.body.len() < GZIP_MINIMUM_SIZE
    {
        return reply;
    }
    let vary: String = match reply.header("vary") {
        Some(existing) => format!("{existing}, Accept-Encoding"),
        None => "Accept-Encoding".to_owned(),
    };
    reply.set_header("vary", vary);
    let accepts: bool = request
        .header("accept-encoding")
        .is_some_and(|value| value.contains("gzip"));
    if !accepts {
        return reply;
    }
    let mut encoder: GzEncoder<Vec<u8>> = GzEncoder::new(Vec::new(), Compression::new(9));
    if encoder.write_all(&reply.body).is_err() {
        return reply;
    }
    let Ok(compressed) = encoder.finish() else {
        return reply;
    };
    reply.set_header("content-encoding", "gzip".to_owned());
    reply.set_header("content-length", compressed.len().to_string());
    reply.body = compressed;
    reply
}

/// A `Set-Cookie` value in the attribute order Python's `http.cookies` writes.
pub(crate) fn set_cookie(
    name: &str,
    value: &str,
    max_age: i64,
    expires: Option<String>,
    http_only: bool,
    secure: bool,
) -> String {
    let mut parts: Vec<String> = vec![format!("{name}={}", cookie_quote(value))];
    if let Some(expires) = expires {
        parts.push(format!("expires={expires}"));
    }
    if http_only {
        parts.push("HttpOnly".to_owned());
    }
    parts.push(format!("Max-Age={max_age}"));
    parts.push("Path=/".to_owned());
    parts.push("SameSite=lax".to_owned());
    if secure {
        parts.push("Secure".to_owned());
    }
    parts.join("; ")
}

/// `http.cookies._quote`: legal values pass through; anything else is double-quoted.
fn cookie_quote(value: &str) -> String {
    const LEGAL: &str = "!#$%&'*+-.^_`|~:";
    if !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || LEGAL.contains(character))
    {
        return value.to_owned();
    }
    let mut quoted: String = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => quoted.push_str("\\\""),
            '\\' => quoted.push_str("\\\\"),
            character
                if u32::from(character) < 0x20
                    || (0x7f..=0xff).contains(&u32::from(character))
                    || character == ','
                    || character == ';' =>
            {
                quoted.push_str(&format!("\\{:03o}", u32::from(character)));
            }
            character => quoted.push(character),
        }
    }
    quoted.push('"');
    quoted
}

/// An RFC 1123 date in GMT, as `email.utils.formatdate(usegmt=True)` writes it.
pub(crate) fn http_date(seconds: i64) -> String {
    const DAYS: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"];
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let days: i64 = seconds.div_euclid(86_400);
    let remainder: i64 = seconds.rem_euclid(86_400);
    let weekday: &str = DAYS[usize::try_from(days.rem_euclid(7)).unwrap_or(0)];
    // Civil-from-days (Howard Hinnant).
    let shifted: i64 = days + 719_468;
    let era: i64 = shifted.div_euclid(146_097);
    let day_of_era: i64 = shifted.rem_euclid(146_097);
    let year_of_era: i64 =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year: i64 = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index: i64 = (5 * day_of_year + 2) / 153;
    let day: i64 = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month: i64 = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year: i64 = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{weekday}, {day:02} {} {year:04} {:02}:{:02}:{:02} GMT",
        MONTHS[usize::try_from(month - 1).unwrap_or(0)],
        remainder / 3600,
        remainder % 3600 / 60,
        remainder % 60,
    )
}

pub(crate) fn now_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

/// MD5, used only for the ETag format the earlier static file server wrote.
pub(crate) fn md5_hex(input: &[u8]) -> String {
    use md5::{Digest, Md5};
    Md5::digest(input)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The router's 405 for a path whose routes take other methods.
pub(crate) fn method_not_allowed(methods: &[&str]) -> HttpReply {
    Failure::reply(
        405,
        &text("Method Not Allowed"),
        &[("Allow".to_owned(), methods.join(", "))],
    )
    .unwrap_or_else(|_| HttpReply::new(405, JSON_CONTENT_TYPE, Vec::new()))
}
