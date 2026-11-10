//! Run history, liveness and live statement progress derived from append-only warehouse facts.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySlice, PyString};
use sqltide_discovery::config::{
    DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS, RUN_UNRESPONSIVE_AFTER_SECONDS,
};

use super::support::{attr, unpack};
use crate::py_kwargs;

const DEFAULT_PRESUMED_FAILED: i64 = DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS as i64;
const DEFAULT_RUNS_LIMIT: i64 = 100;
const SCHEDULED_RUNS_LIMIT: i64 = 25;
const RUN_EVENT_PAGE_SIZE: usize = 500;
const RUN_EVENT_WINDOW_SIZE: i64 = 400;
const RUN_STARTED: &str = "run_started";
const RUN_COMPLETED: &str = "run_completed";
const STATEMENT_STARTED: &str = "statement_started";
const STATEMENT_COMPLETED: &str = "statement_completed";
const AUDIT_STARTED: &str = "audit_started";
const AUDIT_COMPLETED: &str = "audit_completed";
const CANCELLATION_COMPLETED: &str = "cancellation_completed";
const CANCELLATION_FAILED: &str = "cancellation_failed";
const INVOCATIONS_TABLE: &str = "_sqltide_invocations";
const RUN_EVENTS_TABLE: &str = "_sqltide_run_events";
const RUN_STATEMENTS_TABLE: &str = "_sqltide_run_statements";
const EVENT_COLUMNS: &str = "invocation_id, sequence, toString(emitted_at) AS emitted_at, event_kind, step_id, phase, payload_json";

type Any<'py> = Bound<'py, PyAny>;

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

/// `int(str(value))`.
fn int<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "int")?.call1((value.str()?,))
}

fn text<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(value.str()?.into_any())
}

/// `mapping.get(key)`.
fn get<'py>(mapping: &Any<'py>, key: &str) -> PyResult<Any<'py>> {
    mapping.call_method1("get", (key,))
}

/// `mapping.get(key, default)`.
fn get_or<'py, T: IntoPyObject<'py>>(
    mapping: &Any<'py>,
    key: &str,
    default: T,
) -> PyResult<Any<'py>> {
    mapping.call_method1("get", (key, default))
}

fn is_mapping(value: &Any<'_>) -> PyResult<bool> {
    value.is_instance(&attr(value.py(), "collections.abc", "Mapping")?)
}

fn sql_literal(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\'', "\\'")
}

/// Python's `max(a, b)` and `min(a, b)` on floats, which keep the first argument on ties and
/// on NaN comparisons.
fn py_max(first: f64, second: f64) -> f64 {
    if second > first { second } else { first }
}

fn py_min(first: f64, second: f64) -> f64 {
    if second < first { second } else { first }
}

fn named_rows<'py>(connection: &Any<'py>, query: String) -> PyResult<Vec<Any<'py>>> {
    connection
        .call_method1("query", (query,))?
        .call_method0("named_rows")?
        .try_iter()?
        .collect()
}

fn table_exists(connection: &Any<'_>, database: &str, table: &str) -> PyResult<bool> {
    let rows = named_rows(
        connection,
        format!(
            "SELECT count() AS present FROM system.tables WHERE database = '{database}' AND name = '{table}'"
        ),
    )?;
    match rows.first() {
        None => Ok(false),
        Some(row) => int(&row.get_item("present")?)?.gt(0),
    }
}

fn status_member<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, "sqltide.dev_server.types", "RunPresentationStatus")?.getattr(name)
}

/// `datetime.fromisoformat(value.replace(" ", "T").removesuffix("Z")).replace(tzinfo=UTC)`.
fn parse_timestamp<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    let normalized = value
        .call_method1("replace", (" ", "T"))?
        .call_method1("removesuffix", ("Z",))?;
    let parsed = attr(py, "datetime", "datetime")?.call_method1("fromisoformat", (normalized,))?;
    parsed.call_method(
        "replace",
        (),
        Some(&py_kwargs!(py; tzinfo = attr(py, "datetime", "UTC")?)?),
    )
}

/// `max(int((later - earlier).total_seconds()), 0)`.
fn whole_seconds<'py>(later: &Any<'py>, earlier: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = later.py();
    let seconds = attr(py, "builtins", "int")?
        .call1((later.sub(earlier)?.call_method0("total_seconds")?,))?;
    if seconds.lt(0)? {
        Ok(0_i64.into_pyobject(py)?.into_any())
    } else {
        Ok(seconds)
    }
}

fn age_seconds<'py>(timestamp: &Any<'py>, warehouse_now: &Any<'py>) -> PyResult<Any<'py>> {
    whole_seconds(warehouse_now, &parse_timestamp(timestamp)?)
}

fn derive_status<'py>(
    terminal_outcome: &Any<'py>,
    completed_event_outcome: &Any<'py>,
    last_signal_at: &Any<'py>,
    warehouse_now: &Any<'py>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = terminal_outcome.py();
    let statuses: Bound<'py, PyDict> = PyDict::new(py);
    for (outcome, name) in [
        ("succeeded", "SUCCEEDED"),
        ("failed", "FAILED"),
        ("cancelled", "CANCELLED"),
    ] {
        statuses.set_item(outcome, status_member(py, name)?)?;
    }
    for outcome in [terminal_outcome, completed_event_outcome] {
        if statuses.contains(outcome)? {
            return statuses.as_any().get_item(outcome);
        }
    }
    let age: f64 = warehouse_now
        .sub(last_signal_at)?
        .call_method0("total_seconds")?
        .extract()?;
    let signal_age: f64 = py_max(age, 0.0);
    if signal_age < RUN_UNRESPONSIVE_AFTER_SECONDS as f64 {
        return status_member(py, "RUNNING");
    }
    if signal_age < presumed_failed_after_seconds as f64 {
        return status_member(py, "UNRESPONSIVE");
    }
    status_member(py, "PRESUMED_FAILED")
}

/// Derive one reversible presentation status from durable facts.
#[pyfunction]
#[pyo3(signature = (*, terminal_outcome, completed_event_outcome, last_signal_at, warehouse_now, presumed_failed_after_seconds = DEFAULT_PRESUMED_FAILED))]
pub(crate) fn derive_run_status<'py>(
    terminal_outcome: Any<'py>,
    completed_event_outcome: Any<'py>,
    last_signal_at: Any<'py>,
    warehouse_now: Any<'py>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    derive_status(
        &terminal_outcome,
        &completed_event_outcome,
        &last_signal_at,
        &warehouse_now,
        presumed_failed_after_seconds,
    )
}

