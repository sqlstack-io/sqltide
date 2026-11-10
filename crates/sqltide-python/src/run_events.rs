//! Run observability: streamed run events, terminal invocation and node-result records, and
//! their best-effort persistence through the observation workflow.

use std::sync::Mutex;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const CONSTANTS: &str = "sqltide.executor.observability.constants";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const WORKFLOW_EXCEPTIONS: &str = "sqltide.executor.workflow.exceptions";
const OBSERVATION_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_observation_workflow";
const BUILD_CONSTANTS: &str = "sqltide.cli.build.constants";
const NATIVE: &str = "sqltide._native";
const COMPLETE_SUMMARY_COMMANDS: [&str; 2] = ["destroy pipelines", "reset target"];
const TIMESTAMP_FORMAT: &str = "%Y-%m-%d %H:%M:%S.%f";
const RETAINED_PAYLOAD_KEYS: [&str; 2] = ["missing_count", "unexpected_count"];

type Any<'py> = Bound<'py, PyAny>;

fn constant<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, CONSTANTS, name)
}

/// `json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)`, or without
/// `default` when `default_str` is false.
fn compact_json(value: &Any<'_>, default_str: bool) -> PyResult<String> {
    let py: Python<'_> = value.py();
    let separators: Bound<'_, PyTuple> = PyTuple::new(py, [",", ":"])?;
    let arguments: Bound<'_, PyDict> = py_kwargs!(py; sort_keys = true, separators = separators)?;
    if default_str {
        arguments.set_item("default", py.get_type::<PyString>())?;
    }
    attr(py, "json", "dumps")?
        .call((value,), Some(&arguments))?
        .extract()
}

/// A deterministic payload, replaced by a size marker when it exceeds the observation limit.
fn bounded_json(payload: &Any<'_>) -> PyResult<String> {
    let py: Python<'_> = payload.py();
    let rendered: String = compact_json(payload, true)?;
    let limit: usize = constant(py, "MAX_OBSERVATION_JSON_BYTES")?.extract()?;
    if rendered.len() <= limit {
        return Ok(rendered);
    }
    let retained: Bound<'_, PyDict> = PyDict::new(py);
    for key in RETAINED_PAYLOAD_KEYS {
        if payload.contains(key)? {
            retained.set_item(key, payload.get_item(key)?)?;
        }
    }
    retained.set_item("payload_truncated", true)?;
    retained.set_item("original_bytes", rendered.len())?;
    compact_json(retained.as_any(), false)
}

/// One persisted error bounded to the observation limit, `None` kept.
fn concise_error<'py>(message: &Any<'py>) -> PyResult<Any<'py>> {
    if message.is_none() {
        return Ok(message.clone());
    }
    let py: Python<'py> = message.py();
    let limit: isize = constant(py, "MAX_OBSERVATION_ERROR_LENGTH")?.extract()?;
    message.get_item(PySlice::new(py, 0, limit, 1))
}

/// The current UTC time with millisecond precision, as SQLTide records it.
fn utc_timestamp(py: Python<'_>) -> PyResult<String> {
    let now: Any<'_> =
        attr(py, "datetime", "datetime")?.call_method1("now", (attr(py, "datetime", "UTC")?,))?;
    let formatted: String = now
        .call_method1("strftime", (TIMESTAMP_FORMAT,))?
        .extract()?;
    Ok(formatted[..formatted.len() - 3].to_owned())
}

fn monotonic_ns(py: Python<'_>) -> PyResult<i128> {
    attr(py, "time", "monotonic_ns")?.call0()?.extract()
}

/// Adapter-rendered observation SQL as numbered finalization mutations.
pub(crate) fn observation_statements<'py>(rendered: &Any<'py>) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = rendered.py();
    let statement: Any<'py> = attr(py, WORKFLOW_MODELS, "WarehouseStatement")?;
    let finalization: Any<'py> =
        attr(py, WORKFLOW_TYPES, "WorkflowPhase")?.getattr("FINALIZATION")?;
    let mutation: Any<'py> = attr(py, WORKFLOW_TYPES, "StatementIntent")?.getattr("MUTATION")?;
    let mut statements: Vec<Any<'py>> = Vec::new();
    for (index, sql) in rendered.try_iter()?.enumerate() {
        let sequence: usize = index + 1;
        statements.push(statement.call(
            (),
            Some(&py_kwargs!(py;
                sequence = sequence,
                step_id = format!("record_terminal_observation_{sequence}"),
                phase = &finalization,
                intent = &mutation,
                sql = sql?,
            )?),
        )?);
    }
    PyTuple::new(py, statements)
}

