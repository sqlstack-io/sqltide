//! The dev server's scheduled audits: due-state calculation, one due batch's execution and
//! persistence, and the single-process polling scheduler.

use std::sync::{Mutex, MutexGuard, PoisonError, TryLockError};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PySlice, PyString, PyTuple};

use super::routes::DEV_EXCEPTIONS;
use super::support::attr;
use crate::handling::{raise_from, while_handling, within};
use crate::py_kwargs;

const NATIVE: &str = "sqltide._native";
const DEV_TYPES: &str = "sqltide.dev_server.types";
const OBSERVABILITY_TYPES: &str = "sqltide.executor.observability.types";
const OBSERVABILITY_MODELS: &str = "sqltide.executor.observability.models";
const AUDITING_TYPES: &str = "sqltide.executor.auditing.types";
const POLL_SECONDS: f64 = 10.0;
const MAX_BACKOFF_SECONDS: f64 = 300.0;

type Any<'py> = Bound<'py, PyAny>;

pub(super) fn member<'py>(py: Python<'py>, module: &str, class: &str, name: &str) -> PyResult<Any<'py>> {
    attr(py, module, class)?.getattr(name)
}

fn schedule_state<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    member(py, DEV_TYPES, "AuditScheduleState", name)
}

/// `f"..."` of literal text and formatted values, as `"".join(format(part, ""))` builds it.
pub(super) fn fstring<'py>(py: Python<'py>, parts: &[Any<'py>]) -> PyResult<Any<'py>> {
    let format: Any<'py> = attr(py, "builtins", "format")?;
    let pieces: Vec<Any<'py>> = parts.iter().map(|part| format.call1((part, ""))).collect::<PyResult<_>>()?;
    PyString::new(py, "").call_method1("join", (pieces,))
}

pub(super) fn literal<'py>(py: Python<'py>, text: &str) -> Any<'py> {
    PyString::new(py, text).into_any()
}

/// `audit.name or audit.file_path.stem`.
pub(super) fn audit_name<'py>(audit: &Any<'py>) -> PyResult<Any<'py>> {
    let name: Any<'py> = audit.getattr("name")?;
    if name.is_truthy()? { Ok(name) } else { audit.getattr("file_path")?.getattr("stem") }
}

pub(super) fn logical_project_identity<'py>(project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = project_dir.py();
    attr(py, "sqltide.executor.observability.main.logical_project_identity", "logical_project_identity")?
        .call((), Some(&py_kwargs!(py; project_dir = project_dir)?))
}

/// Whether one effective-configuration section of the compiled target is enabled.
fn section_enabled(analysis: &Any<'_>, section: &str) -> PyResult<bool> {
    let loaded_project: Any<'_> = analysis.getattr("discovered_inputs")?.getattr("loaded_project")?;
    if loaded_project.is_none() {
        return Ok(false);
    }
    let configuration: Any<'_> = loaded_project.getattr("effective_configuration")?;
    if configuration.is_none() {
        return Ok(false);
    }
    configuration.getattr(section)?.getattr("enabled")?.is_truthy()
}

/// Return whether the compiled target enables scheduled audits.
#[pyfunction]
pub(crate) fn scheduler_enabled(analysis: &Any<'_>) -> PyResult<bool> {
    section_enabled(analysis, "audit_scheduler")
}

/// Return whether the compiled target enables the sensor dispatcher.
#[pyfunction]
pub(crate) fn sensors_enabled(analysis: &Any<'_>) -> PyResult<bool> {
    section_enabled(analysis, "sensors")
}