/// Return terminal duration or current elapsed time for an active event stream.
#[pyfunction]
#[pyo3(signature = (*, started_at, completed_at, warehouse_now))]
pub(crate) fn derive_run_duration_ms<'py>(
    started_at: Any<'py>,
    completed_at: Any<'py>,
    warehouse_now: Any<'py>,
) -> PyResult<Any<'py>> {
    let end: Any<'py> = if completed_at.is_none() {
        warehouse_now
    } else {
        parse_timestamp(&completed_at)?
    };
    whole_seconds(&end, &parse_timestamp(&started_at)?)?.mul(1000)
}

/// `json.loads(str(payload_json))` when it is a JSON object, otherwise `{}`.
fn parsed_event_payload<'py>(payload_json: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = payload_json.py();
    match attr(py, "json", "loads")?.call1((payload_json.str()?,)) {
        Ok(parsed) => Ok(parsed
            .cast_into::<PyDict>()
            .unwrap_or_else(|_| PyDict::new(py))),
        Err(error) if error.is_instance_of::<PyValueError>(py) => Ok(PyDict::new(py)),
        Err(error) => Err(error),
    }
}

fn audit_counts<'py>(
    py: Python<'py>,
    counts: [Any<'py>; 4],
    total: Any<'py>,
) -> PyResult<Any<'py>> {
    let [passed, warning, failed, error] = counts;
    Ok(py_kwargs!(py; passed = passed, warning = warning, failed = failed, error = error, total = total)?.into_any())
}

/// The durable outcome counts of a completed scheduled audit cycle.
#[pyfunction]
#[pyo3(signature = (*, command, mode, summary_json))]
pub(crate) fn terminal_audit_summary<'py>(
    command: Any<'py>,
    mode: Any<'py>,
    summary_json: Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = command.py();
    if command.ne("audit")? || mode.ne("scheduled")? {
        return Ok(none(py));
    }
    let summary: Bound<'py, PyDict> = parsed_event_payload(&summary_json)?;
    if !summary.contains("scheduled_count")? && !summary.contains("error_count")? {
        return Ok(none(py));
    }
    let summary: Any<'py> = summary.into_any();
    let zero = || -> PyResult<Any<'py>> { Ok(0_i64.into_pyobject(py)?.into_any()) };
    if !get(&summary, "error_count")?.is_none() {
        let error_count = int(&summary.get_item("error_count")?)?;
        return audit_counts(
            py,
            [zero()?, zero()?, zero()?, error_count.clone()],
            error_count,
        );
    }
    let total = int(&get_or(&summary, "scheduled_count", 0)?)?;
    let warning = int(&get_or(&summary, "warning_failure_count", 0)?)?;
    let failed = int(&get_or(&summary, "error_failure_count", 0)?)?;
    let error = int(&get_or(&summary, "execution_error_count", 0)?)?;
    let remaining = total.sub(&warning)?.sub(&failed)?.sub(&error)?;
    let passed = if remaining.lt(0)? { zero()? } else { remaining };
    audit_counts(py, [passed, warning, failed, error], total)
}

#[derive(Default)]
struct TerminalFilter<'a> {
    invocation_id: Option<&'a str>,
    limit: Option<i64>,
    scheduled: Option<bool>,
    table_exists: Option<bool>,
}

fn terminal_runs<'py>(
    connection: &Any<'py>,
    database: &str,
    filter: TerminalFilter<'_>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let runs: Bound<'py, PyDict> = PyDict::new(py);
    let present: bool = match filter.table_exists {
        Some(present) => present,
        None => table_exists(connection, database, INVOCATIONS_TABLE)?,
    };
    if !present {
        return Ok(runs);
    }
    let mut clauses: Vec<String> = Vec::new();
    if let Some(invocation_id) = filter.invocation_id {
        clauses.push(format!("invocation_id = '{}'", sql_literal(invocation_id)));
    }
    match filter.scheduled {
        Some(true) => clauses.push("command = 'audit' AND mode = 'scheduled'".to_owned()),
        Some(false) => {
            clauses.push("NOT (command = 'audit' AND coalesce(mode, '') = 'scheduled')".to_owned())
        }
        None => {}
    }
    let where_clause: String = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    let limit_clause: String = filter
        .limit
        .map(|limit| format!(" LIMIT {limit}"))
        .unwrap_or_default();
    let query: String = format!(
        "SELECT invocation_id, project_identity, command, mode, outcome, exit_code, \
         toString(started_at) AS started_at, toString(completed_at) AS completed_at, \
         duration_ms, selected_node_count, error_message, summary_json, tool_version \
         FROM `{database}`.`{INVOCATIONS_TABLE}`{where_clause} ORDER BY started_at DESC{limit_clause}"
    );
    for row in named_rows(connection, query)? {
        let invocation_id = text(&row.get_item("invocation_id")?)?;
        let field = |name: &str| row.get_item(name);
        let run: Bound<'py, PyDict> = PyDict::new(py);
        run.set_item("invocationId", &invocation_id)?;
        run.set_item("projectIdentity", text(&field("project_identity")?)?)?;
        run.set_item("command", text(&field("command")?)?)?;
        run.set_item("displayCommand", none(py))?;
        run.set_item("mode", text(&field("mode")?)?)?;
        run.set_item("status", text(&field("outcome")?)?)?;
        run.set_item("outcome", text(&field("outcome")?)?)?;
        run.set_item("exitCode", int(&field("exit_code")?)?)?;
        run.set_item("startedAt", text(&field("started_at")?)?)?;
        run.set_item("completedAt", text(&field("completed_at")?)?)?;
        run.set_item("lastSignalAt", text(&field("completed_at")?)?)?;
        run.set_item("lastSignalAgeSeconds", 0)?;
        run.set_item("durationMs", int(&field("duration_ms")?)?)?;
        run.set_item("selectedNodeCount", int(&field("selected_node_count")?)?)?;
        run.set_item("errorMessage", field("error_message")?)?;
        run.set_item("summary", parsed_event_payload(&field("summary_json")?)?)?;
        run.set_item(
            "auditSummary",
            terminal_audit_summary(
                text(&field("command")?)?,
                text(&field("mode")?)?,
                field("summary_json")?,
            )?,
        )?;
        run.set_item("toolVersion", text(&field("tool_version")?)?)?;
        for name in [
            "lastActivity",
            "completedOperationCount",
            "totalStatements",
            "currentStep",
        ] {
            run.set_item(name, none(py))?;
        }
        runs.set_item(invocation_id, run)?;
    }
    Ok(runs)
}