/// Run assembled observation statements through the mutation gateway.
pub(crate) fn execute_observations(
    connection: &Any<'_>,
    statements: &Bound<'_, PyTuple>,
) -> PyResult<()> {
    let py: Python<'_> = connection.py();
    attr(py, OBSERVATION_WORKFLOW, "execute_observation_workflow")?.call(
        (),
        Some(&py_kwargs!(py; statements = statements, connection = connection)?),
    )?;
    Ok(())
}

/// Capture one process-local invocation identity, wall time and monotonic start.
#[pyfunction]
#[pyo3(signature = (*, invocation_id = None))]
fn start_invocation<'py>(
    py: Python<'py>,
    invocation_id: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let started_at: String = utc_timestamp(py)?;
    let identity: Any<'py> = match invocation_id {
        Some(identity) if identity.is_truthy()? => identity,
        _ => attr(py, "uuid", "uuid4")?.call0()?.str()?.into_any(),
    };
    let started_ns: Any<'py> = attr(py, "time", "monotonic_ns")?.call0()?;
    PyTuple::new(
        py,
        [
            identity,
            PyString::new(py, &started_at).into_any(),
            started_ns,
        ],
    )
}

/// The project directory name, stable across release-specific absolute paths.
#[pyfunction]
#[pyo3(signature = (*, project_dir))]
fn logical_project_identity<'py>(project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    project_dir.call_method0("resolve")?.getattr("name")
}

/// One immutable terminal invocation row.
#[pyfunction]
#[pyo3(signature = (*, started, terminal))]
fn build_invocation_record<'py>(
    py: Python<'py>,
    started: &Any<'py>,
    terminal: &Any<'py>,
) -> PyResult<Any<'py>> {
    let completed_at: String = utc_timestamp(py)?;
    let command: Any<'py> = terminal.getattr("command")?;
    let summary: Any<'py> = terminal.getattr("summary")?;
    let complete: bool = COMPLETE_SUMMARY_COMMANDS
        .iter()
        .any(|name| command.eq(*name).unwrap_or(false));
    let summary_json: String = if complete {
        compact_json(&summary, true)?
    } else {
        bounded_json(&summary)?
    };
    let started_ns: i128 = started.get_item(2)?.extract()?;
    let duration_ms: i128 = ((monotonic_ns(py)? - started_ns).div_euclid(1_000_000)).max(0);
    let project_dir: Any<'py> = terminal.getattr("project_dir")?;
    attr(py, ADAPTER_MODELS, "AdapterInvocationRecord")?.call(
        (),
        Some(&py_kwargs!(py;
            invocation_id = started.get_item(0)?,
            project_identity = logical_project_identity(&project_dir)?,
            target_identity = terminal.getattr("target_identity")?,
            command = &command,
            mode = terminal.getattr("mode")?,
            outcome = terminal.getattr("outcome")?,
            exit_code = terminal.getattr("exit_code")?,
            materialized_outcome = terminal.getattr("materialized_outcome")?,
            deployment_id = terminal.getattr("deployment_id")?,
            workflow_id = terminal.getattr("workflow_id")?,
            selected_node_count = terminal.getattr("selected_node_count")?,
            started_at = started.get_item(1)?,
            completed_at = completed_at,
            duration_ms = duration_ms,
            error_message = concise_error(&terminal.getattr("error_message")?)?,
            summary_json = summary_json,
            tool_version = attr(py, BUILD_CONSTANTS, "SQLTIDE_TOOL_VERSION")?,
            artifact_project_dir = project_dir.call_method0("resolve")?.str()?,
        )?),
    )
}

