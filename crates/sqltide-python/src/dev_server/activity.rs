//! Explainable model activity from ClickHouse system logs, independent of replay-lineage
//! columns.

use std::collections::HashMap;

use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::support::attr;
use crate::py_kwargs;

const QUERY_VIEWS_LOG: &str = "query_views_log";
const PART_LOG: &str = "part_log";
const SUCCESS_STATUS: &str = "QueryFinish";
const FAILURE_STATUSES: [&str; 2] = ["ExceptionBeforeStart", "ExceptionWhileProcessing"];
pub(crate) const ACTIVITY_WINDOW_SECONDS: i64 = 120;

type Any<'py> = Bound<'py, PyAny>;

/// Raw evidence collected for one target relation inside the active window.
#[derive(Clone)]
struct Observation<'py> {
    last_triggered_at: Option<String>,
    last_write_at: Option<String>,
    rows_written: Any<'py>,
    latest_status: Option<String>,
}

impl<'py> Observation<'py> {
    fn empty(py: Python<'py>) -> PyResult<Self> {
        Ok(Self { last_triggered_at: None, last_write_at: None, rows_written: 0_i64.into_pyobject(py)?.into_any(), latest_status: None })
    }
}

/// Insertion-ordered observations keyed by relation name, as a Python dict keeps them.
#[derive(Default)]
struct Observations<'py> {
    order: Vec<String>,
    by_name: HashMap<String, Observation<'py>>,
}

impl<'py> Observations<'py> {
    fn get(&self, name: &str) -> Option<&Observation<'py>> {
        self.by_name.get(name)
    }

    fn insert(&mut self, name: String, observation: Observation<'py>) {
        if !self.by_name.contains_key(&name) {
            self.order.push(name.clone());
        }
        self.by_name.insert(name, observation);
    }
}

fn text(value: &Any<'_>) -> PyResult<String> {
    Ok(value.str()?.to_str()?.to_owned())
}

fn int<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "int")?.call1((value.str()?,))
}

fn sql_literal(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\'', "\\'")
}

/// Inspect optional ClickHouse system-log availability.
#[pyfunction]
pub(crate) fn build_activity_capabilities_query() -> &'static str {
    "SELECT name FROM system.tables WHERE database = 'system' AND name IN ('part_log', 'query_views_log') ORDER BY name"
}

/// Read materialized-view execution evidence inside one bounded window.
#[pyfunction]
#[pyo3(signature = (*, database, window_seconds))]
pub(crate) fn build_query_views_activity_query(database: &str, window_seconds: i64) -> String {
    format!(
        "SELECT toString(event_time_microseconds) AS observed_at, view_target, written_rows, toString(status) AS status \
         FROM system.query_views_log WHERE event_time >= now() - INTERVAL {window_seconds} SECOND \
         AND view_type = 'Materialized' AND startsWith(replaceAll(view_target, '`', ''), '{}.') \
         ORDER BY event_time_microseconds",
        sql_literal(database)
    )
}

/// Read insert-created parts while excluding merges and mutations.
#[pyfunction]
#[pyo3(signature = (*, database, window_seconds))]
pub(crate) fn build_part_log_activity_query(database: &str, window_seconds: i64) -> String {
    format!(
        "SELECT toString(event_time_microseconds) AS observed_at, table, rows FROM system.part_log \
         WHERE database = '{}' AND event_type = 'NewPart' AND event_time >= now() - INTERVAL {window_seconds} SECOND \
         ORDER BY event_time_microseconds",
        sql_literal(database)
    )
}

/// Approximate fallback when trustworthy activity logs are unavailable.
#[pyfunction]
#[pyo3(signature = (*, database))]
pub(crate) fn build_parts_activity_query(database: &str) -> String {
    format!(
        "SELECT table, toString(max(modification_time)) AS last_modified_at FROM system.parts \
         WHERE active AND database = '{}' GROUP BY table ORDER BY table",
        sql_literal(database)
    )
}