/// Event rows grouped by invocation in row order, each event merged with its JSON payload.
fn streams_from_rows<'py>(py: Python<'py>, rows: Vec<Any<'py>>) -> PyResult<Bound<'py, PyDict>> {
    let streams: Bound<'py, PyDict> = PyDict::new(py);
    for row in rows {
        let invocation_id = text(&row.get_item("invocation_id")?)?;
        let stream = match streams.get_item(&invocation_id)? {
            Some(stream) => stream,
            None => {
                let stream = PyList::empty(py).into_any();
                streams.set_item(&invocation_id, &stream)?;
                stream
            }
        };
        let event: Bound<'py, PyDict> = PyDict::new(py);
        event.set_item("sequence", int(&row.get_item("sequence")?)?)?;
        event.set_item("emittedAt", text(&row.get_item("emitted_at")?)?)?;
        event.set_item("event", text(&row.get_item("event_kind")?)?)?;
        event.set_item("stepId", row.get_item("step_id")?)?;
        event.set_item("phase", row.get_item("phase")?)?;
        event.update(parsed_event_payload(&row.get_item("payload_json")?)?.as_mapping())?;
        stream.call_method1("append", (event,))?;
    }
    Ok(streams)
}

fn run_started_streams<'py>(
    connection: &Any<'py>,
    database: &str,
    invocation_ids: &[String],
    table_exists: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    if invocation_ids.is_empty() || !table_exists {
        return Ok(PyDict::new(py));
    }
    let literals: String = invocation_ids
        .iter()
        .map(|id| format!("'{}'", sql_literal(id)))
        .collect::<Vec<_>>()
        .join(", ");
    let query: String = format!(
        "SELECT {EVENT_COLUMNS} FROM `{database}`.`{RUN_EVENTS_TABLE}` \
         WHERE invocation_id IN ({literals}) AND event_kind = '{RUN_STARTED}' ORDER BY invocation_id, sequence"
    );
    streams_from_rows(py, named_rows(connection, query)?)
}

#[derive(Default)]
struct EventFilter<'a, 'py> {
    invocation_id: Option<&'a str>,
    after: Option<&'a Any<'py>>,
    row_limit: Option<usize>,
    recent_limit: Option<i64>,
    active_only: bool,
    exclude_terminal_invocations: bool,
    table_exists: Option<bool>,
}

fn event_streams<'py>(
    connection: &Any<'py>,
    database: &str,
    filter: EventFilter<'_, 'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let present: bool = match filter.table_exists {
        Some(present) => present,
        None => table_exists(connection, database, RUN_EVENTS_TABLE)?,
    };
    if !present {
        return Ok(PyDict::new(py));
    }
    let mut clauses: Vec<String> = Vec::new();
    if let Some(invocation_id) = filter.invocation_id {
        clauses.push(format!("invocation_id = '{}'", sql_literal(invocation_id)));
    }
    if let Some(after) = filter.after
        && after.is_truthy()?
    {
        clauses.push(format!("sequence > {}", after.str()?));
    }
    if filter.active_only {
        clauses.push(format!(
            "invocation_id IN (SELECT invocation_id FROM `{database}`.`{RUN_EVENTS_TABLE}` \
             GROUP BY invocation_id HAVING countIf(event_kind = 'run_started') > 0 \
             AND countIf(event_kind = 'run_completed') = 0)"
        ));
    }
    if filter.exclude_terminal_invocations {
        clauses.push(format!(
            "invocation_id NOT IN (SELECT invocation_id FROM `{database}`.`{INVOCATIONS_TABLE}`)"
        ));
    }
    let where_clause: String = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    let query: String = match filter.recent_limit {
        None => {
            let limit_clause: String = filter
                .row_limit
                .map(|limit| format!(" LIMIT {limit}"))
                .unwrap_or_default();
            format!(
                "SELECT {EVENT_COLUMNS} FROM `{database}`.`{RUN_EVENTS_TABLE}`{where_clause} \
                 ORDER BY invocation_id, sequence{limit_clause}"
            )
        }
        Some(recent_limit) => format!(
            "SELECT {EVENT_COLUMNS} FROM (SELECT {EVENT_COLUMNS}, \
             row_number() OVER (PARTITION BY invocation_id ORDER BY sequence DESC) AS recency \
             FROM `{database}`.`{RUN_EVENTS_TABLE}`{where_clause}) \
             WHERE recency <= {recent_limit} OR event_kind = 'run_started' ORDER BY invocation_id, sequence"
        ),
    };
    streams_from_rows(py, named_rows(connection, query)?)
}

fn is_kind(event: &Any<'_>, kind: &str) -> PyResult<bool> {
    event.get_item("event")?.eq(kind)
}

fn first_of<'py>(events: &[Any<'py>], kind: &str) -> PyResult<Option<Any<'py>>> {
    for event in events {
        if is_kind(event, kind)? {
            return Ok(Some(event.clone()));
        }
    }
    Ok(None)
}

fn last_of<'py>(events: &[Any<'py>], kind: &str) -> PyResult<Option<Any<'py>>> {
    for event in events.iter().rev() {
        if is_kind(event, kind)? {
            return Ok(Some(event.clone()));
        }
    }
    Ok(None)
}

fn bump(counts: &Bound<'_, PyDict>, key: &Any<'_>) -> PyResult<()> {
    let current: i64 = match counts.get_item(key)? {
        Some(count) => count.extract()?,
        None => 0,
    };
    counts.set_item(key, current + 1)
}

/// Take one completion for `key`; `false` when none is left to pair with a start.
fn consume(counts: &Bound<'_, PyDict>, key: &Any<'_>) -> PyResult<bool> {
    let Some(count) = counts.get_item(key)? else {
        return Ok(false);
    };
    match count.extract::<i64>()? {
        0 => Ok(false),
        1 => counts.del_item(key).map(|()| true),
        count => counts.set_item(key, count - 1).map(|()| true),
    }
}