/// Return the current due-state preview without executing audit SQL.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, database, project_dir))]
pub(crate) fn build_audit_scheduler_payload<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    let enabled: bool = attr(py, NATIVE, "scheduler_enabled")?.call1((analysis,))?.is_truthy()?;
    let mut audits: Vec<Any<'py>> = Vec::new();
    for audit in analysis.getattr("compiled_project")?.getattr("audits")?.try_iter()? {
        let audit: Any<'py> = audit?;
        if audit.getattr("scheduled")?.is_truthy()? {
            audits.push(audit);
        }
    }
    let warehouse_now: Any<'py> = connection.call_method0("capture_warehouse_timestamp")?;
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("enabled", enabled)?;
    if audits.is_empty() {
        payload.set_item("state", schedule_state(py, if enabled { "IDLE" } else { "DISABLED" })?)?;
        payload.set_item("warehouseNow", &warehouse_now)?;
        payload.set_item("dueCount", 0)?;
        payload.set_item("audits", PyList::empty(py))?;
        return Ok(payload);
    }
    let audits_tuple: Bound<'py, PyTuple> = PyTuple::new(py, &audits)?;
    let model_names: Bound<'py, PySet> = PySet::empty(py)?;
    for audit in &audits {
        for name in audit.getattr("referenced_model_names")?.try_iter()? {
            model_names.add(name?)?;
        }
    }
    let sorted_names: Any<'py> = attr(py, "builtins", "sorted")?.call1((model_names,))?;
    let anchors: Any<'py> = attr(py, "sqltide.executor.auditing.main._load_model_anchors", "load_model_anchors")?.call(
        (),
        Some(&py_kwargs!(py;
            client = connection,
            metadata_database = database,
            target_database = database,
            model_names = PyTuple::new(py, sorted_names.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
            virtual_environments = analysis.getattr("compile_inputs")?.getattr("virtual_environments")?,
        )?),
    )?;
    let relation_names: Any<'py> = analysis.getattr("realized_project")?.getattr("relation_name_by_logical_key")?;
    let relation_name_by_model: Bound<'py, PyDict> = PyDict::new(py);
    for model in analysis.getattr("compiled_project")?.getattr("models")?.try_iter()? {
        let key: Any<'py> = model?.getattr("key")?;
        relation_name_by_model.set_item(key.getattr("name")?, relation_names.get_item(&key)?)?;
    }
    let materialized: Any<'py> = attr(
        py,
        "sqltide.executor.auditing.main._load_materialized_model_names",
        "load_materialized_model_names",
    )?
    .call((), Some(&py_kwargs!(py; client = connection, database = database, relation_name_by_model = relation_name_by_model)?))?;
    let warmup_states: Any<'py> = attr(py, NATIVE, "resolve_audit_warmup_states")?.call(
        (),
        Some(&py_kwargs!(py;
            audits = &audits_tuple,
            anchors_by_model = &anchors,
            materialized_model_names = materialized,
            warehouse_now = &warehouse_now,
        )?),
    )?;
    let audit_kind: Any<'py> = member(py, "sqltide.compiler.quality.types", "QualityNodeKind", "AUDIT")?;
    let status_by_name: Bound<'py, PyDict> = PyDict::new(py);
    let statuses: Any<'py> = checks_status_payload(analysis, connection, database, project_dir)?;
    for status in statuses.try_iter()? {
        let status: Any<'py> = status?;
        if status.get_item("kind")?.eq(&audit_kind)? {
            status_by_name.set_item(status.get_item("name")?.str()?, &status)?;
        }
    }
    let entry: Any<'py> = attr(py, NATIVE, "audit_schedule_entry")?;
    let audit_states: Bound<'py, PyList> = PyList::empty(py);
    for audit in &audits {
        let name: Any<'py> = audit_name(audit)?;
        audit_states.append(entry.call(
            (),
            Some(&py_kwargs!(py;
                audit = audit,
                warmup = warmup_states.get_item(&name)?,
                status = status_by_name.call_method1("get", (&name,))?,
                warehouse_now = &warehouse_now,
                anchors = &anchors,
            )?),
        )?)?;
    }
    let materialization: Any<'py> = attr(py, NATIVE, "read_latest_direct_build_materialization")?.call(
        (),
        Some(&py_kwargs!(py;
            connection = connection,
            database = database,
            project_identity = logical_project_identity(project_dir)?,
        )?),
    )?;
    let blocked: bool = materialization.eq(member(py, OBSERVABILITY_TYPES, "MaterializationOutcome", "FAILED")?)?;
    if blocked {
        let not_materialized: Any<'py> = schedule_state(py, "NOT_MATERIALIZED")?;
        for audit_state in audit_states.iter() {
            if audit_state.get_item("state")?.eq(&not_materialized)? {
                continue;
            }
            audit_state.set_item("state", schedule_state(py, "BLOCKED")?)?;
            audit_state.set_item("blockedReason", "failed_build")?;
        }
    }
    let due: Any<'py> = schedule_state(py, "DUE")?;
    let mut due_count: usize = 0;
    for item in audit_states.iter() {
        if item.get_item("state")?.eq(&due)? {
            due_count += 1;
        }
    }
    let state: &str = match (enabled, blocked, due_count) {
        (false, _, _) => "DISABLED",
        (true, true, _) => "BLOCKED",
        (true, false, 0) => "IDLE",
        (true, false, _) => "DUE",
    };
    payload.set_item("state", schedule_state(py, state)?)?;
    payload.set_item("warehouseNow", &warehouse_now)?;
    payload.set_item("dueCount", due_count)?;
    payload.set_item("audits", audit_states)?;
    Ok(payload)
}

fn checks_status_payload<'py>(analysis: &Any<'py>, connection: &Any<'py>, database: &Any<'py>, project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    attr(py, NATIVE, "build_checks_status_payload")?.call(
        (),
        Some(&py_kwargs!(py; analysis = analysis, connection = connection, database = database, project_dir = project_dir)?),
    )
}

/// The quality status of one executed audit result.
fn audit_status<'py>(result: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = result.py();
    let name: &str = if !result.getattr("error_message")?.is_none() {
        "ERROR"
    } else if result.getattr("passed")?.is_truthy()? {
        "PASSED"
    } else if result.getattr("severity")?.eq(member(py, AUDITING_TYPES, "AuditSeverity", "WARNING")?)? {
        "WARNING"
    } else {
        "FAILED"
    };
    member(py, AUDITING_TYPES, "QualityResultStatus", name)
}