/// One immutable bounded audit or test result row.
#[pyfunction]
#[pyo3(signature = (*, invocation, identity, context, status, severity, failure_count, payload, error_message))]
#[allow(clippy::too_many_arguments)]
fn build_node_result_record<'py>(
    py: Python<'py>,
    invocation: &Any<'py>,
    identity: &Any<'py>,
    context: &Any<'py>,
    status: &Any<'py>,
    severity: &Any<'py>,
    failure_count: &Any<'py>,
    payload: &Any<'py>,
    error_message: &Any<'py>,
) -> PyResult<Any<'py>> {
    let invocation_id: Any<'py> = invocation.getattr("invocation_id")?;
    let field = |name: &str| -> PyResult<Any<'py>> { identity.getattr(name) };
    let key: String = format!(
        "{}:{}:{}:{}:{}:{}",
        invocation_id.str()?,
        field("node_kind")?.str()?,
        field("node_name")?.str()?,
        field("binding_key")?.str()?,
        field("definition_fingerprint")?.str()?,
        field("execution_fingerprint")?.str()?,
    );
    attr(py, ADAPTER_MODELS, "AdapterNodeResultRecord")?.call(
        (),
        Some(&py_kwargs!(py;
            result_id = hex_sha256(key.as_bytes()),
            invocation_id = &invocation_id,
            node_kind = field("node_kind")?,
            node_name = field("node_name")?,
            binding_key = field("binding_key")?,
            definition_fingerprint = field("definition_fingerprint")?,
            execution_fingerprint = field("execution_fingerprint")?,
            target_identity = invocation.getattr("target_identity")?,
            trigger = context.getattr("trigger")?.str()?,
            scheduled_for = context.getattr("scheduled_for")?,
            cadence_seconds = context.getattr("cadence_seconds")?,
            warmup_seconds = context.getattr("warmup_seconds")?,
            status = status,
            severity = severity,
            failure_count = failure_count,
            completed_at = invocation.getattr("completed_at")?,
            payload_json = bounded_json(payload)?,
            error_message = concise_error(error_message)?,
        )?),
    )
}

fn hex_sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Write combined and numbered observation SQL before it executes.
fn publish_observation_artifact(
    invocation: &Any<'_>,
    statements: &Bound<'_, PyTuple>,
) -> PyResult<()> {
    let py: Python<'_> = invocation.py();
    let artifact_dir: Any<'_> = invocation.getattr("artifact_project_dir")?;
    let base: Any<'_> = if artifact_dir.is_truthy()? {
        artifact_dir
    } else {
        invocation.getattr("project_identity")?
    };
    let artifact_root: Any<'_> = attr(py, "pathlib", "Path")?
        .call1((base,))?
        .div("target")?
        .div("run")?
        .div("observations")?
        .div(invocation.getattr("invocation_id")?)?;
    let steps_root: Any<'_> = artifact_root.div("steps")?;
    steps_root.call_method(
        "mkdir",
        (),
        Some(&py_kwargs!(py; parents = true, exist_ok = true)?),
    )?;
    let utf8: Bound<'_, PyDict> = py_kwargs!(py; encoding = "utf-8")?;
    let mut combined: Vec<String> = Vec::new();
    for statement in statements.iter() {
        let sequence: i64 = statement.getattr("sequence")?.extract()?;
        let sql: Any<'_> = statement.getattr("sql")?;
        steps_root
            .div(format!(
                "{sequence:04}_{}.sql",
                statement.getattr("step_id")?.str()?
            ))?
            .call_method("write_text", (&sql,), Some(&utf8))?;
        combined.push(sql.extract()?);
    }
    artifact_root.div("workflow.sql")?.call_method(
        "write_text",
        (combined.join("\n"),),
        Some(&utf8),
    )?;
    Ok(())
}

/// Best-effort terminal persistence that cannot alter the observed command outcome.
#[pyfunction]
#[pyo3(signature = (*, client, database, invocation, node_results))]
fn persist_terminal_observations(
    py: Python<'_>,
    client: &Any<'_>,
    database: &Any<'_>,
    invocation: &Any<'_>,
    node_results: &Any<'_>,
) -> PyResult<Option<String>> {
    let persisted = || -> PyResult<()> {
        let rendered: Any<'_> = client.call_method(
            "render_terminal_observations",
            (),
            Some(&py_kwargs!(py;
                database = database,
                invocation = invocation,
                node_results = node_results,
            )?),
        )?;
        let statements: Bound<'_, PyTuple> = observation_statements(&rendered)?;
        publish_observation_artifact(invocation, &statements)?;
        execute_observations(client, &statements)
    };
    match persisted() {
        Ok(()) => Ok(None),
        Err(error)
            if error.is_instance(py, &attr(py, ADAPTER_EXCEPTIONS, "AdapterError")?)
                || error.is_instance_of::<pyo3::exceptions::PyOSError>(py)
                || error.is_instance(
                    py,
                    &attr(py, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?,
                ) =>
        {
            Ok(Some(format!(
                "Terminal observations were not recorded: {}",
                error.value(py).str()?
            )))
        }
        Err(error) => Err(error),
    }
}

