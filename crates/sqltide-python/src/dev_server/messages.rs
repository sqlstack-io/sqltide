//! Warehouse-backed message browsing compiled from typed, whitelisted predicate documents.
//!
//! Request text is carried as UTF-8 bytes with lone surrogates kept as `surrogatepass` encodes
//! them, so rendered SQL and error messages hold exactly the text Python formatting would.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use super::routes::DEV_EXCEPTIONS;
use super::support::attr;
use crate::handling::raise_from;
use crate::py_kwargs;

const LIMIT_MIN: i64 = 1;
const LIMIT_MAX: i64 = 500;
const VALUE_PREVIEW_BYTES: i64 = 512;
const RECORD_VALUE_MAX_BYTES: i64 = 16_777_216;
const FACET_VALUE_LIMIT: i64 = 20;
const NEWEST_WINDOWS_SECONDS: [Option<i64>; 3] = [Some(3_600), Some(86_400), None];
const FACET_NEWEST_WINDOW_SECONDS: i64 = 86_400;
const PATH_MAX_DEPTH: usize = 8;
const PATH_SEGMENT_MAX_CHARS: usize = 64;
const PREVIEW_PATH_MAX_COUNT: usize = 4;
const CONTROL_CHARACTER_LIMIT: u8 = 32;
const HEADER_KEYS_COLUMN_NAME: &str = "kafka_header_keys";
const MODE_KIND_NEWEST: &[u8] = b"newest";
const MODE_KIND_TIME_RANGE: &[u8] = b"timeRange";
const MODE_KIND_OFFSET_RANGE: &[u8] = b"offsetRange";
const DEV_MODELS: &str = "sqltide.dev_server.models";

type Any<'py> = Bound<'py, PyAny>;
type Text = Vec<u8>;

/// The concatenation of byte-like parts.
macro_rules! cat {
    ($($part:expr),* $(,)?) => {{
        let mut joined: Vec<u8> = Vec::new();
        $(joined.extend_from_slice(AsRef::<[u8]>::as_ref(&$part));)*
        joined
    }};
}

/// One validated message browsing request compiled to SQL fragments.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct MessagesQueryDocument {
    newest: bool,
    conditions: Vec<Text>,
    limit: Py<PyAny>,
    limit_value: i64,
    cursor_condition: Option<Text>,
    preview_projections: Vec<Text>,
}

/// The text of a `str`.
fn raw(value: &Any<'_>) -> PyResult<Text> {
    let string: &Bound<'_, PyString> = value.cast::<PyString>()?;
    match string.to_str() {
        Ok(utf8) => Ok(utf8.as_bytes().to_vec()),
        Err(_) => Ok(string
            .call_method1("encode", ("utf-8", "surrogatepass"))?
            .cast_into::<PyBytes>()?
            .as_bytes()
            .to_vec()),
    }
}

/// The text of `str(value)`, which is what an f-string renders for these values.
fn shown(value: &Any<'_>) -> PyResult<Text> {
    raw(value.str()?.as_any())
}

/// A Python `str` from carried text.
fn text<'py>(py: Python<'py>, value: &[u8]) -> PyResult<Bound<'py, PyString>> {
    match std::str::from_utf8(value) {
        Ok(utf8) => Ok(PyString::new(py, utf8)),
        Err(_) => Ok(PyBytes::new(py, value)
            .call_method1("decode", ("utf-8", "surrogatepass"))?
            .cast_into::<PyString>()?),
    }
}

fn raised(py: Python<'_>, class: &str, message: &[u8]) -> PyResult<PyErr> {
    Ok(PyErr::from_value(attr(py, DEV_EXCEPTIONS, class)?.call1((text(py, message)?,))?))
}

fn invalid(py: Python<'_>, message: &[u8]) -> PyResult<PyErr> {
    raised(py, "MessageQueryValidationError", message)
}

fn unsupported_op(predicate: &Any<'_>, field: &str) -> PyResult<PyErr> {
    let op: Text = raw(&predicate.getattr("op")?)?;
    invalid(predicate.py(), &cat!("unsupported op '", op, "' for field '", field, "'"))
}

/// `int(str(value))`.
fn int<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "int")?.call1((value.str()?,))
}

fn optional_str<'py>(value: &Any<'py>) -> PyResult<Option<Bound<'py, PyString>>> {
    if value.is_none() { Ok(None) } else { Ok(Some(value.str()?)) }
}