fn scheduled_context<'py>(audit: &Any<'py>, scheduled_for: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = audit.py();
    attr(py, OBSERVABILITY_MODELS, "QualityResultContext")?.call(
        (),
        Some(&py_kwargs!(py;
            trigger = member(py, OBSERVABILITY_TYPES, "QualityResultTrigger", "SCHEDULED")?,
            scheduled_for = scheduled_for,
            cadence_seconds = audit.getattr("cadence_seconds")?,
            warmup_seconds = audit.getattr("warmup_seconds")?,
        )?),
    )
}

fn node_result<'py>(invocation: &Any<'py>, audit: &Any<'py>, scheduled_for: &Any<'py>, fields: Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let py: Python<'py> = audit.py();
    let identity: Any<'py> = attr(py, NATIVE, "require_quality_identity")?.call1((audit.getattr("quality_identity")?,))?;
    let kwargs = py_kwargs!(py; invocation = invocation, identity = identity, context = scheduled_context(audit, scheduled_for)?)?;
    kwargs.update(fields.as_mapping())?;
    attr(py, "sqltide.executor.observability.main.build_node_result_record", "build_node_result_record")?.call((), Some(&kwargs))
}

pub(super) fn terminal_invocation<'py>(started: &Any<'py>, terminal: Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let py: Python<'py> = started.py();
    let terminal: Any<'py> = attr(py, OBSERVABILITY_MODELS, "TerminalInvocation")?.call((), Some(&terminal))?;
    attr(py, "sqltide.executor.observability.main.build_invocation_record", "build_invocation_record")?
        .call((), Some(&py_kwargs!(py; started = started, terminal = terminal)?))
}

pub(super) fn persist<'py>(connection: &Any<'py>, database: &Any<'py>, invocation: &Any<'py>, node_results: &Bound<'py, PyTuple>) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    attr(py, "sqltide.executor.observability.main.persist_terminal_observations", "persist_terminal_observations")?.call(
        (),
        Some(&py_kwargs!(py; client = connection, database = database, invocation = invocation, node_results = node_results)?),
    )
}

fn persistence_error<'py>(py: Python<'py>, message: &Any<'py>) -> PyResult<Any<'py>> {
    attr(py, DEV_EXCEPTIONS, "AuditSchedulerPersistenceError")?.call1((message,))
}