fn rows<'py>(connection: &Any<'py>, query: String) -> PyResult<Vec<Any<'py>>> {
    connection.call_method1("query", (query,))?.getattr("rows")?.try_iter()?.collect()
}

fn unqualified_relation(value: &str) -> String {
    let unquoted: String = value.replace('`', "");
    match unquoted.rsplit_once('.') {
        Some((_, name)) => name.to_owned(),
        None => unquoted,
    }
}

fn query_view_observations<'py>(connection: &Any<'py>, database: &str, window_seconds: i64) -> PyResult<Observations<'py>> {
    let py: Python<'py> = connection.py();
    let mut observations: Observations<'py> = Observations::default();
    for row in rows(connection, build_query_views_activity_query(database, window_seconds))? {
        let event_time: String = text(&row.get_item(0)?)?;
        let relation_name: String = unqualified_relation(&text(&row.get_item(1)?)?);
        let written_rows: Any<'py> = int(&row.get_item(2)?)?;
        let status: String = text(&row.get_item(3)?)?;
        let previous: Observation<'py> = match observations.get(&relation_name) {
            Some(previous) => previous.clone(),
            None => Observation::empty(py)?,
        };
        let succeeded: bool = status == SUCCESS_STATUS;
        let wrote: bool = written_rows.gt(0)?;
        let observation = Observation {
            last_triggered_at: Some(event_time.clone()),
            last_write_at: if succeeded && wrote { Some(event_time) } else { previous.last_write_at },
            rows_written: if succeeded { previous.rows_written.add(&written_rows)? } else { previous.rows_written },
            latest_status: Some(status),
        };
        observations.insert(relation_name, observation);
    }
    Ok(observations)
}

fn part_log_observations<'py>(connection: &Any<'py>, database: &str, window_seconds: i64) -> PyResult<Observations<'py>> {
    let py: Python<'py> = connection.py();
    let mut observations: Observations<'py> = Observations::default();
    for row in rows(connection, build_part_log_activity_query(database, window_seconds))? {
        let event_time: String = text(&row.get_item(0)?)?;
        let relation_name: String = text(&row.get_item(1)?)?;
        let written_rows: Any<'py> = int(&row.get_item(2)?)?;
        let previous: Observation<'py> = match observations.get(&relation_name) {
            Some(previous) => previous.clone(),
            None => Observation::empty(py)?,
        };
        let observation = Observation {
            last_triggered_at: Some(event_time.clone()),
            last_write_at: if written_rows.gt(0)? { Some(event_time) } else { previous.last_write_at },
            rows_written: previous.rows_written.add(&written_rows)?,
            latest_status: Some(SUCCESS_STATUS.to_owned()),
        };
        observations.insert(relation_name, observation);
    }
    Ok(observations)
}

fn parts_observations<'py>(connection: &Any<'py>, database: &str) -> PyResult<Observations<'py>> {
    let py: Python<'py> = connection.py();
    let mut observations: Observations<'py> = Observations::default();
    for row in rows(connection, build_parts_activity_query(database))? {
        let name: String = text(&row.get_item(0)?)?;
        let observation = Observation {
            last_triggered_at: None,
            last_write_at: Some(text(&row.get_item(1)?)?),
            rows_written: 0_i64.into_pyobject(py)?.into_any(),
            latest_status: Some(SUCCESS_STATUS.to_owned()),
        };
        observations.insert(name, observation);
    }
    Ok(observations)
}

#[allow(clippy::too_many_arguments)]
fn activity_payload<'py>(
    py: Python<'py>,
    state: &str,
    source: &str,
    source_available: bool,
    approximate: bool,
    window_seconds: i64,
    detail: String,
    observation: Option<&Observation<'py>>,
) -> PyResult<Bound<'py, PyDict>> {
    let empty: Observation<'py> = Observation::empty(py)?;
    let observation: &Observation<'py> = observation.unwrap_or(&empty);
    py_kwargs!(py;
        state = state,
        source = source,
        sourceAvailable = source_available,
        approximate = approximate,
        lastTriggeredAt = observation.last_triggered_at.clone(),
        lastWriteAt = observation.last_write_at.clone(),
        rowsWritten = &observation.rows_written,
        windowSeconds = window_seconds,
        detail = detail,
    )
}