/// The step of the latest statement or audit that started without a matching completion.
fn active_step<'py>(py: Python<'py>, events: &[Any<'py>]) -> PyResult<Any<'py>> {
    let statements: Bound<'py, PyDict> = PyDict::new(py);
    let audits: Bound<'py, PyDict> = PyDict::new(py);
    for event in events.iter().rev() {
        let kind = text(&event.get_item("event")?)?;
        let sequence_present: bool = !get(event, "statementSequence")?.is_none();
        if kind.eq(STATEMENT_COMPLETED)? && sequence_present {
            bump(&statements, &int(&event.get_item("statementSequence")?)?)?;
        } else if kind.eq(AUDIT_COMPLETED)? && !get(event, "stepId")?.is_none() {
            bump(&audits, &text(&event.get_item("stepId")?)?)?;
        } else if kind.eq(STATEMENT_STARTED)? {
            if !get(event, "statementSequence")?.is_none()
                && consume(&statements, &int(&event.get_item("statementSequence")?)?)?
            {
                continue;
            }
            return if get(event, "stepId")?.is_none() {
                Ok(none(py))
            } else {
                text(&event.get_item("stepId")?)
            };
        } else if kind.eq(AUDIT_STARTED)? {
            if get(event, "stepId")?.is_none() {
                return Ok(none(py));
            }
            let key = text(&event.get_item("stepId")?)?;
            if consume(&audits, &key)? {
                continue;
            }
            return Ok(key);
        }
    }
    Ok(none(py))
}

/// The latest started statement without a matching completion.
fn active_statement<'py>(py: Python<'py>, events: &[Any<'py>]) -> PyResult<Option<Any<'py>>> {
    let statements: Bound<'py, PyDict> = PyDict::new(py);
    for event in events.iter().rev() {
        let kind = text(&event.get_item("event")?)?;
        if kind.eq(STATEMENT_COMPLETED)? && !get(event, "statementSequence")?.is_none() {
            bump(&statements, &int(&event.get_item("statementSequence")?)?)?;
        } else if kind.eq(STATEMENT_STARTED)?
            && !get(event, "statementSequence")?.is_none()
            && !consume(&statements, &int(&event.get_item("statementSequence")?)?)?
        {
            return Ok(Some(event.clone()));
        }
    }
    Ok(None)
}

fn event_audit_summary<'py>(py: Python<'py>, events: &[Any<'py>]) -> PyResult<Any<'py>> {
    const STATUSES: [&str; 4] = ["passed", "warning", "failed", "error"];
    let mut counts: [i64; 4] = [0; 4];
    for event in events {
        if event.get_item("event")?.ne(AUDIT_COMPLETED)? {
            continue;
        }
        let status = text(&get_or(event, "status", "")?)?;
        for (index, name) in STATUSES.iter().enumerate() {
            if status.eq(name)? {
                counts[index] += 1;
            }
        }
    }
    let total: i64 = counts.iter().sum();
    if total == 0 {
        return Ok(none(py));
    }
    let [passed, warning, failed, error] = counts;
    Ok(py_kwargs!(py; passed = passed, warning = warning, failed = failed, error = error, total = total)?.into_any())
}

fn run_progress<'py>(py: Python<'py>, events: &[Any<'py>]) -> PyResult<Bound<'py, PyDict>> {
    let started: Option<Any<'py>> = first_of(events, RUN_STARTED)?;
    let raw_total: Any<'py> = match &started {
        None => none(py),
        Some(started) => get(started, "totalStatements")?,
    };
    let total_statements: Any<'py> = if raw_total.is_none() {
        raw_total
    } else {
        int(&raw_total)?
    };
    let completed_kinds = PyFrozenSet::new(py, [STATEMENT_COMPLETED, AUDIT_COMPLETED])?;
    let mut completed: i64 = 0;
    for event in events {
        if completed_kinds.contains(event.get_item("event")?)? {
            completed += 1;
        }
    }
    py_kwargs!(py;
        completedOperationCount = completed,
        totalStatements = total_statements,
        currentStep = active_step(py, events)?,
        auditSummary = event_audit_summary(py, events)?,
    )
}

fn last_activity<'py>(py: Python<'py>, events: &[Any<'py>]) -> PyResult<Any<'py>> {
    for event in events.iter().rev() {
        if !get(event, "stepId")?.is_none() {
            return text(&event.get_item("stepId")?);
        }
    }
    Ok(none(py))
}

/// The run payload of an event stream with no durable terminal fact.
fn active_run<'py>(
    invocation_id: &Any<'py>,
    stream: &Any<'py>,
    events: &[Any<'py>],
    failure: Option<&Any<'py>>,
    cancellation_completed: bool,
    warehouse_now: &Any<'py>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = invocation_id.py();
    let started = stream.get_item(0)?;
    let completed: Option<Any<'py>> = last_of(events, RUN_COMPLETED)?;
    let last_signal_at = text(&stream.get_item(-1)?.get_item("emittedAt")?)?;
    let completed_outcome: Any<'py> = match &completed {
        None => none(py),
        Some(completed) => text(&get(completed, "outcome")?)?,
    };
    let mut status = derive_status(
        &none(py),
        &completed_outcome,
        &parse_timestamp(&last_signal_at)?,
        warehouse_now,
        presumed_failed_after_seconds,
    )?;
    if failure.is_some() {
        status = status_member(py, "CANCELLATION_FAILED")?;
    } else if cancellation_completed {
        status = status_member(py, "CANCELLED")?;
    }
    let started_at = text(&started.get_item("emittedAt")?)?;
    let from_completed = |name: &str| -> PyResult<Any<'py>> {
        match &completed {
            None => Ok(none(py)),
            Some(completed) => get(completed, name),
        }
    };
    let run: Bound<'py, PyDict> = PyDict::new(py);
    run.set_item("invocationId", invocation_id)?;
    run.set_item("command", text(&get_or(&started, "command", "build")?)?)?;
    let command = get_or(&started, "command", "build")?;
    run.set_item(
        "displayCommand",
        text(&get_or(&started, "displayCommand", command)?)?,
    )?;
    run.set_item("mode", text(&get_or(&started, "mode", "unknown")?)?)?;
    run.set_item("status", status.str()?)?;
    run.set_item("outcome", status.str()?)?;
    run.set_item("exitCode", from_completed("exitCode")?)?;
    run.set_item("startedAt", &started_at)?;
    let completed_at: Any<'py> = match &completed {
        None => none(py),
        Some(completed) => completed.get_item("emittedAt")?,
    };
    run.set_item("completedAt", completed_at)?;
    run.set_item("lastSignalAt", &last_signal_at)?;
    run.set_item(
        "lastSignalAgeSeconds",
        age_seconds(&last_signal_at, warehouse_now)?,
    )?;
    let completed_text: Any<'py> = match &completed {
        None => none(py),
        Some(completed) => text(&completed.get_item("emittedAt")?)?,
    };
    run.set_item(
        "durationMs",
        derive_run_duration_ms(started_at.clone(), completed_text, warehouse_now.clone())?,
    )?;
    run.set_item(
        "selectedNodeCount",
        int(&get_or(&started, "selectedNodeCount", 0)?)?,
    )?;
    run.set_item("executedLogicalIds", get(&started, "executedLogicalIds")?)?;
    run.set_item("contextLogicalIds", get(&started, "contextLogicalIds")?)?;
    run.set_item("errorMessage", from_completed("errorMessage")?)?;
    run.set_item("toolVersion", text(&get_or(&started, "toolVersion", "")?)?)?;
    run.set_item("projectIdentity", get(&started, "projectIdentity")?)?;
    run.set_item("lastActivity", last_activity(py, events)?)?;
    run.update(run_progress(py, events)?.as_mapping())?;
    if let Some(failure) = failure {
        run.set_item("errorMessage", get(failure, "errorMessage")?)?;
    }
    Ok(run)
}