/// Execute one sequential scheduled batch and persist its typed observations.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, observation_connection = None, database, project_dir, due))]
pub(crate) fn execute_due_audits<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    observation_connection: Option<Any<'py>>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
    due: &Any<'py>,
) -> PyResult<usize> {
    let py: Python<'py> = analysis.py();
    let scheduled_for_by_name: Bound<'py, PyDict> = PyDict::new(py);
    for item in due.try_iter()? {
        let item: Any<'py> = item?;
        scheduled_for_by_name.set_item(item.get_item("name")?.str()?, item.get_item("scheduledFor")?.str()?)?;
    }
    let mut audits: Vec<Any<'py>> = Vec::new();
    for audit in analysis.getattr("compiled_project")?.getattr("audits")?.try_iter()? {
        let audit: Any<'py> = audit?;
        if scheduled_for_by_name.contains(audit_name(&audit)?)? {
            audits.push(audit);
        }
    }
    if audits.is_empty() {
        return Ok(0);
    }
    let count: usize = audits.len();
    let scheduled = member(py, OBSERVABILITY_TYPES, "QualityResultTrigger", "SCHEDULED")?;
    let started: Any<'py> =
        attr(py, "sqltide.executor.observability.main.start_invocation", "start_invocation")?.call0()?;
    let event_sink: Option<Any<'py>> = match observation_connection.filter(|connection| !connection.is_none()) {
        None => None,
        Some(observation) => Some(attr(py, "sqltide.executor.observability.classes.run_event_sink", "RunEventSink")?.call(
            (),
            Some(&py_kwargs!(py;
                connection = observation,
                database = database,
                invocation_id = started.get_item(0)?,
                project_identity = project_dir.getattr("name")?,
            )?),
        )?),
    };
    if let Some(sink) = &event_sink {
        sink.call_method(
            "run_started",
            (),
            Some(&py_kwargs!(py; command = "audit", mode = &scheduled, total_statements = count, selected_node_count = count)?),
        )?;
    }
    let executed: PyResult<(Any<'py>, Vec<Any<'py>>)> = (|| {
        let mut audit_results: Vec<Any<'py>> = Vec::new();
        for audit in &audits {
            let name: Any<'py> = audit_name(audit)?;
            if let Some(sink) = &event_sink {
                sink.call_method("audit_started", (), Some(&py_kwargs!(py; name = &name)?))?;
            }
            let relation_names: Any<'py> = analysis.getattr("realized_project")?.getattr("relation_name_by_logical_key")?;
            let resolver: Bound<'py, PyDict> = PyDict::new(py);
            for model in analysis.getattr("compiled_project")?.getattr("models")?.try_iter()? {
                let key: Any<'py> = model?.getattr("key")?;
                let relation: Any<'py> = fstring(py, &[database.clone(), literal(py, "."), relation_names.get_item(&key)?])?;
                resolver.set_item(key.getattr("name")?, relation)?;
            }
            let audit_result: Any<'py> = attr(py, "sqltide.executor.auditing.main.execute_sql_audits", "execute_sql_audits")?
                .call(
                    (),
                    Some(&py_kwargs!(py;
                        loaded_audits = PyTuple::new(py, [audit])?,
                        resolver = resolver,
                        client = connection,
                        dialect = analysis.getattr("adapter_profile")?.getattr("sql_analysis_dialect")?,
                    )?),
                )?
                .getattr("audit_results")?
                .get_item(0)?;
            audit_results.push(audit_result.clone());
            if let Some(sink) = &event_sink {
                sink.call_method(
                    "audit_completed",
                    (),
                    Some(&py_kwargs!(py;
                        name = &name,
                        status = audit_status(&audit_result)?,
                        failure_count = audit_result.getattr("failing_row_count")?,
                        error_message = audit_result.getattr("error_message")?,
                    )?),
                )?;
            }
        }
        let result: Any<'py> = attr(py, "sqltide.executor.auditing.models", "SqlAuditRunResult")?
            .call((), Some(&py_kwargs!(py; audit_results = PyTuple::new(py, &audit_results)?)?))?;
        Ok((result, audit_results))
    })();
    let (result, audit_results) = match executed {
        Ok(executed) => executed,
        Err(error) if error.is_instance_of::<PyException>(py) => {
            return match record_failed_batch(py, &error, &audits, &event_sink, &started, &scheduled_for_by_name, connection, database, project_dir)
            {
                Ok(count) => Ok(count),
                Err(raised) => Err(while_handling(py, raised, &error)),
            };
        }
        Err(error) => return Err(error),
    };
    let mut execution_error_count: usize = 0;
    for audit_result in &audit_results {
        if !audit_result.getattr("error_message")?.is_none() {
            execution_error_count += 1;
        }
    }
    let failed: bool = result.getattr("error_failure_count")?.is_truthy()? || execution_error_count > 0;
    let exit_code: i64 = i64::from(failed);
    let outcome: &str = if failed { "failed" } else { "succeeded" };
    if let Some(sink) = &event_sink {
        sink.call_method(
            "run_completed",
            (),
            Some(&py_kwargs!(py; outcome = outcome, exit_code = exit_code, error_message = py.None())?),
        )?;
    }
    let summary: Bound<'py, PyDict> = PyDict::new(py);
    summary.set_item("trigger", "scheduled")?;
    summary.set_item("scheduled_count", count)?;
    summary.set_item("warning_failure_count", result.getattr("warning_failure_count")?)?;
    summary.set_item("error_failure_count", result.getattr("error_failure_count")?)?;
    summary.set_item("execution_error_count", execution_error_count)?;
    let invocation: Any<'py> = terminal_invocation(
        &started,
        py_kwargs!(py;
            project_dir = project_dir,
            target_identity = database,
            command = "audit",
            mode = &scheduled,
            outcome = outcome,
            exit_code = exit_code,
            materialized_outcome = py.None(),
            deployment_id = py.None(),
            workflow_id = py.None(),
            selected_node_count = count,
            error_message = py.None(),
            summary = summary,
        )?,
    )?;
    let mut node_results: Vec<Any<'py>> = Vec::new();
    for (audit, audit_result) in audits.iter().zip(&audit_results) {
        let sample_rows: Bound<'py, PyList> = PyList::empty(py);
        for row in audit_result.getattr("sample_rows")?.get_item(PySlice::new(py, 0, 5, 1))?.try_iter()? {
            sample_rows.append(attr(py, "builtins", "list")?.call1((row?,))?)?;
        }
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("sample_column_names", attr(py, "builtins", "list")?.call1((audit_result.getattr("sample_column_names")?,))?)?;
        payload.set_item("sample_rows", sample_rows)?;
        node_results.push(node_result(
            &invocation,
            audit,
            &scheduled_for_by_name.get_item(audit_name(audit)?)?.expect("selected audits are scheduled"),
            py_kwargs!(py;
                status = audit_status(audit_result)?,
                severity = audit_result.getattr("severity")?,
                failure_count = audit_result.getattr("failing_row_count")?,
                payload = payload,
                error_message = audit_result.getattr("error_message")?,
            )?,
        )?);
    }
    let node_results: Bound<'py, PyTuple> = PyTuple::new(py, node_results)?;
    let persistence: Any<'py> = persist(connection, database, &invocation, &node_results)?;
    if !persistence.is_none() {
        return Err(PyErr::from_value(persistence_error(py, &persistence)?));
    }
    Ok(node_results.len())
}