/// Escape backslashes and single quotes for a ClickHouse string literal.
fn sql_literal(value: &[u8]) -> Text {
    let mut escaped: Text = Vec::with_capacity(value.len());
    for byte in value {
        match byte {
            b'\\' => escaped.extend_from_slice(b"\\\\"),
            b'\'' => escaped.extend_from_slice(b"\\'"),
            other => escaped.push(*other),
        }
    }
    escaped
}

fn items<'py>(value: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    value.try_iter()?.collect()
}

fn require_non_negative(value: &Any<'_>, field_name: &str) -> PyResult<()> {
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() || value.lt(0)? {
        return Err(invalid(value.py(), &cat!("field '", field_name, "' requires non-negative integers"))?);
    }
    Ok(())
}

/// Validate one wire request and compile it to escaped SQL fragments.
#[pyfunction]
pub(crate) fn parse_messages_document(request: &Any<'_>) -> PyResult<MessagesQueryDocument> {
    let py: Python<'_> = request.py();
    let kind: Text = raw(&request.getattr("mode")?.getattr("kind")?)?;
    if ![MODE_KIND_NEWEST, MODE_KIND_TIME_RANGE, MODE_KIND_OFFSET_RANGE].contains(&kind.as_slice()) {
        return Err(invalid(py, &cat!("unsupported mode kind '", kind, "'"))?);
    }
    let time_column_name: Text = raw(&request.getattr("timeColumn")?)?;
    let time_column: &str = match time_column_name.as_slice() {
        b"landed" => "_replay_landed_at",
        b"kafka" => "kafka_timestamp",
        _ => return Err(invalid(py, &cat!("unsupported time column '", time_column_name, "'"))?),
    };
    let preview_paths: Any<'_> = request.getattr("previewPaths")?;
    if preview_paths.len()? > PREVIEW_PATH_MAX_COUNT {
        return Err(invalid(py, &cat!("previewPaths allows at most ", PREVIEW_PATH_MAX_COUNT.to_string(), " paths"))?);
    }
    let mut conditions: Vec<Text> = Vec::new();
    for predicate in request.getattr("predicates")?.try_iter()? {
        conditions.push(predicate_condition(&predicate?)?);
    }
    conditions.extend(mode_conditions(&request.getattr("mode")?, time_column)?);
    // `min(max(limit, 1), 500)`, keeping the request's own object when it is in range.
    let mut limit: Any<'_> = request.getattr("limit")?;
    let floor: Any<'_> = LIMIT_MIN.into_pyobject(py)?.into_any();
    if floor.gt(&limit)? {
        limit = floor;
    }
    let ceiling: Any<'_> = LIMIT_MAX.into_pyobject(py)?.into_any();
    if ceiling.lt(&limit)? {
        limit = ceiling;
    }
    let cursor: Any<'_> = request.getattr("cursor")?;
    let cursor_condition: Option<Text> = if cursor.is_none() { None } else { Some(cursor_condition(&cursor)?) };
    let mut preview_projections: Vec<Text> = Vec::new();
    for (index, path) in preview_paths.try_iter()?.enumerate() {
        let path: Vec<Any<'_>> = items(&path?)?;
        preview_projections.push(cat!(json_string_expression(py, &path, "previewPaths")?, " AS preview_", index.to_string()));
    }
    Ok(MessagesQueryDocument {
        newest: kind == MODE_KIND_NEWEST,
        conditions,
        limit_value: limit.extract()?,
        limit: limit.unbind(),
        cursor_condition,
        preview_projections,
    })
}

/// Render the message list SELECT for one bounded window.
#[pyfunction]
#[pyo3(signature = (*, database, relation_name, document, window_seconds))]
pub(crate) fn build_messages_sql<'py>(
    database: &Any<'py>,
    relation_name: &Any<'py>,
    document: &Bound<'py, MessagesQueryDocument>,
    window_seconds: Option<&Any<'py>>,
) -> PyResult<Bound<'py, PyString>> {
    let py: Python<'py> = database.py();
    let window: Option<Text> = window_seconds.map(shown).transpose()?;
    let sql: Text = messages_sql(py, &shown(database)?, &shown(relation_name)?, document.get(), window)?;
    text(py, &sql)
}