fn assemble<'py>(
    terminal_by_id: &Any<'py>,
    streams: &Any<'py>,
    warehouse_now: &Any<'py>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = terminal_by_id.py();
    let dict = attr(py, "builtins", "dict")?;
    let terminal: Bound<'py, PyDict> = PyDict::new(py);
    for item in terminal_by_id.call_method0("items")?.try_iter()? {
        let pair = unpack(&item?, 2)?;
        terminal.set_item(&pair[0], dict.call1((&pair[1],))?)?;
    }
    let mut runs: Vec<Any<'py>> = terminal.values().iter().collect();
    for item in streams.call_method0("items")?.try_iter()? {
        let pair = unpack(&item?, 2)?;
        let (invocation_id, stream) = (&pair[0], &pair[1]);
        let events: Vec<Any<'py>> = stream.try_iter()?.collect::<PyResult<_>>()?;
        let failure: Option<Any<'py>> = last_of(&events, CANCELLATION_FAILED)?;
        let cancellation_completed: bool = first_of(&events, CANCELLATION_COMPLETED)?.is_some();
        let Some(run) = terminal.get_item(invocation_id)? else {
            runs.push(
                active_run(
                    invocation_id,
                    stream,
                    &events,
                    failure.as_ref(),
                    cancellation_completed,
                    warehouse_now,
                    presumed_failed_after_seconds,
                )?
                .into_any(),
            );
            continue;
        };
        if let Some(started) = first_of(&events, RUN_STARTED)?
            && get(&started, "displayCommand")?.is_truthy()?
        {
            run.set_item(
                "displayCommand",
                text(&started.get_item("displayCommand")?)?,
            )?;
        }
        let mut signalled: bool = false;
        for event in &events {
            if event.get_item("event")?.ne(RUN_STARTED)? {
                signalled = true;
                break;
            }
        }
        if signalled {
            run.set_item("lastSignalAt", stream.get_item(-1)?.get_item("emittedAt")?)?;
            let progress: Bound<'py, PyDict> = run_progress(py, &events)?;
            if !get(&run, "auditSummary")?.is_none() {
                progress.del_item("auditSummary")?;
            }
            run.call_method1("update", (progress,))?;
        }
        if let Some(failure) = &failure {
            run.set_item("status", CANCELLATION_FAILED)?;
            run.set_item("outcome", CANCELLATION_FAILED)?;
            run.set_item("errorMessage", get(failure, "errorMessage")?)?;
        } else if cancellation_completed {
            run.set_item("status", "cancelled")?;
            run.set_item("outcome", "cancelled")?;
        }
    }
    let mut keyed: Vec<(Bound<'py, PyString>, Any<'py>)> = Vec::with_capacity(runs.len());
    for run in runs {
        keyed.push((run.get_item("startedAt")?.str()?, run));
    }
    // A stable descending sort keeps equal start times in their original order, as
    // `list.sort(reverse=True)` does.
    keyed.sort_by(|left, right| {
        right
            .0
            .as_any()
            .compare(left.0.as_any())
            .unwrap_or(Ordering::Equal)
    });
    Ok(keyed.into_iter().map(|(_, run)| run).collect())
}

/// Merge terminal invocations with their event streams into ordered run payloads.
#[pyfunction]
#[pyo3(signature = (*, terminal_by_id, streams, warehouse_now, limit = None, presumed_failed_after_seconds = DEFAULT_PRESUMED_FAILED))]
pub(crate) fn assemble_runs<'py>(
    terminal_by_id: Any<'py>,
    streams: Any<'py>,
    warehouse_now: Any<'py>,
    limit: Option<isize>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = terminal_by_id.py();
    let runs = PyList::new(
        py,
        assemble(
            &terminal_by_id,
            &streams,
            &warehouse_now,
            presumed_failed_after_seconds,
        )?,
    )?;
    match limit {
        None => Ok(runs.into_any()),
        Some(limit) => runs.as_any().get_item(PySlice::new(py, 0, limit, 1)),
    }
}

/// Return terminal and unterminated event streams with derived states.
#[pyfunction]
#[pyo3(signature = (*, connection, database, limit = Some(DEFAULT_RUNS_LIMIT), presumed_failed_after_seconds = DEFAULT_PRESUMED_FAILED))]
pub(crate) fn read_runs<'py>(
    connection: Any<'py>,
    database: &str,
    limit: Option<i64>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let warehouse_now = parse_timestamp(&connection.call_method0("capture_warehouse_timestamp")?)?;
    let invocations_present: bool = table_exists(&connection, database, INVOCATIONS_TABLE)?;
    let events_present: bool = table_exists(&connection, database, RUN_EVENTS_TABLE)?;
    let terminal: Bound<'py, PyDict> = PyDict::new(py);
    if invocations_present {
        for (limit, scheduled) in [(limit, false), (Some(SCHEDULED_RUNS_LIMIT), true)] {
            let filter = TerminalFilter {
                limit,
                scheduled: Some(scheduled),
                table_exists: Some(true),
                ..TerminalFilter::default()
            };
            terminal.update(terminal_runs(&connection, database, filter)?.as_mapping())?;
        }
    }
    let ids: Vec<String> = terminal
        .keys()
        .iter()
        .map(|key| key.extract())
        .collect::<PyResult<_>>()?;
    let streams = run_started_streams(&connection, database, &ids, events_present)?;
    let active = event_streams(
        &connection,
        database,
        EventFilter {
            recent_limit: Some(RUN_EVENT_WINDOW_SIZE),
            active_only: true,
            exclude_terminal_invocations: invocations_present,
            table_exists: Some(events_present),
            ..EventFilter::default()
        },
    )?;
    streams.update(active.as_mapping())?;
    Ok(PyList::new(
        py,
        assemble(
            terminal.as_any(),
            streams.as_any(),
            &warehouse_now,
            presumed_failed_after_seconds,
        )?,
    )?
    .into_any())
}

/// Return only event streams that have no durable terminal fact.
#[pyfunction]
#[pyo3(signature = (*, connection, database, presumed_failed_after_seconds = DEFAULT_PRESUMED_FAILED))]
pub(crate) fn read_active_runs<'py>(
    connection: Any<'py>,
    database: &str,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let warehouse_now = parse_timestamp(&connection.call_method0("capture_warehouse_timestamp")?)?;
    let invocations_present: bool = table_exists(&connection, database, INVOCATIONS_TABLE)?;
    let streams = event_streams(
        &connection,
        database,
        EventFilter {
            recent_limit: Some(RUN_EVENT_WINDOW_SIZE),
            active_only: true,
            exclude_terminal_invocations: invocations_present,
            ..EventFilter::default()
        },
    )?;
    let terminal = PyDict::new(py);
    Ok(PyList::new(
        py,
        assemble(
            terminal.as_any(),
            streams.as_any(),
            &warehouse_now,
            presumed_failed_after_seconds,
        )?,
    )?
    .into_any())
}

