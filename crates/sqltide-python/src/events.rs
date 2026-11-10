//! Derive sensor events from persisted invocation and quality node result rows.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::dev_server::support::attr;
use crate::direct_build::build;

const EVENT_MODELS: &str = "sqltide.events.models";
const EVENT_TYPES: &str = "sqltide.events.types";
const QUALITY_TYPES: &str = "sqltide.compiler.quality.types";
const RESULT_TYPES: &str = "sqltide.executor.auditing.types";
const RUN_COMMANDS: [&str; 7] = [
    "AUDIT",
    "TEST",
    "BUILD",
    "DEPLOYMENT_PROMOTE",
    "JANITOR",
    "DESTROY_PIPELINES",
    "RESET_TARGET",
];
const UNHEALTHY_STATUSES: [&str; 3] = ["WARNING", "FAILED", "ERROR"];
const TERMINAL_STATUSES: [&str; 4] = ["PASSED", "WARNING", "FAILED", "ERROR"];

/// `value == member` for any of `members` of the enum `class_name` in `module`.
fn matches_any(
    value: &Bound<'_, PyAny>,
    module: &str,
    class_name: &str,
    members: &[&str],
) -> PyResult<bool> {
    let enum_class: Bound<'_, PyAny> = attr(value.py(), module, class_name)?;
    for member in members {
        if value.eq(enum_class.getattr(*member)?)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn is_unhealthy(status: &Bound<'_, PyAny>) -> PyResult<bool> {
    matches_any(
        status,
        RESULT_TYPES,
        "QualityResultStatus",
        &UNHEALTHY_STATUSES,
    )
}

/// The transition of one terminal audit status against the prior persisted result.
fn audit_transition<'py>(
    status: &Bound<'py, PyAny>,
    previous_status: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let failing: bool = is_unhealthy(status)?;
    let previously_failing: bool = !previous_status.is_none() && is_unhealthy(previous_status)?;
    let member: &str = match (failing, previously_failing) {
        (true, true) => "STILL_FAILING",
        (true, false) => "NEW_FAILURE",
        (false, true) => "RECOVERED",
        (false, false) => "STILL_PASSING",
    };
    attr(status.py(), EVENT_TYPES, "AuditTransition")?.getattr(member)
}

/// The sample columns and rows a quality result payload carries, or two empty tuples.
fn audit_sample<'py>(
    payload_json: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyTuple>, Bound<'py, PyTuple>)> {
    let py: Python<'py> = payload_json.py();
    let empty = || (PyTuple::empty(py), PyTuple::empty(py));
    let json: Bound<'py, PyModule> = py.import("json")?;
    let payload: Bound<'py, PyAny> = match json.getattr("loads")?.call1((payload_json,)) {
        Ok(payload) => payload,
        Err(error)
            if error.is_instance(py, &json.getattr("JSONDecodeError")?)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            return Ok(empty());
        }
        Err(error) => return Err(error),
    };
    let Ok(payload) = payload.cast_into::<PyDict>() else {
        return Ok(empty());
    };
    let columns: Option<Bound<'py, PyAny>> = payload.get_item("sample_column_names")?;
    let rows: Option<Bound<'py, PyAny>> = payload.get_item("sample_rows")?;
    let (Some(columns), Some(rows)) = (columns, rows) else {
        return Ok(empty());
    };
    let (Ok(columns), Ok(rows)) = (columns.cast_into::<PyList>(), rows.cast_into::<PyList>())
    else {
        return Ok(empty());
    };
    if !columns
        .iter()
        .all(|column| column.is_instance_of::<PyString>())
        || !rows.iter().all(|row| row.is_instance_of::<PyList>())
    {
        return Ok(empty());
    }
    let mut sample_rows: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        sample_rows.push(row.cast_into::<PyList>()?.to_tuple());
    }
    Ok((columns.to_tuple(), PyTuple::new(py, sample_rows)?))
}

/// Map one node result row to its derived events, exhaustively by node kind.
#[pyfunction]
#[pyo3(signature = (*, row, previous_status, target))]
pub(crate) fn events_from_node_result<'py>(
    row: Bound<'py, PyAny>,
    previous_status: Bound<'py, PyAny>,
    target: Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py: Python<'py> = row.py();
    let node_kind: Bound<'py, PyAny> = row.getattr("node_kind")?;
    if matches_any(&node_kind, QUALITY_TYPES, "QualityNodeKind", &["TEST"])? {
        return Ok(Some(PyTuple::empty(py)));
    }
    if !matches_any(&node_kind, QUALITY_TYPES, "QualityNodeKind", &["AUDIT"])? {
        return Ok(None);
    }
    let (sample_column_names, sample_rows) = audit_sample(&row.getattr("payload_json")?)?;
    let status: Bound<'py, PyAny> = row.getattr("status")?;
    if matches_any(&status, RESULT_TYPES, "QualityResultStatus", &["DEFERRED"])? {
        return Ok(Some(PyTuple::empty(py)));
    }
    if !matches_any(
        &status,
        RESULT_TYPES,
        "QualityResultStatus",
        &TERMINAL_STATUSES,
    )? {
        return Ok(None);
    }
    let field = |name: &str| row.getattr(name);
    let event: Bound<'py, PyAny> = build(
        py,
        EVENT_MODELS,
        "AuditCompleted",
        &[
            ("id", field("result_id")?),
            ("audit_name", field("node_name")?),
            ("status", status.clone()),
            ("previous_status", previous_status.clone()),
            ("transition", audit_transition(&status, &previous_status)?),
            ("severity", field("severity")?),
            ("failure_count", field("failure_count")?),
            ("target", target),
            ("trigger", field("trigger")?),
            ("completed_at", field("completed_at")?),
            ("binding_key", field("binding_key")?),
            ("invocation_id", field("invocation_id")?),
            ("scheduled_for", field("scheduled_for")?),
            ("error_message", field("error_message")?),
            ("sample_column_names", sample_column_names.into_any()),
            ("sample_rows", sample_rows.into_any()),
        ],
    )?;
    Ok(Some(PyTuple::new(py, [event])?))
}

/// Map one invocation row to its derived events, exhaustively by command.
#[pyfunction]
#[pyo3(signature = (*, row, target))]
pub(crate) fn events_from_invocation<'py>(
    row: Bound<'py, PyAny>,
    target: Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py: Python<'py> = row.py();
    let command: Bound<'py, PyAny> =
        match attr(py, EVENT_TYPES, "ObservedCommand")?.call1((row.getattr("command")?,)) {
            Ok(command) => command,
            Err(error) if error.is_instance_of::<PyValueError>(py) => {
                return Ok(Some(PyTuple::empty(py)));
            }
            Err(error) => return Err(error),
        };
    if !matches_any(&command, EVENT_TYPES, "ObservedCommand", &RUN_COMMANDS)? {
        return Ok(None);
    }
    let field = |name: &str| row.getattr(name);
    let event: Bound<'py, PyAny> = build(
        py,
        EVENT_MODELS,
        "RunCompleted",
        &[
            ("id", field("invocation_id")?),
            ("command", command.clone()),
            ("mode", field("mode")?),
            ("outcome", field("outcome")?),
            ("exit_code", field("exit_code")?),
            ("target", target),
            ("deployment_id", field("deployment_id")?),
            ("selected_node_count", field("selected_node_count")?),
            ("error_message", field("error_message")?),
            ("completed_at", field("completed_at")?),
        ],
    )?;
    Ok(Some(PyTuple::new(py, [event])?))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(events_from_node_result, module)?)?;
    module.add_function(wrap_pyfunction!(events_from_invocation, module)?)
}