fn messages_sql(
    py: Python<'_>,
    database: &[u8],
    relation_name: &[u8],
    document: &MessagesQueryDocument,
    window: Option<Text>,
) -> PyResult<Text> {
    let projections: Text = document.preview_projections.iter().flat_map(|projection| cat!(", ", projection)).collect();
    let where_clause: Text = where_clause(&document.conditions, window, document.cursor_condition.as_ref());
    Ok(cat!(
        "SELECT toString(_replay_landed_at) AS landed_at, ",
        "toString(kafka_timestamp) AS kafka_timestamp, ",
        "_replay_partition AS partition, _replay_offset AS offset, ",
        "kafka_key AS key, length(kafka_key) AS key_bytes, ",
        "substring(kafka_value, 1, ",
        VALUE_PREVIEW_BYTES.to_string(),
        ") AS value_preview, ",
        "length(kafka_value) AS value_bytes, ",
        "kafka_header_keys AS header_keys, kafka_header_values AS header_values",
        projections,
        " FROM `",
        database,
        "`.`",
        relation_name,
        "`",
        where_clause,
        " ORDER BY _replay_landed_at DESC, _replay_partition DESC, _replay_offset DESC LIMIT ",
        shown(document.limit.bind(py))?,
    ))
}

/// Return the newest matching messages, widening the window until satisfied.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_name, request))]
pub(crate) fn read_source_messages<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    relation_name: &Any<'py>,
    request: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let document: MessagesQueryDocument = parse_messages_document(request)?;
    let windows: Vec<Option<i64>> = if document.newest { NEWEST_WINDOWS_SECONDS.to_vec() } else { vec![None] };
    let mut rows: Any<'py> = PyTuple::empty(py).into_any();
    let mut used_window: Option<i64> = None;
    for window_seconds in windows {
        let sql: Text = messages_sql(
            py,
            &shown(database)?,
            &shown(relation_name)?,
            &document,
            window_seconds.map(|seconds| seconds.to_string().into_bytes()),
        )?;
        rows = connection.call_method1("query", (text(py, &sql)?,))?.call_method0("named_rows")?;
        used_window = window_seconds;
        if i64::try_from(rows.len()?).unwrap_or(i64::MAX) >= document.limit_value {
            break;
        }
    }
    let row_payloads: Bound<'py, PyList> = PyList::empty(py);
    for row in rows.try_iter()? {
        row_payloads.append(row_payload(&row?, document.preview_projections.len())?)?;
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("rows", &row_payloads)?;
    payload.set_item("nextCursor", next_cursor(&row_payloads, document.limit_value)?)?;
    payload.set_item("windowSeconds", used_window)?;
    payload.set_item("limit", document.limit.bind(py))?;
    Ok(payload)
}

/// Render the single-record primary-key lookup.
#[pyfunction]
#[pyo3(signature = (*, database, relation_name, partition, offset))]
pub(crate) fn build_record_sql<'py>(
    database: &Any<'py>,
    relation_name: &Any<'py>,
    partition: &Any<'py>,
    offset: &Any<'py>,
) -> PyResult<Bound<'py, PyString>> {
    require_non_negative(partition, "partition")?;
    require_non_negative(offset, "offset")?;
    let sql: Text = cat!(
        "SELECT toString(_replay_landed_at) AS landed_at, ",
        "toString(kafka_timestamp) AS kafka_timestamp, ",
        "_replay_partition AS partition, _replay_offset AS offset, ",
        "kafka_topic AS topic, kafka_key AS key, length(kafka_key) AS key_bytes, ",
        "substring(kafka_value, 1, ",
        RECORD_VALUE_MAX_BYTES.to_string(),
        ") AS value, ",
        "length(kafka_value) AS value_bytes, ",
        "kafka_header_keys AS header_keys, kafka_header_values AS header_values ",
        "FROM `",
        shown(database)?,
        "`.`",
        shown(relation_name)?,
        "` WHERE _replay_partition = ",
        shown(partition)?,
        " AND _replay_offset = ",
        shown(offset)?,
        " LIMIT 1",
    );
    text(database.py(), &sql)
}