/// Persist externally coordinated run events through the observation workflow.
#[pyfunction]
#[pyo3(signature = (*, connection, database, events))]
fn persist_run_events(connection: &Any<'_>, database: &Any<'_>, events: &Any<'_>) -> PyResult<()> {
    let py: Python<'_> = connection.py();
    let rendered: Any<'_> = connection.call_method(
        "render_run_events",
        (),
        Some(&py_kwargs!(py; database = database, events = events)?),
    )?;
    execute_observations(connection, &observation_statements(&rendered)?)
}

/// Mutable sink state; guards are held only for reads and assignments, never across Python calls.
struct SinkState {
    sequence: u64,
    migrated: bool,
    heartbeat_thread: Option<Py<PyAny>>,
    persistence_warning_emitted: bool,
    workflow_revision: i64,
    operation_evidence: Option<Py<PyAny>>,
}

/// Best-effort run narration: JSONL for pipes plus durable rows for readers.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct RunEventSink {
    connection: Py<PyAny>,
    database: Py<PyAny>,
    invocation_id: Py<PyAny>,
    project_identity: Py<PyAny>,
    jsonl_stream: Py<PyAny>,
    strict_persistence: bool,
    emit_lock: Py<PyAny>,
    heartbeat_stop: Py<PyAny>,
    state: Mutex<SinkState>,
}

impl RunEventSink {
    fn with_state<T>(&self, change: impl FnOnce(&mut SinkState) -> T) -> T {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        change(&mut state)
    }