fn project_identity_predicate(project_identity: &str) -> String {
    let logical: &str = project_identity
        .rsplit_once('/')
        .map_or(project_identity, |(_, name)| name);
    format!(
        "(project_identity = '{}' OR endsWith(project_identity, '{}'))",
        sql_literal(logical),
        sql_literal(&format!("/{logical}"))
    )
}

fn latest_direct_build_rows<'py>(
    connection: &Any<'py>,
    database: &str,
    project_identity: &str,
    column: &str,
    outcome_clause: &str,
) -> PyResult<Option<Vec<Any<'py>>>> {
    if !table_exists(connection, database, INVOCATIONS_TABLE)? {
        return Ok(None);
    }
    let query: String = format!(
        "SELECT {column} FROM `{database}`.`{INVOCATIONS_TABLE}` WHERE {} AND \
         target_identity = '{}' AND command = 'build' AND mode = 'direct' \
         AND {outcome_clause} ORDER BY completed_at DESC, invocation_id DESC LIMIT 1",
        project_identity_predicate(project_identity),
        sql_literal(database),
    );
    named_rows(connection, query).map(Some)
}

/// Return the latest terminal direct-build materialization outcome for this target.
#[pyfunction]
#[pyo3(signature = (*, connection, database, project_identity))]
pub(crate) fn read_latest_direct_build_materialization<'py>(
    connection: Any<'py>,
    database: &str,
    project_identity: &str,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let rows = latest_direct_build_rows(
        &connection,
        database,
        project_identity,
        "materialized_outcome",
        "materialized_outcome IS NOT NULL",
    )?;
    let Some(row) = rows.as_ref().and_then(|rows| rows.first()) else {
        return Ok(none(py));
    };
    let value = row.get_item("materialized_outcome")?;
    if value.is_truthy()? {
        text(&value)
    } else {
        Ok(none(py))
    }
}

/// Return the latest successful direct materialization completion for this target.
#[pyfunction]
#[pyo3(signature = (*, connection, database, project_identity))]
pub(crate) fn read_latest_applied_direct_build_at<'py>(
    connection: Any<'py>,
    database: &str,
    project_identity: &str,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let rows = latest_direct_build_rows(
        &connection,
        database,
        project_identity,
        "toString(completed_at) AS completed_at",
        "materialized_outcome = 'applied'",
    )?;
    match rows.as_ref().and_then(|rows| rows.first()) {
        None => Ok(none(py)),
        Some(row) => text(&row.get_item("completed_at")?),
    }
}

/// Approximate replay progress weighted by captured offset span.
struct OffsetProgress {
    percentage: f64,
    eta_seconds: Option<f64>,
    completed_span: i64,
    total_span: i64,
    observed_partitions: usize,
    total_partitions: usize,
}

fn offset_progress(
    request: &Any<'_>,
    frontiers: &Any<'_>,
    elapsed_seconds: f64,
    completed: bool,
) -> PyResult<Option<OffsetProgress>> {
    let mut spans: Vec<(Any<'_>, i64)> = Vec::new();
    for item in request.getattr("ranges")?.try_iter()? {
        let item = item?;
        let upper: i64 = item.getattr("upper_offset")?.extract()?;
        let lower: i64 = item.getattr("lower_offset")?.extract()?;
        spans.push((item, (upper - lower).max(0)));
    }
    let total_span: i64 = spans.iter().map(|(_, span)| span).sum();
    if total_span <= 0 {
        return Ok(None);
    }
    let mut frontier_by_partition: HashMap<i64, i64> = HashMap::new();
    for frontier in frontiers.try_iter()? {
        let frontier = frontier?;
        let partition: i64 = frontier.getattr("partition")?.extract()?;
        let offset: i64 = frontier.getattr("completed_offset")?.extract()?;
        let previous: i64 = frontier_by_partition
            .get(&partition)
            .copied()
            .unwrap_or(offset);
        frontier_by_partition.insert(partition, offset.max(previous));
    }
    let completed_span: i64 = if completed {
        total_span
    } else {
        let mut sum: i64 = 0;
        for (item, span) in &spans {
            let partition: i64 = item.getattr("partition")?.extract()?;
            let lower: i64 = item.getattr("lower_offset")?.extract()?;
            let frontier: i64 = frontier_by_partition
                .get(&partition)
                .copied()
                .unwrap_or(lower);
            sum += (frontier - lower).max(0).min(*span);
        }
        sum
    };
    let percentage: f64 = py_min(
        py_max(completed_span as f64 / total_span as f64 * 100.0, 0.0),
        100.0,
    );
    let velocity: f64 = if elapsed_seconds <= 0.0 {
        0.0
    } else {
        completed_span as f64 / elapsed_seconds
    };
    let eta_seconds: Option<f64> = if completed {
        Some(0.0)
    } else if velocity <= 0.0 {
        None
    } else {
        Some(py_max((total_span - completed_span) as f64 / velocity, 0.0))
    };
    let mut partitions: HashSet<i64> = HashSet::new();
    for (item, _) in &spans {
        partitions.insert(item.getattr("partition")?.extract()?);
    }
    Ok(Some(OffsetProgress {
        percentage,
        eta_seconds,
        completed_span,
        total_span,
        observed_partitions: frontier_by_partition
            .keys()
            .filter(|partition| partitions.contains(partition))
            .count(),
        total_partitions: partitions.len(),
    }))
}

/// Weight partition completion by captured offset span and clamp sparse frontiers.
#[pyfunction]
#[pyo3(signature = (*, request, frontiers, elapsed_seconds, completed = false))]
pub(crate) fn calculate_replay_offset_progress<'py>(
    request: Any<'py>,
    frontiers: Any<'py>,
    elapsed_seconds: f64,
    completed: bool,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = request.py();
    let Some(progress) = offset_progress(&request, &frontiers, elapsed_seconds, completed)? else {
        return Ok(none(py));
    };
    attr(py, "sqltide.dev_server.models", "ReplayOffsetProgress")?.call(
        (),
        Some(&py_kwargs!(py;
            percentage = progress.percentage,
            eta_seconds = progress.eta_seconds,
            completed_span = progress.completed_span,
            total_span = progress.total_span,
            observed_partitions = progress.observed_partitions,
            total_partitions = progress.total_partitions,
        )?),
    )
}