/// Return one full message by replay coordinates, or None when absent.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_name, partition, offset))]
pub(crate) fn read_source_message_record<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    relation_name: &Any<'py>,
    partition: &Any<'py>,
    offset: &Any<'py>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let py: Python<'py> = connection.py();
    let sql: Bound<'py, PyString> = build_record_sql(database, relation_name, partition, offset)?;
    let rows: Any<'py> = connection.call_method1("query", (sql,))?.call_method0("named_rows")?;
    if !rows.is_truthy()? {
        return Ok(None);
    }
    let row: Any<'py> = rows.get_item(0)?;
    let value_bytes: Any<'py> = int(&row.get_item("value_bytes")?)?;
    let record: Bound<'py, PyDict> = PyDict::new(py);
    record.set_item("landedAt", row.get_item("landed_at")?.str()?)?;
    record.set_item("kafkaTimestamp", optional_str(&row.get_item("kafka_timestamp")?)?)?;
    record.set_item("partition", int(&row.get_item("partition")?)?)?;
    record.set_item("offset", int(&row.get_item("offset")?)?)?;
    record.set_item("topic", row.get_item("topic")?.str()?)?;
    record.set_item("key", row.get_item("key")?.str()?)?;
    record.set_item("keyBytes", int(&row.get_item("key_bytes")?)?)?;
    record.set_item("value", row.get_item("value")?.str()?)?;
    let truncated: bool = value_bytes.gt(RECORD_VALUE_MAX_BYTES)?;
    record.set_item("valueBytes", value_bytes)?;
    record.set_item("valueTruncated", truncated)?;
    record.set_item("headers", header_pairs(&row)?)?;
    Ok(Some(record))
}

/// Render the facet top-values and totals statements over one filtered window.
#[pyfunction]
#[pyo3(signature = (*, database, relation_name, document, facet_path))]
pub(crate) fn build_facets_sql<'py>(
    database: &Any<'py>,
    relation_name: &Any<'py>,
    document: &Bound<'py, MessagesQueryDocument>,
    facet_path: &Any<'py>,
) -> PyResult<(Bound<'py, PyString>, Bound<'py, PyString>)> {
    let py: Python<'py> = database.py();
    let (top_values, totals) = facets_sql(database, relation_name, document.get(), &items(facet_path)?)?;
    Ok((text(py, &top_values)?, text(py, &totals)?))
}

fn facets_sql(
    database: &Any<'_>,
    relation_name: &Any<'_>,
    document: &MessagesQueryDocument,
    facet_path: &[Any<'_>],
) -> PyResult<(Text, Text)> {
    let window: Option<Text> = document.newest.then(|| FACET_NEWEST_WINDOW_SECONDS.to_string().into_bytes());
    let inner: Text = cat!(
        "SELECT ",
        json_string_expression(database.py(), facet_path, "facetPath")?,
        " AS facet_value FROM `",
        shown(database)?,
        "`.`",
        shown(relation_name)?,
        "`",
        where_clause(&document.conditions, window, None),
    );
    let top_values: Text = cat!(
        "SELECT facet_value, count() AS n FROM (",
        inner,
        ") WHERE facet_value != '' GROUP BY facet_value ORDER BY n DESC, facet_value ASC LIMIT ",
        FACET_VALUE_LIMIT.to_string(),
    );
    let totals: Text = cat!("SELECT count() AS total, countIf(facet_value = '') AS null_count FROM (", inner, ")");
    Ok((top_values, totals))
}

/// Return top facet values with counts inside the current filter document.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_name, request))]
pub(crate) fn read_source_message_facets<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    relation_name: &Any<'py>,
    request: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let messages_request: Any<'py> = attr(py, DEV_MODELS, "MessagesQueryRequest")?.call(
        (),
        Some(&py_kwargs!(py;
            mode = request.getattr("mode")?,
            predicates = request.getattr("predicates")?,
            limit = request.getattr("limit")?,
            cursor = py.None(),
            timeColumn = request.getattr("timeColumn")?,
            previewPaths = PyList::empty(py),
        )?),
    )?;
    let document: MessagesQueryDocument = parse_messages_document(&messages_request)?;
    let facet_path: Vec<Any<'py>> = items(&request.getattr("facetPath")?)?;
    let (top_values_sql, totals_sql) = facets_sql(database, relation_name, &document, &facet_path)?;
    let value_rows: Any<'py> = connection.call_method1("query", (text(py, &top_values_sql)?,))?.call_method0("named_rows")?;
    let totals_rows: Any<'py> = connection.call_method1("query", (text(py, &totals_sql)?,))?.call_method0("named_rows")?;
    let zero: Any<'py> = 0_i64.into_pyobject(py)?.into_any();
    let (total_count, null_count) = if totals_rows.is_truthy()? {
        let first: Any<'py> = totals_rows.get_item(0)?;
        let total: Any<'py> = int(&first.get_item("total")?)?;
        (total, int(&first.get_item("null_count")?)?)
    } else {
        (zero.clone(), zero.clone())
    };
    let values: Bound<'py, PyList> = PyList::empty(py);
    for row in value_rows.try_iter()? {
        let row: Any<'py> = row?;
        let value: Bound<'py, PyDict> = PyDict::new(py);
        value.set_item("value", row.get_item("facet_value")?.str()?)?;
        value.set_item("count", int(&row.get_item("n")?)?)?;
        values.append(value)?;
    }
    let mut shown_count: Any<'py> = zero.clone();
    for row in value_rows.try_iter()? {
        shown_count = shown_count.add(int(&row?.get_item("n")?)?)?;
    }
    let mut other_count: Any<'py> = total_count.sub(&null_count)?.sub(&shown_count)?;
    if zero.gt(&other_count)? {
        other_count = zero;
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("values", values)?;
    payload.set_item("nullCount", null_count)?;
    payload.set_item("otherCount", other_count)?;
    payload.set_item("totalCount", total_count)?;
    payload.set_item("windowSeconds", document.newest.then_some(FACET_NEWEST_WINDOW_SECONDS))?;
    Ok(payload)
}