    fn evidence<'py>(&self, py: Python<'py>) -> Any<'py> {
        self.with_state(|state| {
            state
                .operation_evidence
                .as_ref()
                .map(|value| value.clone_ref(py))
        })
        .map_or_else(|| py.None().into_bound(py), |value| value.into_bound(py))
    }

    fn emit(
        &self,
        py: Python<'_>,
        event_kind: &str,
        step_id: Any<'_>,
        phase: Any<'_>,
        payload: Bound<'_, PyDict>,
        complete_payload: bool,
    ) -> PyResult<()> {
        let lock: &Any<'_> = self.emit_lock.bind(py);
        lock.call_method0("acquire")?;
        let emitted = || -> PyResult<()> {
            let sequence: u64 = self.with_state(|state| {
                state.sequence += 1;
                state.sequence
            });
            let payload_json: String = if complete_payload {
                compact_json(payload.as_any(), true)?
            } else {
                bounded_json(payload.as_any())?
            };
            let record: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterRunEventRecord")?.call(
                (),
                Some(&py_kwargs!(py;
                    invocation_id = self.invocation_id.bind(py),
                    sequence = sequence,
                    event_kind = event_kind,
                    step_id = &step_id,
                    phase = &phase,
                    payload_json = payload_json,
                )?),
            )?;
            self.write_jsonl(py, &record, &payload)?;
            self.persist(py, &record)
        };
        let outcome: PyResult<()> = emitted();
        lock.call_method0("release")?;
        outcome
    }

    fn write_jsonl(
        &self,
        py: Python<'_>,
        record: &Any<'_>,
        payload: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let stream: &Any<'_> = self.jsonl_stream.bind(py);
        if stream.is_none() {
            return Ok(());
        }
        let written = || -> PyResult<()> {
            let line: Bound<'_, PyDict> = PyDict::new(py);
            line.set_item("event", record.getattr("event_kind")?)?;
            line.set_item("invocationId", record.getattr("invocation_id")?)?;
            line.set_item("sequence", record.getattr("sequence")?)?;
            line.set_item("stepId", record.getattr("step_id")?)?;
            line.set_item("phase", record.getattr("phase")?)?;
            line.update(payload.as_mapping())?;
            let text: Any<'_> = attr(py, "json", "dumps")?.call(
                (line,),
                Some(&py_kwargs!(py; default = py.get_type::<PyString>())?),
            )?;
            attr(py, "builtins", "print")?
                .call((text,), Some(&py_kwargs!(py; file = stream, flush = true)?))?;
            Ok(())
        };
        match written() {
            Err(error) if !error.is_instance_of::<PyException>(py) => Err(error),
            _ => Ok(()),
        }
    }

    fn persist(&self, py: Python<'_>, record: &Any<'_>) -> PyResult<()> {
        let persisted = || -> PyResult<()> {
            let migrated: bool = self.with_state(|state| state.migrated);
            let rendered: Any<'_> = self.connection.bind(py).call_method(
                "render_run_events",
                (),
                Some(&py_kwargs!(py;
                    database = self.database.bind(py),
                    events = PyTuple::new(py, [record])?,
                    include_migration = !migrated,
                )?),
            )?;
            execute_observations(
                self.connection.bind(py),
                &observation_statements(&rendered)?,
            )?;
            self.with_state(|state| {
                state.migrated = true;
                state.persistence_warning_emitted = false;
            });
            Ok(())
        };
        let Err(error) = persisted() else {
            return Ok(());
        };
        if !error.is_instance_of::<PyException>(py) {
            return Err(error);
        }
        let message: String = error.value(py).str()?.extract()?;
        if self.strict_persistence {
            let raised: PyErr = PyErr::from_value(
                attr(py, ADAPTER_EXCEPTIONS, "AdapterWarehouseError")?
                    .call1((format!("Required run-event persistence failed: {message}"),))?,
            );
            raised.value(py).setattr("__context__", error.value(py))?;
            raised.set_cause(py, Some(error));
            return Err(raised);
        }
        if !self.with_state(|state| state.persistence_warning_emitted) {
            let warned = || -> PyResult<()> {
                attr(py, "builtins", "print")?.call(
                    (format!(
                        "run observation failed; continuing execution and retrying: {message}"
                    ),),
                    Some(&py_kwargs!(py; file = attr(py, "sys", "stderr")?)?),
                )?;
                Ok(())
            };
            if let Err(failure) = warned()
                && !failure.is_instance_of::<PyException>(py)
            {
                return Err(failure);
            }
            self.with_state(|state| state.persistence_warning_emitted = true);
        }
        Ok(())
    }

    fn verify_workflow_statements(&self, py: Python<'_>, expected: Vec<Any<'_>>) -> PyResult<()> {
        let query: Any<'_> = attr(
            py,
            NATIVE,
            "render_clickhouse_run_statement_verification_query",
        )?
        .call1((self.database.bind(py), self.invocation_id.bind(py)))?;
        let result: Any<'_> = self.connection.bind(py).call_method1("query", (query,))?;
        let int: Any<'_> = attr(py, "builtins", "int")?;
        let mut observed: Vec<Any<'_>> = Vec::new();
        for row in result.getattr("rows")?.try_iter()? {
            let row: Any<'_> = row?;
            observed.push(
                PyTuple::new(
                    py,
                    [
                        int.call1((row.get_item(0)?.str()?,))?,
                        row.get_item(1)?.str()?.into_any(),
                        row.get_item(2)?.str()?.into_any(),
                    ],
                )?
                .into_any(),
            );
        }
        let (observed_count, expected_count): (usize, usize) = (observed.len(), expected.len());
        if !PyTuple::new(py, observed)?.eq(PyTuple::new(py, expected)?)? {
            return Err(PyErr::from_value(
                attr(py, ADAPTER_EXCEPTIONS, "AdapterWarehouseError")?.call1((format!(
                    "Run statement persistence verification failed; warehouse execution was not \
                     started (expected {expected_count} rows, observed {observed_count} rows)"
                ),))?,
            ));
        }
        Ok(())
    }
}

/// `list(value)`, or an empty list when the argument was omitted.
fn list<'py>(py: Python<'py>, value: Option<Any<'py>>) -> PyResult<Any<'py>> {
    match value {
        Some(value) => {
            Ok(PyList::new(py, value.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any())
        }
        None => Ok(PyList::empty(py).into_any()),
    }
}

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

fn text<'py>(py: Python<'py>, value: &str) -> Any<'py> {
    PyString::new(py, value).into_any()
}

#[pymethods]
impl RunEventSink {
    #[new]
    #[pyo3(signature = (*, connection, database, invocation_id, project_identity = None, jsonl_stream = None, strict_persistence = false))]
    fn new(
        py: Python<'_>,
        connection: Py<PyAny>,
        database: Py<PyAny>,
        invocation_id: Py<PyAny>,
        project_identity: Option<Py<PyAny>>,
        jsonl_stream: Option<Py<PyAny>>,
        strict_persistence: bool,
    ) -> PyResult<Self> {
        Ok(Self {
            connection,
            database,
            invocation_id,
            project_identity: project_identity.unwrap_or_else(|| py.None()),
            jsonl_stream: jsonl_stream.unwrap_or_else(|| py.None()),
            strict_persistence,
            emit_lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
            heartbeat_stop: attr(py, "threading", "Event")?.call0()?.unbind(),
            state: Mutex::new(SinkState {
                sequence: 0,
                migrated: false,
                heartbeat_thread: None,
                persistence_warning_emitted: false,
                workflow_revision: 0,
                operation_evidence: None,
            }),
        })
    }