/// The `except Exception` branch of one batch: mark every selected slot attempted.
#[allow(clippy::too_many_arguments)]
fn record_failed_batch<'py>(
    py: Python<'py>,
    error: &PyErr,
    audits: &[Any<'py>],
    event_sink: &Option<Any<'py>>,
    started: &Any<'py>,
    scheduled_for_by_name: &Bound<'py, PyDict>,
    connection: &Any<'py>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<usize> {
    let raised: Any<'py> = error.value(py).clone().into_any();
    let message: Bound<'py, PyString> = raised.str()?;
    if let Some(sink) = event_sink {
        sink.call_method(
            "run_completed",
            (),
            Some(&py_kwargs!(py; outcome = "failed", exit_code = 1, error_message = &message)?),
        )?;
    }
    let summary: Bound<'py, PyDict> = PyDict::new(py);
    summary.set_item("trigger", "scheduled")?;
    summary.set_item("error_count", audits.len())?;
    let invocation: Any<'py> = terminal_invocation(
        started,
        py_kwargs!(py;
            project_dir = project_dir,
            target_identity = database,
            command = "audit",
            mode = member(py, OBSERVABILITY_TYPES, "QualityResultTrigger", "SCHEDULED")?,
            outcome = "failed",
            exit_code = 1,
            materialized_outcome = py.None(),
            deployment_id = py.None(),
            workflow_id = py.None(),
            selected_node_count = audits.len(),
            error_message = &message,
            summary = summary,
        )?,
    )?;
    let mut error_results: Vec<Any<'py>> = Vec::new();
    for audit in audits {
        let batch_failure: Bound<'py, PyDict> = PyDict::new(py);
        batch_failure.set_item("batch_failure", true)?;
        error_results.push(node_result(
            &invocation,
            audit,
            &scheduled_for_by_name.get_item(audit_name(audit)?)?.expect("selected audits are scheduled"),
            py_kwargs!(py;
                status = member(py, AUDITING_TYPES, "QualityResultStatus", "ERROR")?,
                severity = audit.getattr("severity")?,
                failure_count = 1,
                payload = batch_failure,
                error_message = raised.str()?,
            )?,
        )?);
    }
    let error_results: Bound<'py, PyTuple> = PyTuple::new(py, error_results)?;
    let persistence: Any<'py> = persist(connection, database, &invocation, &error_results)?;
    if !persistence.is_none() {
        return Err(raise_from(&persistence_error(py, &persistence)?, &raised, Some(&raised))?);
    }
    Ok(error_results.len())
}

/// Process-local scheduler health, read and written only without calling into Python.
struct Health {
    state: Py<PyAny>,
    consecutive_errors: u32,
    backoff_until: f64,
    latest_error: Option<Py<PyAny>>,
    last_successful_tick: Option<Py<PyAny>>,
    running_audit_count: usize,
}

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// `max(a, b)` as Python returns it: `a` unless `b` is greater.
fn python_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Poll current compile and metadata state, then run one due batch at a time.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct AuditScheduler {
    state: Py<PyAny>,
    warehouse: Py<PyAny>,
    database: Option<Py<PyAny>>,
    project_dir: Py<PyAny>,
    builds: Py<PyAny>,
    presumed_failed_after_seconds: Py<PyAny>,
    stop: Py<PyAny>,
    tick_lock: Mutex<()>,
    thread: Mutex<Option<Py<PyAny>>>,
    health: Mutex<Health>,
}

impl AuditScheduler {
    fn monotonic(py: Python<'_>) -> PyResult<f64> {
        attr(py, "time", "monotonic")?.call0()?.extract()
    }

    fn seconds_until_attempt(&self, py: Python<'_>) -> PyResult<f64> {
        let now: f64 = Self::monotonic(py)?;
        Ok(python_max(guard(&self.health).backoff_until - now, 0.0))
    }

    fn record_error(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        let errors: u32 = guard(&self.health).consecutive_errors + 1;
        let delay: f64 = (POLL_SECONDS * 2_f64.powi(i32::try_from(errors - 1).unwrap_or(i32::MAX))).min(MAX_BACKOFF_SECONDS);
        let backoff_until: f64 = Self::monotonic(py)? + delay;
        let message: Py<PyAny> = error.value(py).str()?.into_any().unbind();
        let backing_off: Py<PyAny> = schedule_state(py, "BACKING_OFF")?.unbind();
        let mut health = guard(&self.health);
        health.consecutive_errors = errors;
        health.backoff_until = backoff_until;
        health.latest_error = Some(message);
        health.state = backing_off;
        Ok(())
    }