/// Fail with an actionable reset message when the raw table predates header capture.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_name))]
pub(crate) fn ensure_header_columns(connection: &Any<'_>, database: &Any<'_>, relation_name: &Any<'_>) -> PyResult<()> {
    let py: Python<'_> = connection.py();
    let query: Text = cat!(
        "SELECT count() AS present FROM system.columns WHERE database = '",
        sql_literal(&raw(database)?),
        "' AND table = '",
        sql_literal(&raw(relation_name)?),
        "' AND name = '",
        HEADER_KEYS_COLUMN_NAME,
        "'",
    );
    let rows: Any<'_> = connection.call_method1("query", (text(py, &query)?,))?.call_method0("named_rows")?;
    let present: bool = rows.is_truthy()? && int(&rows.get_item(0)?.get_item("present")?)?.gt(0)?;
    if !present {
        return Err(raised(
            py,
            "MessageSchemaError",
            &cat!(
                shown(database)?,
                ".",
                shown(relation_name)?,
                " was created before SQLTide captured Kafka headers; drop this pre-production raw table ",
                "(direct mode) or redeploy the source (virtual mode) so SQLTide can recreate it, then retry",
            ),
        )?);
    }
    Ok(())
}

fn predicate_condition(predicate: &Any<'_>) -> PyResult<Text> {
    let field: Text = raw(&predicate.getattr("field")?)?;
    match field.as_slice() {
        b"partition" => partition_condition(predicate),
        b"key" => key_condition(predicate),
        b"value" => value_condition(predicate),
        b"json" => json_condition(predicate),
        b"header" => header_condition(predicate),
        _ => Err(invalid(predicate.py(), &cat!("unsupported predicate field '", field, "'"))?),
    }
}

fn partition_condition(predicate: &Any<'_>) -> PyResult<Text> {
    if raw(&predicate.getattr("op")?)? != b"in" {
        return Err(unsupported_op(predicate, "partition")?);
    }
    let values: Any<'_> = predicate.getattr("values")?;
    if !values.is_truthy()? {
        return Err(invalid(predicate.py(), b"field 'partition' requires at least one value")?);
    }
    let values: Vec<Any<'_>> = items(&values)?;
    for value in &values {
        require_non_negative(value, "partition")?;
    }
    let rendered: Vec<Text> = values.iter().map(shown).collect::<PyResult<_>>()?;
    Ok(cat!("_replay_partition IN (", rendered.join(b", ".as_slice()), ")"))
}

fn key_condition(predicate: &Any<'_>) -> PyResult<Text> {
    let literal: Text = string_value(predicate, "key")?;
    match raw(&predicate.getattr("op")?)?.as_slice() {
        b"eq" => Ok(cat!("kafka_key = '", literal, "'")),
        b"contains" => Ok(cat!("position(kafka_key, '", literal, "') > 0")),
        b"prefix" => Ok(cat!("startsWith(kafka_key, '", literal, "')")),
        _ => Err(unsupported_op(predicate, "key")?),
    }
}