    /// Set complete operation evidence for the next run boundary event.
    fn set_operation_evidence(&self, py: Python<'_>, evidence: Py<PyAny>) {
        let evidence: Option<Py<PyAny>> = (!evidence.is_none(py)).then_some(evidence);
        self.with_state(|state| state.operation_evidence = evidence);
    }

    /// The workflow is assembled and about to execute.
    #[pyo3(signature = (*, command, mode, total_statements, selected_node_count, display_command = None, selectors = None, start_time = None, executed_logical_ids = None, context_logical_ids = None, startup_timings = None))]
    #[allow(clippy::too_many_arguments)]
    fn run_started(
        slf: &Bound<'_, Self>,
        command: Any<'_>,
        mode: Any<'_>,
        total_statements: Any<'_>,
        selected_node_count: Any<'_>,
        display_command: Option<Any<'_>>,
        selectors: Option<Any<'_>>,
        start_time: Option<Any<'_>>,
        executed_logical_ids: Option<Any<'_>>,
        context_logical_ids: Option<Any<'_>>,
        startup_timings: Option<Any<'_>>,
    ) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let sink: &Self = slf.get();
        let display: Any<'_> = match display_command.filter(|value| !value.is_none()) {
            Some(display) => display,
            None => attr(py, "os", "environ")?.call_method1(
                "get",
                (constant(py, "RUN_DISPLAY_COMMAND_ENV_VAR")?, &command),
            )?,
        };
        let timings: Any<'_> = match startup_timings.filter(|value| !value.is_none()) {
            None => none(py),
            Some(timings) => py_kwargs!(py;
                compileMs = timings.getattr("compile_ms")?,
                observabilityMs = timings.getattr("observability_ms")?,
                planningMs = timings.getattr("planning_ms")?,
                totalMs = timings.getattr("total_ms")?,
            )?
            .into_any(),
        };
        let payload: Bound<'_, PyDict> = py_kwargs!(py;
            command = &command,
            displayCommand = display,
            mode = mode,
            toolVersion = attr(py, BUILD_CONSTANTS, "SQLTIDE_TOOL_VERSION")?,
            totalStatements = total_statements,
            selectedNodeCount = selected_node_count,
            database = sink.database.bind(py),
            projectIdentity = sink.project_identity.bind(py),
            selectors = list(py, selectors)?,
            startTime = start_time.unwrap_or_else(|| none(py)),
            executedLogicalIds = list(py, executed_logical_ids)?,
            contextLogicalIds = list(py, context_logical_ids)?,
            startupTimings = timings,
            operationEvidence = sink.evidence(py),
        )?;
        sink.emit(py, "run_started", none(py), none(py), payload, true)?;
        sink.heartbeat_stop.bind(py).call_method0("clear")?;
        let target: Py<Self> = slf.clone().unbind();
        let beat = pyo3::types::PyCFunction::new_closure(
            py,
            Some(c"heartbeat_loop"),
            None,
            move |arguments: &Bound<'_, PyTuple>, _: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                let py: Python<'_> = arguments.py();
                let sink: &Self = target.get();
                loop {
                    let interval: Any<'_> = constant(py, "HEARTBEAT_INTERVAL_SECONDS")?;
                    if sink
                        .heartbeat_stop
                        .bind(py)
                        .call_method1("wait", (interval,))?
                        .is_truthy()?
                    {
                        return Ok(());
                    }
                    sink.emit(
                        py,
                        "run_heartbeat",
                        none(py),
                        none(py),
                        PyDict::new(py),
                        false,
                    )?;
                }
            },
        )?;
        let thread: Any<'_> = attr(py, "threading", "Thread")?
            .call((), Some(&py_kwargs!(py; target = beat, daemon = true)?))?;
        sink.with_state(|state| state.heartbeat_thread = Some(thread.clone().unbind()));
        thread.call_method0("start")?;
        Ok(())
    }

    /// The run reached a terminal state; closes the event stream.
    #[pyo3(signature = (*, outcome, exit_code, error_message))]
    fn run_completed(
        &self,
        py: Python<'_>,
        outcome: Any<'_>,
        exit_code: Any<'_>,
        error_message: Any<'_>,
    ) -> PyResult<()> {
        self.stop(py)?;
        let evidence: Any<'_> = self.evidence(py);
        let complete: bool = !evidence.is_none();
        let payload: Bound<'_, PyDict> = py_kwargs!(py;
            outcome = outcome,
            exitCode = exit_code,
            errorMessage = error_message,
            operationEvidence = evidence,
        )?;
        self.emit(py, "run_completed", none(py), none(py), payload, complete)
    }

    /// Stop the heartbeat without requiring a terminal event write.
    fn stop(&self, py: Python<'_>) -> PyResult<()> {
        self.heartbeat_stop.bind(py).call_method0("set")?;
        let thread: Option<Py<PyAny>> = self.with_state(|state| {
            state
                .heartbeat_thread
                .as_ref()
                .map(|thread| thread.clone_ref(py))
        });
        if let Some(thread) = thread {
            let thread: Any<'_> = thread.into_bound(py);
            if !thread.is(&attr(py, "threading", "current_thread")?.call0()?) {
                thread.call_method(
                    "join",
                    (),
                    Some(&py_kwargs!(py; timeout = constant(py, "HEARTBEAT_INTERVAL_SECONDS")?)?),
                )?;
            }
        }
        Ok(())
    }

    /// One statement is about to execute; returns its query id.
    fn statement_started(&self, py: Python<'_>, statement: Any<'_>) -> PyResult<String> {
        let query_id: String = attr(py, "uuid", "uuid4")?.call0()?.str()?.extract()?;
        let payload: Bound<'_, PyDict> = py_kwargs!(py;
            statementSequence = statement.getattr("sequence")?,
            intent = statement.getattr("intent")?.str()?,
            queryId = &query_id,
            displayName = statement.getattr("display_name")?,
            replayOffsetProgress = replay_offset_progress_payload(&statement)?,
        )?;
        self.emit(
            py,
            "statement_started",
            statement.getattr("step_id")?,
            statement.getattr("phase")?.str()?.into_any(),
            payload,
            false,
        )?;
        Ok(query_id)
    }

    /// Persist and verify the statement set, skipping adapters that render none.
    #[pyo3(signature = (*, statements, workflow_sha256))]
    fn workflow_prepared(
        &self,
        py: Python<'_>,
        statements: Any<'_>,
        workflow_sha256: Any<'_>,
    ) -> PyResult<()> {
        if !statements.is_truthy()? {
            return Ok(());
        }
        let statements: Vec<Any<'_>> = statements.try_iter()?.collect::<PyResult<_>>()?;
        let next_revision: i64 = self.with_state(|state| state.workflow_revision) + 1;
        let sql: Vec<Any<'_>> = statements
            .iter()
            .map(|statement| statement.getattr("sql"))
            .collect::<PyResult<_>>()?;
        let redacted: Bound<'_, PyTuple> =
            attr(py, NATIVE, "redact_clickhouse_workflow_statements")?
                .call1((PyList::new(py, sql)?, &workflow_sha256))?
                .cast_into()?;
        let persisted_sql: Vec<String> = redacted.get_item(0)?.extract()?;
        let persisted_workflow_sha256: Any<'_> = redacted.get_item(1)?;
        let record_class: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterRunStatementRecord")?;
        let mut records: Vec<Any<'_>> = Vec::new();
        let mut expected: Vec<Any<'_>> = Vec::new();
        for (statement, persisted) in statements.iter().zip(&persisted_sql) {
            let digest: String = hex_sha256(persisted.as_bytes());
            let sequence: Any<'_> = statement.getattr("sequence")?;
            records.push(record_class.call(
                (),
                Some(&py_kwargs!(py;
                    invocation_id = self.invocation_id.bind(py),
                    statement_sequence = &sequence,
                    step_id = statement.getattr("step_id")?,
                    phase = statement.getattr("phase")?.str()?,
                    intent = statement.getattr("intent")?.str()?,
                    sql = persisted,
                    sql_sha256 = &digest,
                    workflow_sha256 = &persisted_workflow_sha256,
                    workflow_revision = next_revision,
                )?),
            )?);
            expected.push(
                PyTuple::new(
                    py,
                    [
                        sequence,
                        text(py, &digest),
                        persisted_workflow_sha256.clone(),
                    ],
                )?
                .into_any(),
            );
        }
        let migrated: bool = self.with_state(|state| state.migrated);
        let rendered: Any<'_> = self.connection.bind(py).call_method(
            "render_run_statements",
            (),
            Some(&py_kwargs!(py;
                database = self.database.bind(py),
                statements = PyTuple::new(py, records)?,
                include_migration = !migrated,
            )?),
        )?;
        if !rendered.is_truthy()? {
            if self.strict_persistence {
                return Err(PyErr::from_value(
                    attr(py, ADAPTER_EXCEPTIONS, "AdapterWarehouseError")?
                        .call1(("Adapter did not provide durable run-statement persistence",))?,
                ));
            }
            return Ok(());
        }
        self.with_state(|state| state.workflow_revision = next_revision);
        execute_observations(
            self.connection.bind(py),
            &observation_statements(&rendered)?,
        )?;
        self.with_state(|state| state.migrated = true);
        self.verify_workflow_statements(py, expected)
    }

    /// One scheduled audit is about to execute.
    #[pyo3(signature = (*, name))]
    fn audit_started(&self, py: Python<'_>, name: Any<'_>) -> PyResult<()> {
        let payload: Bound<'_, PyDict> = py_kwargs!(py; name = &name)?;
        self.emit(py, "audit_started", name, text(py, "audit"), payload, false)
    }

    /// One scheduled audit reached a terminal result.
    #[pyo3(signature = (*, name, status, failure_count, error_message))]
    fn audit_completed(
        &self,
        py: Python<'_>,
        name: Any<'_>,
        status: Any<'_>,
        failure_count: Any<'_>,
        error_message: Any<'_>,
    ) -> PyResult<()> {
        let payload: Bound<'_, PyDict> = py_kwargs!(py;
            name = &name,
            status = status,
            failureCount = failure_count,
            errorMessage = error_message,
        )?;
        self.emit(
            py,
            "audit_completed",
            name,
            text(py, "audit"),
            payload,
            false,
        )
    }

    /// One statement finished, successfully or not.
    #[pyo3(signature = (*, statement, error_message, written_rows, elapsed_ms))]
    fn statement_completed(
        &self,
        py: Python<'_>,
        statement: Any<'_>,
        error_message: Any<'_>,
        written_rows: Any<'_>,
        elapsed_ms: Any<'_>,
    ) -> PyResult<()> {
        let payload: Bound<'_, PyDict> = py_kwargs!(py;
            statementSequence = statement.getattr("sequence")?,
            intent = statement.getattr("intent")?.str()?,
            elapsedMs = elapsed_ms,
            writtenRows = written_rows,
            errorMessage = error_message,
            displayName = statement.getattr("display_name")?,
        )?;
        self.emit(
            py,
            "statement_completed",
            statement.getattr("step_id")?,
            statement.getattr("phase")?.str()?.into_any(),
            payload,
            false,
        )
    }
}

