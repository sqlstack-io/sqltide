//! Python bindings for CLI start times and audit warmup and schedule timing.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::{PyOverflowError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use sqltide_executor::audit_schedule::{LastRun, ScheduledAudit, Warmup, resolve_warmup, schedule};
use sqltide_executor::timestamps::{
    Instant, TimestampError, normalize_start_time, parse_warehouse_timestamp,
};

const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";
const AUDITING_MODELS: &str = "sqltide.executor.auditing.models";
const DEV_SERVER_TYPES: &str = "sqltide.dev_server.types";
const START_TIME_ERROR: &str =
    "--start-time must be YYYY-MM-DD or a UTC timestamp like YYYY-MM-DDTHH:MM:SS[.sss][Z]";

pub(crate) fn timestamp_error(error: TimestampError) -> PyErr {
    match error {
        TimestampError::Value(message) => PyValueError::new_err(message),
        TimestampError::Overflow(message) => PyOverflowError::new_err(message),
        TimestampError::Type(message) => PyTypeError::new_err(message),
    }
}

/// A `--start-time` value as a ClickHouse millisecond timestamp; other formats raise
/// `CliUserError`.
#[pyfunction]
fn normalize_cli_start_time(py: Python<'_>, raw_value: &str) -> PyResult<String> {
    normalize_start_time(raw_value).ok_or_else(|| {
        match py
            .import(CLI_EXCEPTIONS)
            .and_then(|module| module.getattr("CliUserError"))
            .and_then(|class| class.call1((START_TIME_ERROR,)))
        {
            Ok(exception) => PyErr::from_value(exception),
            Err(error) => error,
        }
    })
}

/// `audit.name`, or the audit file's stem when it is unnamed.
fn audit_name(audit: &Bound<'_, PyAny>) -> PyResult<String> {
    let name: Bound<'_, PyAny> = audit.getattr("name")?;
    if name.is_truthy()? {
        name.extract()
    } else {
        audit.getattr("file_path")?.getattr("stem")?.extract()
    }
}

/// Warmup eligibility by audit name, from the newest anchor across each audit's models.
#[pyfunction]
#[pyo3(signature = (*, audits, anchors_by_model, materialized_model_names, warehouse_now))]
fn resolve_audit_warmup_states<'py>(
    py: Python<'py>,
    audits: Vec<Bound<'py, PyAny>>,
    anchors_by_model: HashMap<String, String>,
    materialized_model_names: HashSet<String>,
    warehouse_now: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let now: Instant = parse_warehouse_timestamp(warehouse_now).map_err(timestamp_error)?;
    let state_class: Bound<'py, PyAny> = py.import(AUDITING_MODELS)?.getattr("AuditWarmupState")?;
    let states: Bound<'py, PyDict> = PyDict::new(py);
    for audit in audits {
        let referenced: Vec<String> = audit.getattr("referenced_model_names")?.extract()?;
        let warmup: Warmup = resolve_warmup(
            &referenced,
            audit.getattr("warmup_seconds")?.extract()?,
            &anchors_by_model,
            &materialized_model_names,
            now,
        )
        .map_err(timestamp_error)?;
        let arguments: Bound<'py, PyDict> = PyDict::new(py);
        arguments.set_item("eligible", warmup.eligible)?;
        arguments.set_item("anchor", warmup.anchor)?;
        arguments.set_item("eligible_at", warmup.eligible_at)?;
        arguments.set_item(
            "missing_model_names",
            PyTuple::new(py, warmup.missing_model_names)?,
        )?;
        states.set_item(audit_name(&audit)?, state_class.call((), Some(&arguments))?)?;
    }
    Ok(states)
}

/// One audit's dry-run scheduler entry: its state, when it is scheduled for, and the warmup and
/// last-run evidence behind that.
#[pyfunction]
#[pyo3(signature = (*, audit, warmup, status, warehouse_now, anchors))]
fn audit_schedule_entry<'py>(
    py: Python<'py>,
    audit: &Bound<'py, PyAny>,
    warmup: &Bound<'py, PyAny>,
    status: Option<&Bound<'py, PyAny>>,
    warehouse_now: &str,
    anchors: HashMap<String, String>,
) -> PyResult<Bound<'py, PyDict>> {
    let eligible_at: Bound<'py, PyAny> = warmup.getattr("eligible_at")?;
    let anchor: Bound<'py, PyAny> = warmup.getattr("anchor")?;
    let missing: Bound<'py, PyAny> = warmup.getattr("missing_model_names")?;
    let eligible_at_text: Option<String> = if eligible_at.is_truthy()? {
        Some(eligible_at.extract()?)
    } else {
        None
    };
    let completed_at: Option<String> = match status {
        Some(status) => {
            let value: Bound<'py, PyAny> = status.call_method1("get", ("completedAt",))?;
            value
                .extract::<String>()
                .ok()
                .filter(|value| !value.is_empty())
        }
        None => None,
    };
    let last_status: String = match status {
        Some(status) => status.get_item("status")?.str()?.extract()?,
        None => "never_run".to_owned(),
    };
    let referenced: Vec<String> = audit.getattr("referenced_model_names")?.extract()?;
    let cadence: Bound<'py, PyAny> = audit.getattr("cadence_seconds")?;
    let (state, scheduled_for) = schedule(
        &ScheduledAudit {
            referenced_model_names: &referenced,
            cadence_seconds: cadence.extract()?,
            has_missing_models: missing.is_truthy()?,
            eligible: warmup.getattr("eligible")?.is_truthy()?,
            anchored: !anchor.is_none(),
            eligible_at: eligible_at_text.as_deref(),
            last_run: status.map(|_| LastRun {
                status: &last_status,
                completed_at: completed_at.as_deref(),
            }),
        },
        &anchors,
        warehouse_now,
    )
    .map_err(timestamp_error)?;
    let entry: Bound<'py, PyDict> = PyDict::new(py);
    entry.set_item("name", audit_name(audit)?)?;
    entry.set_item(
        "state",
        py.import(DEV_SERVER_TYPES)?
            .getattr("AuditScheduleState")?
            .call1((state.as_str(),))?,
    )?;
    entry.set_item("scheduledFor", scheduled_for.map(Instant::render))?;
    entry.set_item("eligibleAt", eligible_at)?;
    entry.set_item("anchor", anchor)?;
    entry.set_item("cadenceSeconds", cadence)?;
    entry.set_item("warmupSeconds", audit.getattr("warmup_seconds")?)?;
    entry.set_item("lastStatus", last_status)?;
    entry.set_item("referencedModels", PyList::new(py, referenced)?)?;
    entry.set_item(
        "missingRelations",
        PyList::new(py, missing.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
    )?;
    Ok(entry)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(normalize_cli_start_time, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_audit_warmup_states, module)?)?;
    module.add_function(wrap_pyfunction!(audit_schedule_entry, module)?)
}