fn value_condition(predicate: &Any<'_>) -> PyResult<Text> {
    if raw(&predicate.getattr("op")?)? != b"contains" {
        return Err(unsupported_op(predicate, "value")?);
    }
    let literal: Text = string_value(predicate, "value")?;
    Ok(cat!("position(kafka_value, '", literal, "') > 0"))
}

fn json_condition(predicate: &Any<'_>) -> PyResult<Text> {
    let path: Vec<Any<'_>> = items(&predicate.getattr("path")?)?;
    let string_expression: Text = json_string_expression(predicate.py(), &path, "json")?;
    let op: Text = raw(&predicate.getattr("op")?)?;
    if op == b"exists" {
        return Ok(cat!("JSONHas(kafka_value, ", json_path_arguments(predicate.py(), &path, "json")?, ")"));
    }
    let numeric_operator: Option<&str> = match op.as_slice() {
        b"gt" => Some(">"),
        b"lt" => Some("<"),
        _ => None,
    };
    if let Some(operator) = numeric_operator {
        let number: Text = numeric_value(predicate, "json")?;
        let path_arguments: Text = json_path_arguments(predicate.py(), &path, "json")?;
        return Ok(cat!("JSONExtractFloat(kafka_value, ", path_arguments, ") ", operator, " ", number));
    }
    let literal: Text = string_value(predicate, "json")?;
    match op.as_slice() {
        b"eq" => Ok(cat!(string_expression, " = '", literal, "'")),
        b"ne" => Ok(cat!(string_expression, " != '", literal, "'")),
        b"contains" => Ok(cat!("position(", string_expression, ", '", literal, "') > 0")),
        _ => Err(unsupported_op(predicate, "json")?),
    }
}

fn header_condition(predicate: &Any<'_>) -> PyResult<Text> {
    let literal: Text = string_value(predicate, "header")?;
    match raw(&predicate.getattr("op")?)?.as_slice() {
        b"eq" => Ok(cat!("has(kafka_header_keys, '", literal, "')")),
        b"contains" => Ok(cat!("arrayExists(v -> position(v, '", literal, "') > 0, kafka_header_values)")),
        _ => Err(unsupported_op(predicate, "header")?),
    }
}

fn mode_conditions(mode: &Any<'_>, time_column: &str) -> PyResult<Vec<Text>> {
    let kind: Text = raw(&mode.getattr("kind")?)?;
    if kind == MODE_KIND_NEWEST {
        return Ok(Vec::new());
    }
    if kind == MODE_KIND_TIME_RANGE {
        return time_range_conditions(mode, time_column);
    }
    offset_range_conditions(mode)
}

fn time_range_conditions(mode: &Any<'_>, time_column: &str) -> PyResult<Vec<Text>> {
    let from_time: Any<'_> = mode.getattr("fromTime")?;
    let to_time: Any<'_> = mode.getattr("toTime")?;
    if from_time.is_none() && to_time.is_none() {
        return Err(invalid(mode.py(), b"timeRange mode requires fromTime or toTime")?);
    }
    let mut conditions: Vec<Text> = Vec::new();
    if !from_time.is_none() {
        conditions.push(cat!(time_column, " >= ", time_literal(&from_time, "fromTime")?));
    }
    if !to_time.is_none() {
        conditions.push(cat!(time_column, " <= ", time_literal(&to_time, "toTime")?));
    }
    Ok(conditions)
}

fn offset_range_conditions(mode: &Any<'_>) -> PyResult<Vec<Text>> {
    let partition: Any<'_> = mode.getattr("partition")?;
    if partition.is_none() {
        return Err(invalid(mode.py(), b"offsetRange mode requires exactly one partition")?);
    }
    require_non_negative(&partition, "partition")?;
    let mut conditions: Vec<Text> = vec![cat!("_replay_partition = ", shown(&partition)?)];
    let from_offset: Any<'_> = mode.getattr("fromOffset")?;
    if !from_offset.is_none() {
        require_non_negative(&from_offset, "fromOffset")?;
        conditions.push(cat!("_replay_offset >= ", shown(&from_offset)?));
    }
    let to_offset: Any<'_> = mode.getattr("toOffset")?;
    if !to_offset.is_none() {
        require_non_negative(&to_offset, "toOffset")?;
        conditions.push(cat!("_replay_offset <= ", shown(&to_offset)?));
    }
    if !from_offset.is_none() && !to_offset.is_none() && from_offset.gt(&to_offset)? {
        return Err(invalid(mode.py(), b"fromOffset must not exceed toOffset")?);
    }
    Ok(conditions)
}