fn decode_ranges<'py>(
    metadata: &Any<'py>,
    raw_ranges: &Any<'py>,
) -> PyResult<(Vec<Any<'py>>, [Any<'py>; 4])> {
    let py: Python<'py> = metadata.py();
    let range_class = attr(py, "sqltide.adapter.models", "AdapterReplayOffsetRange")?;
    let mut ranges: Vec<Any<'py>> = Vec::new();
    for raw_item in raw_ranges.try_iter()? {
        let item = raw_item?;
        if !is_mapping(&item)? {
            continue;
        }
        let range = range_class.call(
            (),
            Some(&py_kwargs!(py;
                partition = int(&item.get_item("partition")?)?,
                lower_offset = int(&item.get_item("lowerOffset")?)?,
                upper_offset = int(&item.get_item("upperOffset")?)?,
            )?),
        )?;
        ranges.push(range);
    }
    let names = [
        text(&metadata.get_item("database")?)?,
        text(&metadata.get_item("relation")?)?,
        text(&metadata.get_item("partitionColumn")?)?,
        text(&metadata.get_item("offsetColumn")?)?,
    ];
    Ok((ranges, names))
}

/// Decode persisted event metadata conservatively for historical compatibility.
#[pyfunction]
pub(crate) fn decode_replay_offset_progress_request<'py>(value: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    if !is_mapping(&value)? {
        return Ok(none(py));
    }
    let raw_ranges = get(&value, "ranges")?;
    if !raw_ranges.is_instance_of::<PyList>() {
        return Ok(none(py));
    }
    let (ranges, [database, relation, partition_column, offset_column]) =
        match decode_ranges(&value, &raw_ranges) {
            Ok(decoded) => decoded,
            Err(error)
                if error.is_instance_of::<PyKeyError>(py)
                    || error.is_instance_of::<PyTypeError>(py)
                    || error.is_instance_of::<PyValueError>(py) =>
            {
                return Ok(none(py));
            }
            Err(error) => return Err(error),
        };
    if ranges.is_empty() {
        return Ok(none(py));
    }
    for name in [&database, &relation, &partition_column, &offset_column] {
        if !name.is_truthy()? {
            return Ok(none(py));
        }
    }
    attr(
        py,
        "sqltide.adapter.models",
        "AdapterReplayOffsetProgressRequest",
    )?
    .call(
        (),
        Some(&py_kwargs!(py;
            database = database,
            relation = relation,
            partition_column = partition_column,
            offset_column = offset_column,
            ranges = pyo3::types::PyTuple::new(py, ranges)?,
        )?),
    )
}

/// Live warehouse progress of the active statement, with replay offset progress when the
/// statement recorded its offset ranges.
#[pyfunction]
#[pyo3(signature = (*, connection, events, observed_at))]
pub(crate) fn statement_progress<'py>(
    connection: Any<'py>,
    events: Any<'py>,
    observed_at: Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let events: Vec<Any<'py>> = events.try_iter()?.collect::<PyResult<_>>()?;
    let Some(active) = active_statement(py, &events)? else {
        return Ok(none(py));
    };
    if get(&active, "queryId")?.is_none() {
        return Ok(none(py));
    }
    let query_id = text(&active.get_item("queryId")?)?;
    let adapter_error = attr(py, "sqltide.adapter.exceptions", "AdapterError")?;
    let tolerant = |result: PyResult<Any<'py>>| -> PyResult<Any<'py>> {
        match result {
            Err(error) if error.is_instance(py, &adapter_error) => Ok(none(py)),
            other => other,
        }
    };
    let progress = tolerant(connection.call_method(
        "load_statement_progress",
        (),
        Some(&py_kwargs!(py; query_id = &query_id)?),
    ))?;
    let payload: Bound<'py, PyDict> = py_kwargs!(py;
        found = !progress.is_none(),
        queryId = &query_id,
        statementSequence = int(&active.get_item("statementSequence")?)?,
        stepId = get(&active, "stepId")?,
        phase = get(&active, "phase")?,
        observedAt = observed_at,
    )?;
    if progress.is_none() {
        return Ok(payload.into_any());
    }
    let elapsed = progress.getattr("elapsed_seconds")?;
    let rate = |name: &str| -> PyResult<Any<'py>> {
        if elapsed.le(0)? {
            Ok(0.0_f64.into_pyobject(py)?.into_any())
        } else {
            progress.getattr(name)?.div(&elapsed)
        }
    };
    let metrics: Bound<'py, PyDict> = py_kwargs!(py;
        elapsedSeconds = &elapsed,
        readRows = progress.getattr("read_rows")?,
        readBytes = progress.getattr("read_bytes")?,
        totalRowsApprox = progress.getattr("total_rows_approx")?,
        memoryUsageBytes = progress.getattr("memory_usage_bytes")?,
        readRowsPerSecond = rate("read_rows")?,
        readBytesPerSecond = rate("read_bytes")?,
        settings = attr(py, "builtins", "dict")?.call1((progress.getattr("settings")?,))?,
    )?;
    payload.update(metrics.as_mapping())?;
    let request = decode_replay_offset_progress_request(get(&active, "replayOffsetProgress")?)?;
    if request.is_none() {
        return Ok(payload.into_any());
    }
    let frontiers = tolerant(connection.call_method(
        "load_replay_offset_frontiers",
        (),
        Some(&py_kwargs!(py; query_id = &query_id, request = &request)?),
    ))?;
    if frontiers.is_none() {
        return Ok(payload.into_any());
    }
    if let Some(progress) = offset_progress(&request, &frontiers, elapsed.extract()?, false)? {
        payload.set_item(
            "replayOffsetProgress",
            py_kwargs!(py;
                percentage = progress.percentage,
                etaSeconds = progress.eta_seconds,
                completedSpan = progress.completed_span,
                totalSpan = progress.total_span,
                observedPartitions = progress.observed_partitions,
                totalPartitions = progress.total_partitions,
            )?,
        )?;
    }
    Ok(payload.into_any())
}