    fn record_success(&self, py: Python<'_>, state: &str, completed_at: Option<Any<'_>>) -> PyResult<()> {
        let state: Py<PyAny> = schedule_state(py, state)?.unbind();
        {
            let mut health = guard(&self.health);
            health.consecutive_errors = 0;
            health.backoff_until = 0.0;
            health.latest_error = None;
            health.state = state;
        }
        // The fields above are reset before the tick time is read, as the Python did.
        let completed_at: Any<'_> = match completed_at.filter(|value| value.is_truthy().unwrap_or(true)) {
            Some(value) => value,
            None => {
                let now: String = attr(py, "datetime", "datetime")?
                    .call_method1("now", (attr(py, "datetime", "UTC")?,))?
                    .call_method1("strftime", ("%Y-%m-%d %H:%M:%S.%f",))?
                    .extract()?;
                PyString::new(py, &now[..now.len() - 3]).into_any()
            }
        };
        guard(&self.health).last_successful_tick = Some(completed_at.unbind());
        Ok(())
    }

    fn record_blocked(&self, py: Python<'_>, warning: Any<'_>) -> PyResult<()> {
        self.record_success(py, "BLOCKED", None)?;
        guard(&self.health).latest_error = Some(warning.unbind());
        Ok(())
    }

    fn set_running_audit_count(&self, py: Python<'_>, count: usize) -> PyResult<()> {
        let running: Py<PyAny> = schedule_state(py, "RUNNING")?.unbind();
        let mut health = guard(&self.health);
        health.running_audit_count = count;
        if count > 0 {
            health.state = running;
        }
        Ok(())
    }

    fn database<'py>(&self, py: Python<'py>) -> Option<Any<'py>> {
        self.database.as_ref().map(|database| database.bind(py).clone())
    }

    /// `local build {invocationId or 'unknown'} is running` when a local build runs.
    fn local_build_warning<'py>(&self, py: Python<'py>) -> PyResult<Option<Any<'py>>> {
        let feed: Any<'py> = self.builds.bind(py).call_method("feed", (), Some(&py_kwargs!(py; after = 0)?))?;
        if !feed.get_item("running")?.is_truthy()? {
            return Ok(None);
        }
        let invocation: Any<'py> = feed.call_method1("get", ("invocationId",))?;
        let invocation: Any<'py> = if invocation.is_truthy()? { invocation } else { literal(py, "unknown") };
        Ok(Some(fstring(py, &[literal(py, "local build "), invocation, literal(py, " is running")])?))
    }

    fn active_runs<'py>(&self, py: Python<'py>, connection: &Any<'py>, database: &Any<'py>) -> PyResult<Any<'py>> {
        attr(py, NATIVE, "read_active_runs")?.call(
            (),
            Some(&py_kwargs!(py;
                connection = connection,
                database = database,
                presumed_failed_after_seconds = &self.presumed_failed_after_seconds,
            )?),
        )
    }

    fn live_statuses(py: Python<'_>) -> PyResult<Bound<'_, PySet>> {
        let running = member(py, DEV_TYPES, "RunPresentationStatus", "RUNNING")?;
        let unresponsive = member(py, DEV_TYPES, "RunPresentationStatus", "UNRESPONSIVE")?;
        PySet::new(py, [running, unresponsive])
    }

    fn blocking_mutating_build<'py>(&self, py: Python<'py>) -> PyResult<Option<Any<'py>>> {
        let connection: Any<'py> = self.warehouse.bind(py).getattr("connection")?;
        let Some(database) = self.database(py).filter(|_| !connection.is_none()) else {
            return Ok(None);
        };
        let project_identity: Any<'py> = logical_project_identity(self.project_dir.bind(py))?;
        let latest_applied_at: Any<'py> = attr(py, NATIVE, "read_latest_applied_direct_build_at")?.call(
            (),
            Some(&py_kwargs!(py; connection = &connection, database = &database, project_identity = &project_identity)?),
        )?;
        let build = member(py, "sqltide.cli.entry.types", "CliCommand", "BUILD")?;
        let presumed_failed = member(py, DEV_TYPES, "RunPresentationStatus", "PRESUMED_FAILED")?;
        let live: Bound<'py, PySet> = Self::live_statuses(py)?;
        for run in self.active_runs(py, &connection, &database)?.try_iter()? {
            let run: Any<'py> = run?;
            if run.get_item("command")?.ne(&build)? {
                continue;
            }
            let run_project_identity: Any<'py> = run.call_method1("get", ("projectIdentity",))?;
            if !run_project_identity.is_none() {
                let last: Any<'py> = run_project_identity
                    .str()?
                    .call_method("rsplit", ("/",), Some(&py_kwargs!(py; maxsplit = 1)?))?
                    .get_item(-1)?;
                if last.ne(&project_identity)? {
                    continue;
                }
            }
            let status: Any<'py> = run.get_item("status")?;
            if live.contains(&status)? {
                return Ok(Some(run));
            }
            if status.eq(&presumed_failed)?
                && (latest_applied_at.is_none() || run.get_item("startedAt")?.str()?.ge(&latest_applied_at)?)
            {
                return Ok(Some(run));
            }
        }
        Ok(None)
    }

    fn has_active_scheduled_audit(&self, py: Python<'_>) -> PyResult<bool> {
        let connection: Any<'_> = self.warehouse.bind(py).getattr("connection")?;
        let Some(database) = self.database(py).filter(|_| !connection.is_none()) else {
            return Ok(false);
        };
        let audit = member(py, "sqltide.cli.entry.types", "CliCommand", "AUDIT")?;
        let scheduled = member(py, OBSERVABILITY_TYPES, "QualityResultTrigger", "SCHEDULED")?;
        let live: Bound<'_, PySet> = Self::live_statuses(py)?;
        for run in self.active_runs(py, &connection, &database)?.try_iter()? {
            let run: Any<'_> = run?;
            if run.get_item("command")?.eq(&audit)?
                && run.get_item("mode")?.eq(&scheduled)?
                && live.contains(run.get_item("status")?)?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn tick_once(&self, py: Python<'_>) -> PyResult<usize> {
        let connection: Any<'_> = self.warehouse.bind(py).getattr("connection")?;
        let Some(database) = self.database(py).filter(|_| !connection.is_none()) else {
            self.record_success(py, "DISABLED", None)?;
            return Ok(0);
        };
        if let Some(warning) = self.local_build_warning(py)? {
            self.record_blocked(py, warning)?;
            return Ok(0);
        }
        let _ticking = match self.tick_lock.try_lock() {
            Ok(ticking) => ticking,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(0),
        };
        let result: PyResult<usize> = within(&self.state.bind(py).getattr("query_lock")?, || {
            self.locked_tick(py, &connection, &database)
        });
        self.set_running_audit_count(py, 0)?;
        result
    }

    fn locked_tick(&self, py: Python<'_>, connection: &Any<'_>, database: &Any<'_>) -> PyResult<usize> {
        if let Some(warning) = self.local_build_warning(py)? {
            self.record_blocked(py, warning)?;
            return Ok(0);
        }
        let analysis: Any<'_> = self.state.bind(py).call_method0("current_analysis")?;
        if !attr(py, NATIVE, "scheduler_enabled")?.call1((&analysis,))?.is_truthy()? {
            self.record_success(py, "DISABLED", None)?;
            return Ok(0);
        }
        if let Some(blocking) = self.blocking_mutating_build(py)? {
            let invocation: Any<'_> = blocking.call_method1("get", ("invocationId",))?;
            let invocation: Any<'_> = if invocation.is_truthy()? { invocation } else { literal(py, "unknown") };
            let warning: Any<'_> =
                fstring(py, &[literal(py, "build "), invocation, literal(py, " is "), blocking.get_item("status")?])?;
            self.record_blocked(py, warning)?;
            return Ok(0);
        }
        if self.has_active_scheduled_audit(py)? {
            self.record_blocked(py, literal(py, "another scheduler process has an active scheduled audit run"))?;
            return Ok(0);
        }
        let project_dir: &Bound<'_, PyAny> = self.project_dir.bind(py);
        let payload: Any<'_> = attr(py, NATIVE, "build_audit_scheduler_payload")?.call(
            (),
            Some(&py_kwargs!(py; analysis = &analysis, connection = connection, database = database, project_dir = project_dir)?),
        )?;
        if !payload.get_item("enabled")?.is_truthy()? {
            self.record_success(py, "DISABLED", None)?;
            return Ok(0);
        }
        let raw_audits: Any<'_> = payload.get_item("audits")?;
        let due_state: Any<'_> = schedule_state(py, "DUE")?;
        let mut due: Vec<Any<'_>> = Vec::new();
        if raw_audits.is_instance_of::<PyList>() {
            for item in raw_audits.try_iter()? {
                let item: Any<'_> = item?;
                let Ok(item) = item.cast::<PyDict>() else {
                    continue;
                };
                let typed_item: Bound<'_, PyDict> = PyDict::new(py);
                for (key, value) in item.iter() {
                    typed_item.set_item(key.str()?, value)?;
                }
                if typed_item.call_method1("get", ("state",))?.eq(&due_state)? {
                    due.push(typed_item.into_any());
                }
            }
        }
        let payload_state: Any<'_> = attr(py, DEV_TYPES, "AuditScheduleState")?.call1((payload.get_item("state")?.str()?,))?;
        if due.is_empty() {
            let completed_at: Any<'_> = payload.get_item("warehouseNow")?.str()?.into_any();
            self.record_success_with(py, payload_state, Some(completed_at))?;
            return Ok(0);
        }
        self.set_running_audit_count(py, due.len())?;
        let result: Any<'_> = attr(py, NATIVE, "execute_due_audits")?.call(
            (),
            Some(&py_kwargs!(py;
                analysis = &analysis,
                connection = connection,
                observation_connection = self.warehouse.bind(py).getattr("observation_connection")?,
                database = database,
                project_dir = project_dir,
                due = PyTuple::new(py, due)?,
            )?),
        )?;
        self.record_success(py, "IDLE", None)?;
        result.extract()
    }

    fn record_success_with(&self, py: Python<'_>, state: Any<'_>, completed_at: Option<Any<'_>>) -> PyResult<()> {
        self.record_success(py, "IDLE", completed_at)?;
        guard(&self.health).state = state.unbind();
        Ok(())
    }
}