fn write_activity_payload<'py>(
    py: Python<'py>,
    observation: &Observation<'py>,
    source: &str,
    window_seconds: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let moving: bool = observation.rows_written.gt(0)?;
    let detail: String = if moving {
        format!("{} rows were written in the last {window_seconds}s.", observation.rows_written.str()?)
    } else {
        format!("The model was triggered but wrote no rows in the last {window_seconds}s.")
    };
    activity_payload(py, if moving { "moving" } else { "idle" }, source, true, false, window_seconds, detail, Some(observation))
}

fn view_activity_payload<'py>(py: Python<'py>, observation: &Observation<'py>, window_seconds: i64) -> PyResult<Bound<'py, PyDict>> {
    if observation.latest_status.as_deref().is_some_and(|status| FAILURE_STATUSES.contains(&status)) {
        return activity_payload(
            py,
            "stalled",
            QUERY_VIEWS_LOG,
            true,
            false,
            window_seconds,
            "The latest materialized-view execution failed.".to_owned(),
            Some(observation),
        );
    }
    write_activity_payload(py, observation, QUERY_VIEWS_LOG, window_seconds)
}

/// `datetime.fromisoformat` of a warehouse timestamp with its space separator replaced; `None`
/// when it does not parse.
pub(crate) fn parse_warehouse_timestamp<'py>(py: Python<'py>, value: &str) -> PyResult<Option<Any<'py>>> {
    let datetime = attr(py, "datetime", "datetime")?;
    match datetime.call_method1("fromisoformat", (value.replace(' ', "T"),)) {
        Ok(parsed) => Ok(Some(parsed)),
        Err(error) if error.is_instance_of::<PyValueError>(py) => Ok(None),
        Err(error) => Err(error),
    }
}

fn age_seconds(py: Python<'_>, timestamp: Option<&str>, captured_at: &str) -> PyResult<Option<f64>> {
    let Some(timestamp) = timestamp else {
        return Ok(None);
    };
    let Some(observed_at) = parse_warehouse_timestamp(py, timestamp)? else {
        return Ok(None);
    };
    let Some(now_at) = parse_warehouse_timestamp(py, captured_at)? else {
        return Ok(None);
    };
    let seconds: f64 = now_at.sub(&observed_at)?.call_method0("total_seconds")?.extract()?;
    Ok(Some(if 0.0 > seconds { 0.0 } else { seconds }))
}

fn approximate_activity_payload<'py>(
    py: Python<'py>,
    observation: &Observation<'py>,
    captured_at: &str,
    window_seconds: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let age: Option<f64> = age_seconds(py, observation.last_write_at.as_deref(), captured_at)?;
    let moving: bool = age.is_some_and(|age| age <= window_seconds as f64);
    let detail: &str = if moving {
        "A table part changed recently; this may include background merges."
    } else {
        "No recent table-part modification was observed."
    };
    activity_payload(py, if moving { "moving" } else { "idle" }, "system_parts", true, true, window_seconds, detail.to_owned(), Some(observation))
}

type Evidence<'py> = (Vec<String>, Observations<'py>, Observations<'py>, Observations<'py>);