/// Return durable cursor events and the current derived run state.
#[pyfunction]
#[pyo3(signature = (*, connection, database, invocation_id, after = None, presumed_failed_after_seconds = DEFAULT_PRESUMED_FAILED))]
pub(crate) fn read_run_events<'py>(
    connection: Any<'py>,
    database: &str,
    invocation_id: &str,
    after: Option<Any<'py>>,
    presumed_failed_after_seconds: i64,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let warehouse_timestamp = connection.call_method0("capture_warehouse_timestamp")?;
    let warehouse_now = parse_timestamp(&warehouse_timestamp)?;
    let terminal = terminal_runs(
        &connection,
        database,
        TerminalFilter {
            invocation_id: Some(invocation_id),
            ..TerminalFilter::default()
        },
    )?;
    let status_streams = event_streams(
        &connection,
        database,
        EventFilter {
            invocation_id: Some(invocation_id),
            recent_limit: Some(RUN_EVENT_WINDOW_SIZE),
            ..EventFilter::default()
        },
    )?;
    let runs = assemble(
        terminal.as_any(),
        status_streams.as_any(),
        &warehouse_now,
        presumed_failed_after_seconds,
    )?;
    let run: Option<&Any<'py>> = runs.first();
    let streams = event_streams(
        &connection,
        database,
        EventFilter {
            invocation_id: Some(invocation_id),
            after: after.as_ref(),
            row_limit: Some(RUN_EVENT_PAGE_SIZE),
            ..EventFilter::default()
        },
    )?;
    let events: Any<'py> = streams
        .get_item(invocation_id)?
        .unwrap_or_else(|| PyList::empty(py).into_any());
    let event_count: usize = events.len()?;
    let run_value = |name: &str| -> PyResult<Any<'py>> {
        match run {
            None => Ok(none(py)),
            Some(run) => run.get_item(name),
        }
    };
    let status_events: Vec<Any<'py>> = match status_streams.get_item(invocation_id)? {
        None => Vec::new(),
        Some(stream) => stream.try_iter()?.collect::<PyResult<_>>()?,
    };
    Ok(py_kwargs!(py;
        found = run.is_some() || event_count > 0,
        events = &events,
        hasMore = event_count == RUN_EVENT_PAGE_SIZE,
        status = run_value("status")?,
        lastSignalAt = run_value("lastSignalAt")?,
        lastSignalAgeSeconds = run_value("lastSignalAgeSeconds")?,
        statementProgress = statement_progress(connection.clone(), PyList::new(py, status_events)?.into_any(), warehouse_timestamp)?,
    )?
    .into_any())
}

/// Return one lazily requested SQL statement for a durable run.
#[pyfunction]
#[pyo3(signature = (*, connection, database, invocation_id, statement_sequence))]
pub(crate) fn read_run_statement<'py>(
    connection: Any<'py>,
    database: &str,
    invocation_id: Any<'py>,
    statement_sequence: Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let missing = || -> PyResult<Any<'py>> { Ok(py_kwargs!(py; found = false)?.into_any()) };
    if !table_exists(&connection, database, RUN_STATEMENTS_TABLE)? {
        return missing();
    }
    let invocation_literal: String = sql_literal(&invocation_id.extract::<String>()?);
    let sequence = attr(py, "builtins", "int")?.call1((&statement_sequence,))?;
    let query: String = format!(
        "SELECT step_id, phase, intent, sql, toString(sql_sha256) AS sql_sha256, \
         toString(workflow_sha256) AS workflow_sha256 FROM `{database}`.`{RUN_STATEMENTS_TABLE}` FINAL \
         WHERE invocation_id = '{invocation_literal}' AND statement_sequence = {} LIMIT 1",
        sequence.str()?
    );
    let rows = named_rows(&connection, query)?;
    let Some(row) = rows.first() else {
        return missing();
    };
    Ok(py_kwargs!(py;
        found = true,
        invocationId = &invocation_id,
        statementSequence = &statement_sequence,
        stepId = text(&row.get_item("step_id")?)?,
        phase = text(&row.get_item("phase")?)?,
        intent = text(&row.get_item("intent")?)?,
        sql = text(&row.get_item("sql")?)?,
        sqlSha256 = text(&row.get_item("sql_sha256")?)?,
        workflowSha256 = text(&row.get_item("workflow_sha256")?)?,
    )?
    .into_any())
}

/// Return one exact terminal run without applying the history-list limit.
#[pyfunction]
#[pyo3(signature = (*, connection, database, invocation_id))]
pub(crate) fn read_terminal_run<'py>(
    connection: Any<'py>,
    database: &str,
    invocation_id: &str,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let runs = terminal_runs(
        &connection,
        database,
        TerminalFilter {
            invocation_id: Some(invocation_id),
            ..TerminalFilter::default()
        },
    )?;
    Ok(runs.get_item(invocation_id)?.unwrap_or_else(|| none(py)))
}

/// Return exact terminal intent, falling back to complete destruction events.
#[pyfunction]
#[pyo3(signature = (*, connection, database, invocation_id))]
pub(crate) fn read_destruction_recovery_run<'py>(
    connection: Any<'py>,
    database: &str,
    invocation_id: &str,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let terminal = read_terminal_run(connection.clone(), database, invocation_id)?;
    if !terminal.is_none() {
        return Ok(terminal);
    }
    let streams = event_streams(
        &connection,
        database,
        EventFilter {
            invocation_id: Some(invocation_id),
            recent_limit: Some(1),
            ..EventFilter::default()
        },
    )?;
    let events: Vec<Any<'py>> = match streams.get_item(invocation_id)? {
        None => Vec::new(),
        Some(stream) => stream.try_iter()?.collect::<PyResult<_>>()?,
    };
    let started = first_of(&events, RUN_STARTED)?;
    let completed = last_of(&events, RUN_COMPLETED)?;
    let (Some(started), Some(completed)) = (started, completed) else {
        return Ok(none(py));
    };
    let mut evidence = get(&completed, "operationEvidence")?;
    if !is_mapping(&evidence)? {
        evidence = get(&started, "operationEvidence")?;
    }
    let summary: Any<'py> = if is_mapping(&evidence)? {
        attr(py, "builtins", "dict")?.call1((evidence,))?
    } else {
        none(py)
    };
    Ok(py_kwargs!(py;
        invocationId = invocation_id,
        projectIdentity = get(&started, "projectIdentity")?,
        command = get(&started, "command")?,
        mode = get(&started, "mode")?,
        outcome = get(&completed, "outcome")?,
        summary = summary,
    )?
    .into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(derive_run_status, module)?)?;
    module.add_function(wrap_pyfunction!(derive_run_duration_ms, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_audit_summary, module)?)?;
    module.add_function(wrap_pyfunction!(assemble_runs, module)?)?;
    module.add_function(wrap_pyfunction!(statement_progress, module)?)?;
    module.add_function(wrap_pyfunction!(read_runs, module)?)?;
    module.add_function(wrap_pyfunction!(read_active_runs, module)?)?;
    module.add_function(wrap_pyfunction!(read_run_events, module)?)?;
    module.add_function(wrap_pyfunction!(read_run_statement, module)?)?;
    module.add_function(wrap_pyfunction!(read_terminal_run, module)?)?;
    module.add_function(wrap_pyfunction!(read_destruction_recovery_run, module)?)?;
    module.add_function(wrap_pyfunction!(
        read_latest_direct_build_materialization,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        read_latest_applied_direct_build_at,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        decode_replay_offset_progress_request,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(calculate_replay_offset_progress, module)?)
}