#[pymethods]
impl AuditScheduler {
    #[new]
    #[pyo3(signature = (
        *,
        state,
        warehouse = None,
        connection = None,
        observation_connection = None,
        database,
        project_dir,
        builds,
        presumed_failed_after_seconds = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        state: Any<'_>,
        warehouse: Option<Any<'_>>,
        connection: Option<Any<'_>>,
        observation_connection: Option<Any<'_>>,
        database: Option<Any<'_>>,
        project_dir: Any<'_>,
        builds: Any<'_>,
        presumed_failed_after_seconds: Option<Any<'_>>,
    ) -> PyResult<Self> {
        let warehouse: Any<'_> = match warehouse.filter(|warehouse| warehouse.is_truthy().unwrap_or(true)) {
            Some(warehouse) => warehouse,
            None => attr(py, NATIVE, "WarehouseRuntime")?.call(
                (),
                Some(&py_kwargs!(py;
                    connection = connection,
                    observation_connection = observation_connection,
                    connection_factory = py.None(),
                    observation_connection_factory = py.None(),
                    database = &database,
                )?),
            )?,
        };
        let presumed_failed_after_seconds: Any<'_> = match presumed_failed_after_seconds {
            Some(seconds) => seconds,
            None => attr(py, "sqltide.compiler.discovery.constants", "DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS")?,
        };
        let database: Option<Any<'_>> = database.filter(|database| !database.is_none());
        let initial: &str =
            if !warehouse.getattr("connection")?.is_none() && database.is_some() { "IDLE" } else { "DISABLED" };
        Ok(Self {
            state: state.unbind(),
            warehouse: warehouse.unbind(),
            database: database.map(Bound::unbind),
            project_dir: project_dir.unbind(),
            builds: builds.unbind(),
            presumed_failed_after_seconds: presumed_failed_after_seconds.unbind(),
            stop: attr(py, "threading", "Event")?.call0()?.unbind(),
            tick_lock: Mutex::new(()),
            thread: Mutex::new(None),
            health: Mutex::new(Health {
                state: schedule_state(py, initial)?.unbind(),
                consecutive_errors: 0,
                backoff_until: 0.0,
                latest_error: None,
                last_successful_tick: None,
                running_audit_count: 0,
            }),
        })
    }

    /// Start the daemon polling loop once.
    fn start(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        if guard(&slf.get().thread).is_some() {
            return Ok(());
        }
        let thread: Any<'_> = attr(py, "threading", "Thread")?.call(
            (),
            Some(&py_kwargs!(py; target = slf.getattr("_run")?, name = "sqltide-audits", daemon = true)?),
        )?;
        *guard(&slf.get().thread) = Some(thread.clone().unbind());
        thread.call_method0("start")?;
        Ok(())
    }

    /// Stop polling and wait briefly for the current tick.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.stop.bind(py).call_method0("set")?;
        let thread: Option<Any<'_>> = guard(&self.thread).as_ref().map(|thread| thread.bind(py).clone());
        if let Some(thread) = thread {
            thread.call_method0("join")?;
        }
        Ok(())
    }

    /// Run at most one due batch, returning its terminal result count.
    fn tick(&self, py: Python<'_>) -> PyResult<usize> {
        if self.seconds_until_attempt(py)? > 0.0 {
            return Ok(0);
        }
        match self.tick_once(py) {
            Err(error) if error.is_instance_of::<PyException>(py) => {
                self.record_error(py, &error)?;
                Err(error)
            }
            other => other,
        }
    }

    /// Return the process-local scheduler lifecycle and backoff state.
    fn health<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let backoff_seconds: f64 = self.seconds_until_attempt(py)?;
        let next_tick_seconds: f64 = python_max(self.seconds_until_attempt(py)?, POLL_SECONDS);
        let health = guard(&self.health);
        let state: Py<PyAny> = health.state.clone_ref(py);
        let consecutive_errors: u32 = health.consecutive_errors;
        let latest_error: Option<Py<PyAny>> = health.latest_error.as_ref().map(|error| error.clone_ref(py));
        let last_successful_tick: Option<Py<PyAny>> = health.last_successful_tick.as_ref().map(|tick| tick.clone_ref(py));
        let running_audit_count: usize = health.running_audit_count;
        drop(health);
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("state", state)?;
        payload.set_item("consecutiveErrors", consecutive_errors)?;
        payload.set_item("latestError", latest_error)?;
        payload.set_item("backoffSeconds", backoff_seconds)?;
        payload.set_item("nextTickSeconds", next_tick_seconds)?;
        payload.set_item("lastSuccessfulTick", last_successful_tick)?;
        payload.set_item("runningAuditCount", running_audit_count)?;
        Ok(payload)
    }

    #[pyo3(name = "_run")]
    fn run(&self, py: Python<'_>) -> PyResult<()> {
        let stop: &Bound<'_, PyAny> = self.stop.bind(py);
        while !stop.call_method0("is_set")?.is_truthy()? {
            if let Err(error) = self.tick(py)
                && !error.is_instance_of::<PyException>(py)
            {
                return Err(error);
            }
            stop.call_method1("wait", (python_max(POLL_SECONDS, self.seconds_until_attempt(py)?),))?;
        }
        Ok(())
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<AuditScheduler>()?;
    module.add_function(wrap_pyfunction!(scheduler_enabled, module)?)?;
    module.add_function(wrap_pyfunction!(sensors_enabled, module)?)?;
    module.add_function(wrap_pyfunction!(build_audit_scheduler_payload, module)?)?;
    module.add_function(wrap_pyfunction!(execute_due_audits, module)?)
}