fn collect_evidence<'py>(connection: &Any<'py>, database: &str, window_seconds: i64) -> PyResult<Evidence<'py>> {
    let mut capabilities: Vec<String> = Vec::new();
    for row in rows(connection, build_activity_capabilities_query().to_owned())? {
        let name: String = text(&row.get_item(0)?)?;
        if !capabilities.contains(&name) {
            capabilities.push(name);
        }
    }
    let has = |name: &str| capabilities.iter().any(|item| item == name);
    let views: Observations<'py> =
        if has(QUERY_VIEWS_LOG) { query_view_observations(connection, database, window_seconds)? } else { Observations::default() };
    let parts: Observations<'py> =
        if has(PART_LOG) { part_log_observations(connection, database, window_seconds)? } else { Observations::default() };
    let approximate: Observations<'py> =
        if capabilities.is_empty() { parts_observations(connection, database)? } else { Observations::default() };
    Ok((capabilities, views, parts, approximate))
}

/// Return capability-aware activity evidence for every model relation.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_names, captured_at, active_bindings = None, window_seconds = ACTIVITY_WINDOW_SECONDS))]
pub(crate) fn read_model_activity<'py>(
    py: Python<'py>,
    connection: Any<'py>,
    database: &str,
    relation_names: Vec<String>,
    captured_at: &str,
    active_bindings: Option<Any<'py>>,
    window_seconds: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let unknown: Bound<'py, PyDict> = PyDict::new(py);
    for relation_name in &relation_names {
        unknown.set_item(
            relation_name,
            activity_payload(
                py,
                "unknown",
                "unavailable",
                false,
                false,
                window_seconds,
                "ClickHouse activity telemetry is unavailable.".to_owned(),
                None,
            )?,
        )?;
    }
    let (capabilities, views, parts, approximate) = match collect_evidence(&connection, database, window_seconds) {
        Ok(evidence) => evidence,
        Err(error)
            if error.is_instance_of::<PyKeyError>(py)
                || error.is_instance_of::<PyTypeError>(py)
                || error.is_instance_of::<PyValueError>(py)
                || error.is_instance(py, &attr(py, "sqltide.adapter.exceptions", "AdapterError")?) =>
        {
            return Ok(unknown);
        }
        Err(error) => return Err(error),
    };
    let physical_by_logical: Bound<'py, PyDict> = match active_bindings {
        None => PyDict::new(py),
        Some(bindings) => attr(py, "builtins", "dict")?.call1((bindings,))?.cast_into::<PyDict>()?,
    };
    let payloads: Bound<'py, PyDict> = PyDict::new(py);
    for relation_name in &relation_names {
        // A non-string physical name matches no observation, as a str-keyed dict lookup would.
        let evidence: Option<String> = match physical_by_logical.get_item(relation_name)? {
            None => Some(relation_name.clone()),
            Some(physical) => physical.extract().ok(),
        };
        let find = |observations: &Observations<'py>| -> Option<Observation<'py>> {
            evidence.as_deref().and_then(|name| observations.get(name)).or_else(|| observations.get(relation_name)).cloned()
        };
        let payload: Bound<'py, PyDict> = if let Some(observation) = find(&views) {
            view_activity_payload(py, &observation, window_seconds)?
        } else if let Some(observation) = find(&parts) {
            write_activity_payload(py, &observation, PART_LOG, window_seconds)?
        } else if let Some(observation) = find(&approximate) {
            approximate_activity_payload(py, &observation, captured_at, window_seconds)?
        } else if !capabilities.is_empty() {
            let source: &str = if capabilities.iter().any(|item| item == QUERY_VIEWS_LOG) { QUERY_VIEWS_LOG } else { PART_LOG };
            activity_payload(
                py,
                "idle",
                source,
                true,
                false,
                window_seconds,
                format!("No model writes were observed in the last {window_seconds}s."),
                None,
            )?
        } else {
            unknown.get_item(relation_name)?.expect("every relation has an unknown payload").cast_into::<PyDict>()?
        };
        payloads.set_item(relation_name, payload)?;
    }
    Ok(payloads)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_activity_capabilities_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_query_views_activity_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_part_log_activity_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_parts_activity_query, module)?)?;
    module.add_function(wrap_pyfunction!(read_model_activity, module)?)
}