fn replay_offset_progress_payload<'py>(statement: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = statement.py();
    let request: Any<'py> = statement.getattr("replay_offset_progress")?;
    if request.is_none() {
        return Ok(request);
    }
    let mut ranges: Vec<Any<'py>> = Vec::new();
    for range in request.getattr("ranges")?.try_iter()? {
        let range: Any<'py> = range?;
        ranges.push(
            py_kwargs!(py;
                partition = range.getattr("partition")?,
                lowerOffset = range.getattr("lower_offset")?,
                upperOffset = range.getattr("upper_offset")?,
            )?
            .into_any(),
        );
    }
    Ok(py_kwargs!(py;
        database = request.getattr("database")?,
        relation = request.getattr("relation")?,
        partitionColumn = request.getattr("partition_column")?,
        offsetColumn = request.getattr("offset_column")?,
        ranges = PyList::new(py, ranges)?,
    )?
    .into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RunEventSink>()?;
    module.add_function(wrap_pyfunction!(start_invocation, module)?)?;
    module.add_function(wrap_pyfunction!(logical_project_identity, module)?)?;
    module.add_function(wrap_pyfunction!(build_invocation_record, module)?)?;
    module.add_function(wrap_pyfunction!(build_node_result_record, module)?)?;
    module.add_function(wrap_pyfunction!(persist_terminal_observations, module)?)?;
    module.add_function(wrap_pyfunction!(persist_run_events, module)?)
}