fn cursor_condition(cursor: &Any<'_>) -> PyResult<Text> {
    let partition: Any<'_> = cursor.getattr("partition")?;
    require_non_negative(&partition, "cursor.partition")?;
    let offset: Any<'_> = cursor.getattr("offset")?;
    require_non_negative(&offset, "cursor.offset")?;
    let landed_literal: Text = time_literal(&cursor.getattr("landedAt")?, "cursor.landedAt")?;
    Ok(cat!(
        "(_replay_landed_at, _replay_partition, _replay_offset) < (",
        landed_literal,
        ", ",
        shown(&partition)?,
        ", ",
        shown(&offset)?,
        ")",
    ))
}

fn where_clause(conditions: &[Text], window: Option<Text>, cursor_condition: Option<&Text>) -> Text {
    let mut all: Vec<Text> = conditions.to_vec();
    if let Some(window_seconds) = window {
        all.push(cat!("_replay_landed_at >= now64(3) - INTERVAL ", window_seconds, " SECOND"));
    }
    if let Some(condition) = cursor_condition {
        all.push(condition.clone());
    }
    if all.is_empty() { Vec::new() } else { cat!(" WHERE ", all.join(b" AND ".as_slice())) }
}

fn json_string_expression(py: Python<'_>, path: &[Any<'_>], field_name: &str) -> PyResult<Text> {
    Ok(cat!("JSONExtractString(kafka_value, ", json_path_arguments(py, path, field_name)?, ")"))
}

fn json_path_arguments(py: Python<'_>, path: &[Any<'_>], field_name: &str) -> PyResult<Text> {
    if path.is_empty() {
        return Err(invalid(py, &cat!("field '", field_name, "' requires a JSON path"))?);
    }
    if path.len() > PATH_MAX_DEPTH {
        return Err(invalid(
            py,
            &cat!("field '", field_name, "' allows JSON paths of depth at most ", PATH_MAX_DEPTH.to_string()),
        )?);
    }
    let segments: Vec<Text> =
        path.iter().map(|segment| json_path_segment(segment, field_name)).collect::<PyResult<_>>()?;
    Ok(segments.join(b", ".as_slice()))
}

fn json_path_segment(segment: &Any<'_>, field_name: &str) -> PyResult<Text> {
    if segment.is_instance_of::<PyInt>() {
        require_non_negative(segment, field_name)?;
        return shown(segment);
    }
    let value: Text = raw(segment)?;
    // Code points, as `len(str)` counts them: every byte but UTF-8 continuation bytes.
    let characters: usize = value.iter().filter(|byte| (**byte & 0xC0) != 0x80).count();
    if characters == 0 || characters > PATH_SEGMENT_MAX_CHARS {
        return Err(invalid(
            segment.py(),
            &cat!(
                "field '",
                field_name,
                "' requires path segments of 1 to ",
                PATH_SEGMENT_MAX_CHARS.to_string(),
                " characters"
            ),
        )?);
    }
    let forbidden: bool =
        value.iter().any(|byte| matches!(byte, b'\'' | b'"' | b'`' | b'\\') || *byte < CONTROL_CHARACTER_LIMIT);
    if forbidden {
        return Err(invalid(
            segment.py(),
            &cat!(
                "field '",
                field_name,
                "' path segments cannot contain quotes, backslashes, backticks, or control characters"
            ),
        )?);
    }
    Ok(cat!("'", value, "'"))
}

fn string_value(predicate: &Any<'_>, field_name: &str) -> PyResult<Text> {
    let value: Any<'_> = predicate.getattr("value")?;
    if !value.is_instance_of::<PyString>() {
        return Err(invalid(predicate.py(), &cat!("field '", field_name, "' requires a string value"))?);
    }
    Ok(sql_literal(&raw(&value)?))
}

/// `str(float(value))` of a numeric, non-boolean predicate value.
fn numeric_value(predicate: &Any<'_>, field_name: &str) -> PyResult<Text> {
    let py: Python<'_> = predicate.py();
    let value: Any<'_> = predicate.getattr("value")?;
    if value.is_instance_of::<PyBool>() || !(value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>()) {
        return Err(invalid(py, &cat!("field '", field_name, "' requires a numeric value for gt/lt"))?);
    }
    shown(&attr(py, "builtins", "float")?.call1((value,))?)
}

/// `parseDateTime64BestEffort` of a timestamp `datetime.fromisoformat` accepts once a space
/// separator and a trailing `Z` are normalized.
fn time_literal(value: &Any<'_>, field_name: &str) -> PyResult<Text> {
    let py: Python<'_> = value.py();
    let parsed: PyResult<Any<'_>> = value
        .call_method1("replace", (" ", "T"))
        .and_then(|normalized| normalized.call_method1("removesuffix", ("Z",)))
        .and_then(|normalized| attr(py, "datetime", "datetime")?.call_method1("fromisoformat", (normalized,)));
    if let Err(error) = parsed {
        if !error.is_instance_of::<PyValueError>(py) {
            return Err(error);
        }
        let message: String = format!("field '{field_name}' requires an ISO 8601 timestamp");
        let replacement: Any<'_> = attr(py, DEV_EXCEPTIONS, "MessageQueryValidationError")?.call1((message,))?;
        let handled: Any<'_> = error.value(py).clone().into_any();
        return Err(raise_from(&replacement, &handled, Some(&handled))?);
    }
    Ok(cat!("parseDateTime64BestEffort('", sql_literal(&raw(value)?), "', 3)"))
}

fn row_payload<'py>(row: &Any<'py>, preview_count: usize) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = row.py();
    let value_bytes: Any<'py> = int(&row.get_item("value_bytes")?)?;
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("landedAt", row.get_item("landed_at")?.str()?)?;
    payload.set_item("kafkaTimestamp", optional_str(&row.get_item("kafka_timestamp")?)?)?;
    payload.set_item("partition", int(&row.get_item("partition")?)?)?;
    payload.set_item("offset", int(&row.get_item("offset")?)?)?;
    payload.set_item("key", row.get_item("key")?.str()?)?;
    payload.set_item("keyBytes", int(&row.get_item("key_bytes")?)?)?;
    payload.set_item("valuePreview", row.get_item("value_preview")?.str()?)?;
    let truncated: bool = value_bytes.gt(VALUE_PREVIEW_BYTES)?;
    payload.set_item("valueBytes", value_bytes)?;
    payload.set_item("valueTruncated", truncated)?;
    payload.set_item("headers", header_pairs(row)?)?;
    let previews: Bound<'py, PyList> = PyList::empty(py);
    for index in 0..preview_count {
        previews.append(row.get_item(format!("preview_{index}"))?.str()?)?;
    }
    payload.set_item("previewValues", previews)?;
    Ok(payload)
}

fn next_cursor<'py>(row_payloads: &Bound<'py, PyList>, limit: i64) -> PyResult<Option<Bound<'py, PyDict>>> {
    if i64::try_from(row_payloads.len()).unwrap_or(i64::MAX) < limit {
        return Ok(None);
    }
    let last: Any<'py> = row_payloads.get_item(row_payloads.len() - 1)?;
    let cursor: Bound<'py, PyDict> = PyDict::new(row_payloads.py());
    for key in ["landedAt", "partition", "offset"] {
        cursor.set_item(key, last.get_item(key)?)?;
    }
    Ok(Some(cursor))
}

fn header_pairs<'py>(row: &Any<'py>) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = row.py();
    let keys: Any<'py> = row.get_item("header_keys")?;
    let values: Any<'py> = row.get_item("header_values")?;
    let pairs: Bound<'py, PyList> = PyList::empty(py);
    let sequence = |value: &Any<'py>| value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>();
    if !sequence(&keys) || !sequence(&values) {
        return Ok(pairs);
    }
    for (key, value) in keys.try_iter()?.zip(values.try_iter()?) {
        pairs.append(PyList::new(py, [key?.str()?, value?.str()?])?)?;
    }
    Ok(pairs)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<MessagesQueryDocument>()?;
    module.add_function(wrap_pyfunction!(parse_messages_document, module)?)?;
    module.add_function(wrap_pyfunction!(build_messages_sql, module)?)?;
    module.add_function(wrap_pyfunction!(read_source_messages, module)?)?;
    module.add_function(wrap_pyfunction!(build_record_sql, module)?)?;
    module.add_function(wrap_pyfunction!(read_source_message_record, module)?)?;
    module.add_function(wrap_pyfunction!(build_facets_sql, module)?)?;
    module.add_function(wrap_pyfunction!(read_source_message_facets, module)?)?;
    module.add_function(wrap_pyfunction!(ensure_header_columns, module)?)
}
