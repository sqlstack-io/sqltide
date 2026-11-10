//! The `sqt build` command bodies for a prepared direct, virtual or mixed workflow: confirmation,
//! the target mutation lock, execution with recovery, published workflow evidence, terminal
//! observations, run events and the operator-facing result. Python adapters, observability
//! helpers and Kafka administration are looked up from their modules when called; terminal output
//! goes through Python's `print` and `sys` streams so redirection and tests see it.

use pyo3::exceptions::{PyException, PyKeyboardInterrupt, PyOSError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PyString};

use crate::artifacts::json_dumps;
use crate::build_workflow::identity;
use crate::direct_build::{Value, boolean, build, call, class, is_a, items, none, text, tuple};
use crate::handling::{raise_from, while_handling, within};

const NATIVE: &str = "sqltide._native";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const DIRECT_EXCEPTIONS: &str = "sqltide.executor.direct.exceptions";
const WORKFLOW_EXCEPTIONS: &str = "sqltide.executor.workflow.exceptions";
const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const AUDITING_TYPES: &str = "sqltide.executor.auditing.types";
const OBSERVABILITY_MODELS: &str = "sqltide.executor.observability.models";
const OBSERVABILITY_TYPES: &str = "sqltide.executor.observability.types";
const RUN_EVENT_SINK: &str = "sqltide._native";
const INVOCATION_RECORD: &str = "sqltide._native";
const NODE_RESULT_RECORD: &str = "sqltide._native";
const PROJECT_IDENTITY: &str = "sqltide._native";
const TERMINAL_OBSERVATIONS: &str = "sqltide._native";
const START_INVOCATION: &str = "sqltide._native";
const RESET_OFFSETS: &str = "sqltide.executor.kafka_admin.main.reset_fresh_landing_offsets";
const MUTATION_LOCK: &str = "sqltide.executor.workflow.main.target_mutation_lock";
const BUILD_WORKFLOW: &str = "sqltide.executor.workflow.main.execute_build_workflow";
const RECOVERY_STATEMENTS: &str = "sqltide.executor.workflow.main._execute_recovery_statements";
const PUBLISH_BUILD: &str = "sqltide._native";
const SIGINT_EXIT_CODE: i64 = 130;
const DIRECT_MODE: &str = "direct";
const VIRTUAL_MODE: &str = "virtual_environment";
const CANCELLED: &str = "Build cancelled.";

fn native<'py>(py: Python<'py>, name: &str, pairs: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
    call(py, NATIVE, name, pairs)
}

pub(crate) fn int(py: Python<'_>, value: i64) -> PyResult<Value<'_>> {
    Ok(value.into_pyobject(py)?.into_any())
}

fn caught(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    Ok(error.is_instance(py, &class(py, module, name)?))
}

/// `print(value)`, or `print(value, file=sys.stderr)`, resolving the stream when called.
pub(crate) fn print(py: Python<'_>, value: &Value<'_>, stderr: bool) -> PyResult<()> {
    let arguments: Bound<'_, PyDict> = PyDict::new(py);
    if stderr {
        arguments.set_item("file", py.import("sys")?.getattr("stderr")?)?;
    }
    py.import("builtins")?
        .getattr("print")?
        .call((value,), Some(&arguments))?;
    Ok(())
}

fn print_text(py: Python<'_>, value: &str, stderr: bool) -> PyResult<()> {
    print(py, &text(py, value), stderr)
}

/// `try: body() except Exception: pass`.
pub(crate) fn quietly(py: Python<'_>, body: impl FnOnce() -> PyResult<()>) -> PyResult<()> {
    match body() {
        Err(error) if error.is_instance_of::<PyException>(py) => Ok(()),
        outcome => outcome,
    }
}

/// `print(warning, file=sys.stderr)` unless it is `None`, ignoring an `Exception` from printing.
fn print_optional_warning(py: Python<'_>, warning: &Value<'_>) -> PyResult<()> {
    if warning.is_none() {
        return Ok(());
    }
    quietly(py, || print(py, warning, true))
}

/// `options.pipelines_root.parent / "target"`.
fn target_dir<'py>(options: &Value<'py>) -> PyResult<Value<'py>> {
    options
        .getattr("pipelines_root")?
        .getattr("parent")?
        .call_method1("__truediv__", ("target",))
}

fn project_dir<'py>(options: &Value<'py>) -> PyResult<Value<'py>> {
    options.getattr("pipelines_root")?.getattr("parent")
}

/// `type:name` graph-node identities of logical keys, in order.
fn resource_identities<'py>(py: Python<'py>, keys: Value<'py>) -> PyResult<Value<'py>> {
    let mut identities: Vec<Value<'py>> = Vec::new();
    for key in items(&keys)? {
        identities.push(PyString::new(py, &identity(&key)?).into_any());
    }
    tuple(py, identities)
}

/// The run event sink a build reports to, on the observation connection.
fn event_sink<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    preview: &Value<'py>,
    started: &Value<'py>,
) -> PyResult<Value<'py>> {
    let sink: Value<'py> = class(py, RUN_EVENT_SINK, "RunEventSink")?;
    let connection: Value<'py> = client.clone();
    let database: Value<'py> = preview.getattr("metadata_database")?;
    let invocation_id: Value<'py> = started.get_item(0)?;
    let project_identity: Value<'py> = call(
        py,
        PROJECT_IDENTITY,
        "logical_project_identity",
        &[("project_dir", project_dir(options)?)],
    )?;
    let stream: Value<'py> = if options.getattr("events_output")?.is_truthy()? {
        py.import("sys")?.getattr("stdout")?
    } else {
        none(py)
    };
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    arguments.set_item("connection", connection)?;
    arguments.set_item("database", database)?;
    arguments.set_item("invocation_id", invocation_id)?;
    arguments.set_item("project_identity", project_identity)?;
    arguments.set_item("jsonl_stream", stream)?;
    sink.call((), Some(&arguments))
}

fn run_completed(
    sink: &Value<'_>,
    outcome: &str,
    exit_code: i64,
    error_message: Option<Value<'_>>,
) -> PyResult<()> {
    let py: Python<'_> = sink.py();
    let arguments: Bound<'_, PyDict> = PyDict::new(py);
    arguments.set_item("outcome", outcome)?;
    arguments.set_item("exit_code", exit_code)?;
    arguments.set_item("error_message", error_message.unwrap_or_else(|| none(py)))?;
    sink.call_method("run_completed", (), Some(&arguments))?;
    Ok(())
}

/// `sink.run_completed(...)` behind the commands' `if sink is not None` guard.
fn complete_run(
    sink: &Value<'_>,
    outcome: &str,
    exit_code: i64,
    error_message: Option<Value<'_>>,
) -> PyResult<()> {
    if sink.is_none() {
        return Ok(());
    }
    run_completed(sink, outcome, exit_code, error_message)
}

pub(crate) fn invocation_record<'py>(
    py: Python<'py>,
    started: &Value<'py>,
    terminal: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let terminal: Value<'py> = build(py, OBSERVABILITY_MODELS, "TerminalInvocation", terminal)?;
    call(
        py,
        INVOCATION_RECORD,
        "build_invocation_record",
        &[("started", started.clone()), ("terminal", terminal)],
    )
}

/// Persist terminal observations, printing a returned warning and ignoring a printing failure.
fn persist_terminal<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    database: Value<'py>,
    invocation: Value<'py>,
    node_results: Value<'py>,
) -> PyResult<()> {
    let warning: Value<'py> = call(
        py,
        TERMINAL_OBSERVATIONS,
        "persist_terminal_observations",
        &[
            ("client", client.clone()),
            ("database", database),
            ("invocation", invocation),
            ("node_results", node_results),
        ],
    )?;
    print_optional_warning(py, &warning)
}

fn confirm<'py>(py: Python<'py>, options: &Value<'py>, preparation: &Value<'py>) -> PyResult<bool> {
    native(
        py,
        "confirm_build",
        &[
            ("options", options.clone()),
            ("plan_text", preparation.getattr("plan_text")?),
            (
                "protection_requirements",
                preparation.getattr("protection_requirements")?,
            ),
        ],
    )?
    .is_truthy()
}

/// The target mutation lock, or a null context when the caller already holds it.
fn lock_context<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    database: Value<'py>,
    acquire: bool,
) -> PyResult<Value<'py>> {
    if !acquire {
        return py.import("contextlib")?.getattr("nullcontext")?.call0();
    }
    call(
        py,
        MUTATION_LOCK,
        "target_mutation_lock",
        &[
            ("connection", client.clone()),
            ("database", database),
            ("ensure_database", boolean(py, true)?),
        ],
    )
}

/// Print each notice of the fresh landing offsets reset for `source_preparation`.
fn reset_fresh_landing_offsets<'py>(
    py: Python<'py>,
    source_preparation: Value<'py>,
) -> PyResult<()> {
    let resets: Value<'py> = call(
        py,
        RESET_OFFSETS,
        "reset_fresh_landing_offsets",
        &[("source_preparation", source_preparation)],
    )?;
    for reset in resets.try_iter()? {
        print_optional_warning(py, &reset?.getattr("notice")?)?;
    }
    Ok(())
}

// Direct builds.

/// Show the destructive plan, require confirmation, then build.
fn execute_confirmed_direct_build<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    emitter: &Value<'py>,
    confirmation_required: bool,
    acquire_lock: bool,
) -> PyResult<Option<Value<'py>>> {
    if confirmation_required && !confirm(py, options, preparation)? {
        return Ok(None);
    }
    let request: Value<'py> = preparation.getattr("request")?;
    let manager: Value<'py> = lock_context(py, client, request.getattr("database")?, acquire_lock)?;
    within(&manager, || {
        reject_new_out_of_scope_consumers(py, &request, client)?;
        let preview: Value<'py> = preparation.getattr("preview")?;
        let pair: Value<'py> = native(
            py,
            "plan_preserved_managed_sources",
            &[
                ("plan", request.getattr("plan")?),
                (
                    "realized_project",
                    preview.getattr("analysis")?.getattr("realized_project")?,
                ),
                (
                    "catalog",
                    preview.getattr("warehouse_snapshot")?.getattr("catalog")?,
                ),
                ("database", preview.getattr("database")?),
            ],
        )?;
        let (source_preparation, _): (Value<'py>, Value<'py>) = pair.extract()?;
        reset_fresh_landing_offsets(py, source_preparation)?;
        let runtime_execution: Value<'py> = match native(
            py,
            "execute_direct_build_workflow",
            &[
                ("workflow", preparation.getattr("workflow")?),
                ("connection", client.clone()),
                ("emitter", emitter.clone()),
            ],
        ) {
            Ok(runtime_execution) => runtime_execution,
            Err(error)
                if caught(
                    py,
                    &error,
                    DIRECT_EXCEPTIONS,
                    "DirectWorkflowExecutionError",
                )? =>
            {
                let raised: PyErr = match direct_workflow_failure(
                    py,
                    preparation,
                    options,
                    client,
                    &request,
                    error.value(py).as_any(),
                ) {
                    Ok(raised) | Err(raised) => raised,
                };
                return Err(while_handling(py, raised, &error));
            }
            Err(error) => return Err(error),
        };
        let execution: Value<'py> = runtime_execution.getattr("execution")?;
        let captures: Value<'py> = runtime_execution.getattr("captures")?;
        let result: Value<'py> = native(
            py,
            "build_direct_execution_result",
            &[
                ("request", request.clone()),
                ("execution", execution.clone()),
                ("captures", captures.clone()),
            ],
        )?;
        let audit_result: Value<'py> = result.getattr("audit_result")?;
        let status: &str = if audit_result.getattr("error_failure_count")?.is_truthy()? {
            "failed"
        } else {
            "succeeded"
        };
        let target: Value<'py> = target_dir(options)?;
        let workflow: Value<'py> = runtime_execution.getattr("workflow")?;
        let execution_json: Value<'py> = native(
            py,
            "render_direct_execution_json",
            &[
                ("request", request.clone()),
                ("status", text(py, status)),
                ("captures", captures),
                ("execution", execution),
                ("failed_step_id", none(py)),
                ("error_message", none(py)),
                ("audit_result", audit_result),
            ],
        )?;
        let warning: Value<'py> =
            publish_direct_build_artifact(py, target, workflow, execution_json)?;
        print_optional_warning(py, &warning)?;
        let warning: Value<'py> = native(
            py,
            "persist_direct_fingerprints",
            &[("request", request.clone()), ("connection", client.clone())],
        )?;
        print_optional_warning(py, &warning)?;
        Ok(Some(result))
    })
}

/// The handler for a failed direct workflow: record evidence and fingerprints for the steps that
/// completed, then the exception to raise in its place.
fn direct_workflow_failure<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    request: &Value<'py>,
    error: &Value<'py>,
) -> PyResult<PyErr> {
    let cause: Value<'py> = error.getattr("cause")?;
    let interrupted: bool = cause.is_instance_of::<PyKeyboardInterrupt>();
    let target: Value<'py> = target_dir(options)?;
    let workflow: Value<'py> = error.getattr("workflow")?;
    let execution_json: Value<'py> = native(
        py,
        "render_direct_execution_json",
        &[
            ("request", request.clone()),
            (
                "status",
                text(py, if interrupted { "cancelled" } else { "failed" }),
            ),
            ("captures", error.getattr("captures")?),
            ("execution", error.getattr("partial_result")?),
            ("failed_step_id", error.getattr("failed_step_id")?),
            ("error_message", cause.str()?.into_any()),
            ("audit_result", none(py)),
        ],
    )?;
    let warning: Value<'py> = publish_direct_build_artifact(py, target, workflow, execution_json)?;
    print_optional_warning(py, &warning)?;
    let applied_model_names: Value<'py> = applied_model_names(
        py,
        request,
        &preparation.getattr("workflow")?,
        &error.getattr("partial_result")?,
    )?;
    let warning: Value<'py> = native(
        py,
        "persist_direct_fingerprints",
        &[
            ("request", request.clone()),
            ("connection", client.clone()),
            ("applied_model_names", applied_model_names),
        ],
    )?;
    print_optional_warning(py, &warning)?;
    let changes: Value<'py> = request.getattr("row_error_policy_changes")?;
    let policy_failure: Value<'py> = native(
        py,
        "row_error_policy_failure_message",
        &[
            ("changes", changes.clone()),
            ("failed_step_id", error.getattr("failed_step_id")?),
            ("cause", cause.clone()),
        ],
    )?;
    if changes.is_truthy()? && interrupted {
        let notes: Value<'py> = match cause.getattr("__notes__") {
            Ok(notes) => notes,
            Err(missing) if missing.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                PyList::empty(py).into_any()
            }
            Err(failure) => return Err(failure),
        };
        let joined: Value<'py> = PyString::new(py, "\n").call_method1("join", (notes,))?;
        let warning: Value<'py> = if joined.is_truthy()? {
            joined
        } else {
            none(py)
        };
        print_optional_warning(py, &warning)?;
    }
    if !policy_failure.is_none() && is_a(&cause, ADAPTER_EXCEPTIONS, "AdapterError")? {
        let replacement: Value<'py> =
            class(py, ADAPTER_EXCEPTIONS, "AdapterError")?.call1((policy_failure,))?;
        return raise_from(&replacement, error, Some(&cause));
    }
    raise_from(&cause, error, Some(error))
}

fn reject_new_out_of_scope_consumers<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<()> {
    let plan: Value<'py> = request.getattr("plan")?;
    if !plan.getattr("prerequisite_execution_scope")?.is_truthy()? {
        return Ok(());
    }
    let realized_project: Value<'py> = request.getattr("realized_project")?;
    let catalog: Value<'py> =
        client.call_method1("load_catalog", (request.getattr("database")?,))?;
    let consumers: Value<'py> = native(
        py,
        "find_direct_out_of_scope_consumers",
        &[
            ("realized_project", realized_project),
            ("catalog", catalog),
            (
                "prerequisite_execution_scope",
                plan.getattr("prerequisite_execution_scope")?,
            ),
            ("execution_scope", plan.getattr("execution_scope")?),
        ],
    )?;
    if !consumers.is_truthy()? {
        return Ok(());
    }
    let names: Value<'py> = PyString::new(py, ", ").call_method1("join", (consumers,))?;
    let message: String = format!(
        "Direct build stopped because out-of-scope materialized views now consume rebuilt \
         upstream models: {}",
        names.extract::<String>()?
    );
    Err(PyErr::from_value(
        class(py, DIRECT_EXCEPTIONS, "DirectBuildError")?.call1((message,))?,
    ))
}

/// Publish the workflow and its execution evidence, returning a warning when the write fails.
fn publish_direct_build_artifact<'py>(
    py: Python<'py>,
    target_dir: Value<'py>,
    workflow: Value<'py>,
    execution_json: Value<'py>,
) -> PyResult<Value<'py>> {
    match call(
        py,
        PUBLISH_BUILD,
        "publish_build_workflow",
        &[
            ("target_dir", target_dir),
            ("workflow", workflow),
            ("execution_json", execution_json),
        ],
    ) {
        Ok(_) => Ok(none(py)),
        Err(error) if error.is_instance_of::<PyOSError>(py) => Ok(PyString::new(
            py,
            &format!(
                "Direct build artifact was not recorded: {}",
                error.value(py).str()?
            ),
        )
        .into_any()),
        Err(error) => Err(error),
    }
}

/// Models whose replay completed, less those owned by a runtime replay that did not.
fn applied_model_names<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    workflow: &Value<'py>,
    execution: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut completed: Vec<Value<'py>> = Vec::new();
    for result in items(&execution.getattr("statement_results")?)? {
        completed.push(result.getattr("step_id")?);
    }
    let completed: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, completed)?;
    let mut replayed: Vec<Value<'py>> = Vec::new();
    for runtime in items(&workflow.getattr("runtime_replays")?)? {
        if completed.contains(runtime.getattr("replay_step_id")?)? {
            replayed.push(runtime.getattr("model_name")?);
        }
    }
    let replayed: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, replayed)?;
    let applied: Bound<'py, PySet> = PySet::empty(py)?;
    for replay_root in items(&request.getattr("plan")?.getattr("replay_roots")?)? {
        if replayed.contains(replay_root.getattr("model_key")?.getattr("name")?)? {
            for key in items(&replay_root.getattr("propagated_model_keys")?)? {
                applied.add(key.getattr("name")?)?;
            }
        }
    }
    let incomplete: Bound<'py, PySet> = PySet::empty(py)?;
    for runtime in items(&workflow.getattr("runtime_replays")?)? {
        if !completed.contains(runtime.getattr("replay_step_id")?)? {
            incomplete.add(runtime.getattr("model_name")?)?;
        }
    }
    applied.call_method1("difference_update", (incomplete,))?;
    Ok(PyFrozenSet::new(py, applied.iter())?.into_any())
}

struct DirectRun<'a, 'py> {
    py: Python<'py>,
    preview: Value<'py>,
    options: &'a Value<'py>,
    client: &'a Value<'py>,
    started: &'a Value<'py>,
}

impl<'py> DirectRun<'_, 'py> {
    fn invocation(
        &self,
        exit_code: i64,
        outcome: &str,
        materialized_outcome: Value<'py>,
        audit_result: Option<&Value<'py>>,
        error_message: Value<'py>,
    ) -> PyResult<Value<'py>> {
        let py: Python<'py> = self.py;
        let failure_count = |name: &str| -> PyResult<Value<'py>> {
            match audit_result {
                Some(audit_result) => audit_result.getattr(name),
                None => int(py, 0),
            }
        };
        let summary: Bound<'py, PyDict> = PyDict::new(py);
        summary.set_item(
            "audit_error_failure_count",
            failure_count("error_failure_count")?,
        )?;
        summary.set_item(
            "audit_warning_failure_count",
            failure_count("warning_failure_count")?,
        )?;
        invocation_record(
            py,
            self.started,
            &[
                ("project_dir", project_dir(self.options)?),
                ("target_identity", self.preview.getattr("database")?),
                ("command", text(py, "build")),
                ("mode", text(py, DIRECT_MODE)),
                ("outcome", text(py, outcome)),
                ("exit_code", int(py, exit_code)?),
                ("materialized_outcome", materialized_outcome),
                ("deployment_id", none(py)),
                ("workflow_id", none(py)),
                (
                    "selected_node_count",
                    int(py, self.execution_scope()?.len()? as i64)?,
                ),
                ("error_message", error_message),
                ("summary", summary.into_any()),
            ],
        )
    }

    fn execution_scope(&self) -> PyResult<Value<'py>> {
        self.preview.getattr("plan")?.getattr("execution_scope")
    }

    fn persist(&self, invocation: Value<'py>, node_results: Value<'py>) -> PyResult<()> {
        persist_terminal(
            self.py,
            self.client,
            self.preview.getattr("metadata_database")?,
            invocation,
            node_results,
        )
    }

    fn materialization(&self, name: &str) -> PyResult<Value<'py>> {
        class(self.py, OBSERVABILITY_TYPES, "MaterializationOutcome")?.getattr(name)
    }

    /// Record one terminal outcome without audit results.
    fn record(
        &self,
        exit_code: i64,
        outcome: &str,
        materialized_outcome: Value<'py>,
        error_message: Value<'py>,
    ) -> PyResult<()> {
        let invocation: Value<'py> = self.invocation(
            exit_code,
            outcome,
            materialized_outcome,
            None,
            error_message,
        )?;
        self.persist(invocation, tuple(self.py, Vec::new())?)
    }

    fn quality_context(&self, audit: &Value<'py>) -> PyResult<Value<'py>> {
        build(
            self.py,
            OBSERVABILITY_MODELS,
            "QualityResultContext",
            &[
                (
                    "trigger",
                    class(self.py, OBSERVABILITY_TYPES, "QualityResultTrigger")?
                        .getattr("BUILD")?,
                ),
                ("cadence_seconds", audit.getattr("cadence_seconds")?),
                ("warmup_seconds", audit.getattr("warmup_seconds")?),
            ],
        )
    }

    fn quality_identity(&self, audit: &Value<'py>) -> PyResult<Value<'py>> {
        self.py
            .import(NATIVE)?
            .getattr("require_quality_identity")?
            .call1((audit.getattr("quality_identity")?,))
    }

    fn node_result(&self, pairs: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
        call(
            self.py,
            NODE_RESULT_RECORD,
            "build_node_result_record",
            pairs,
        )
    }

    /// Node results for audits that ran during the build, paired strictly with their results.
    fn audit_node_results(
        &self,
        invocation: &Value<'py>,
        audits: Vec<Value<'py>>,
        audit_result: &Value<'py>,
    ) -> PyResult<Vec<Value<'py>>> {
        let py: Python<'py> = self.py;
        let arguments: Bound<'py, PyDict> = PyDict::new(py);
        arguments.set_item("strict", true)?;
        let pairs: Value<'py> = py.import("builtins")?.getattr("zip")?.call(
            (tuple(py, audits)?, audit_result.getattr("audit_results")?),
            Some(&arguments),
        )?;
        let warning: Value<'py> = class(py, AUDITING_TYPES, "AuditSeverity")?.getattr("WARNING")?;
        let first_five: Value<'py> = py
            .import("builtins")?
            .getattr("slice")?
            .call1((py.None(), 5))?;
        let mut records: Vec<Value<'py>> = Vec::new();
        for pair in pairs.try_iter()? {
            let pair: Value<'py> = pair?;
            let audit: Value<'py> = pair.get_item(0)?;
            let result: Value<'py> = pair.get_item(1)?;
            let status: &str = if !result.getattr("error_message")?.is_none() {
                "error"
            } else if result.getattr("passed")?.is_truthy()? {
                "passed"
            } else if result.getattr("severity")?.eq(&warning)? {
                "warning"
            } else {
                "failed"
            };
            let identity: Value<'py> = self.quality_identity(&audit)?;
            let context: Value<'py> = self.quality_context(&audit)?;
            let payload: Bound<'py, PyDict> = PyDict::new(py);
            payload.set_item(
                "sample_column_names",
                PyList::new(py, items(&result.getattr("sample_column_names")?)?)?,
            )?;
            let mut rows: Vec<Value<'py>> = Vec::new();
            for row in items(&result.getattr("sample_rows")?.get_item(&first_five)?)? {
                rows.push(PyList::new(py, items(&row)?)?.into_any());
            }
            payload.set_item("sample_rows", PyList::new(py, rows)?)?;
            records.push(self.node_result(&[
                ("invocation", invocation.clone()),
                ("identity", identity),
                ("context", context),
                ("status", text(py, status)),
                ("severity", result.getattr("severity")?),
                ("failure_count", result.getattr("failing_row_count")?),
                ("payload", payload.into_any()),
                ("error_message", result.getattr("error_message")?),
            ])?);
        }
        Ok(records)
    }

    /// Deferred node results for audits that wait out their warmup after the build.
    fn deferred_audit_node_results(
        &self,
        invocation: &Value<'py>,
        audits: Vec<Value<'py>>,
    ) -> PyResult<Vec<Value<'py>>> {
        let py: Python<'py> = self.py;
        if audits.is_empty() {
            return Ok(Vec::new());
        }
        let anchors_by_model: Bound<'py, PyDict> = PyDict::new(py);
        for audit in &audits {
            for model_name in items(&audit.getattr("referenced_model_names")?)? {
                anchors_by_model.set_item(model_name, invocation.getattr("completed_at")?)?;
            }
        }
        let audits_tuple: Value<'py> = tuple(py, audits.clone())?;
        let states: Value<'py> = native(
            py,
            "resolve_audit_warmup_states",
            &[
                ("audits", audits_tuple),
                ("anchors_by_model", anchors_by_model.clone().into_any()),
                (
                    "materialized_model_names",
                    PyFrozenSet::new(py, anchors_by_model.keys().iter())?.into_any(),
                ),
                ("warehouse_now", invocation.getattr("completed_at")?),
            ],
        )?;
        let mut records: Vec<Value<'py>> = Vec::new();
        for audit in &audits {
            let identity: Value<'py> = self.quality_identity(audit)?;
            let context: Value<'py> = self.quality_context(audit)?;
            let severity: Value<'py> = audit.getattr("severity")?;
            let name: Value<'py> = audit.getattr("name")?;
            let key: Value<'py> = if name.is_truthy()? {
                name
            } else {
                audit.getattr("file_path")?.getattr("stem")?
            };
            let payload: Bound<'py, PyDict> = PyDict::new(py);
            payload.set_item("eligible_at", states.get_item(key)?.getattr("eligible_at")?)?;
            records.push(self.node_result(&[
                ("invocation", invocation.clone()),
                ("identity", identity),
                ("context", context),
                ("status", text(py, "deferred")),
                ("severity", severity),
                ("failure_count", int(py, 0)?),
                ("payload", payload.into_any()),
                ("error_message", none(py)),
            ])?);
        }
        Ok(records)
    }
}

/// Confirm, execute, and audit one prepared direct build.
#[pyfunction]
#[pyo3(signature = (
    *,
    preparation,
    options,
    client,
    observation_client,
    started,
    confirmation_required=true,
    startup_timings=None,
    _acquire_target_mutation_lock=true,
))]
#[allow(clippy::too_many_arguments)]
fn execute_direct_build_command<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    started: &Value<'py>,
    confirmation_required: bool,
    startup_timings: Option<Value<'py>>,
    _acquire_target_mutation_lock: bool,
) -> PyResult<i64> {
    let acquire_lock: bool = _acquire_target_mutation_lock;
    let preview: Value<'py> = preparation.getattr("preview")?;
    let run: DirectRun<'_, 'py> = DirectRun {
        py,
        preview: preview.clone(),
        options,
        client,
        started,
    };
    let sink: Value<'py> = event_sink(py, options, observation_client, &preview, started)?;
    let attempt = || -> PyResult<Result<Option<Value<'py>>, i64>> {
        let plan: Value<'py> = preview.getattr("plan")?;
        if !sink.is_none() {
            let mut context: Vec<Value<'py>> = Vec::new();
            for item in items(&plan.getattr("prerequisite_scope")?)? {
                context.push(item.getattr("key")?);
            }
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("command", "build")?;
            arguments.set_item("mode", DIRECT_MODE)?;
            arguments.set_item(
                "total_statements",
                preparation
                    .getattr("workflow")?
                    .getattr("statements")?
                    .len()?,
            )?;
            arguments.set_item(
                "selected_node_count",
                plan.getattr("execution_scope")?.len()?,
            )?;
            arguments.set_item("selectors", options.getattr("selectors")?)?;
            arguments.set_item("start_time", options.getattr("start_time")?)?;
            arguments.set_item(
                "executed_logical_ids",
                resource_identities(py, plan.getattr("execution_scope")?)?,
            )?;
            arguments.set_item(
                "context_logical_ids",
                resource_identities(py, tuple(py, context)?)?,
            )?;
            arguments.set_item(
                "startup_timings",
                startup_timings.clone().unwrap_or_else(|| none(py)),
            )?;
            sink.call_method("run_started", (), Some(&arguments))?;
        }
        let changed: Value<'py> =
            class(py, PLANNER_TYPES, "DirectSelectionMode")?.getattr("CHANGED")?;
        if plan.getattr("selection_mode")?.eq(&changed)?
            && !plan.getattr("execution_scope")?.is_truthy()?
            && !preview.getattr("row_error_policy_changes")?.is_truthy()?
        {
            run.record(0, "succeeded", none(py), none(py))?;
            complete_run(&sink, "succeeded", 0, None)?;
            if !options.getattr("events_output")?.is_truthy()? {
                let message: &str = if options.getattr("json_output")?.is_truthy()? {
                    r#"{"mode": "direct", "status": "no_op", "reason": "no_changes"}"#
                } else {
                    "No changed models to build."
                };
                print_text(py, message, false)?;
            }
            return Ok(Err(0));
        }
        Ok(Ok(execute_confirmed_direct_build(
            py,
            preparation,
            options,
            client,
            &sink,
            confirmation_required,
            acquire_lock,
        )?))
    };
    let execution: Option<Value<'py>> = match attempt() {
        Ok(Ok(execution)) => execution,
        Ok(Err(exit_code)) => return Ok(exit_code),
        Err(error)
            if caught(py, &error, ADAPTER_EXCEPTIONS, "AdapterError")?
                || caught(py, &error, DIRECT_EXCEPTIONS, "DirectBuildError")?
                || error.is_instance_of::<PyOSError>(py) =>
        {
            let handle = || -> PyResult<i64> {
                let message: Value<'py> = error.value(py).str()?.into_any();
                let materialized: Value<'py> = run.materialization("FAILED")?;
                run.record(1, "failed", materialized, message)?;
                complete_run(&sink, "failed", 1, Some(error.value(py).str()?.into_any()))?;
                print(py, error.value(py).str()?.as_any(), true)?;
                Ok(1)
            };
            return handle().map_err(|raised| while_handling(py, raised, &error));
        }
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
            let handle = || -> PyResult<i64> {
                let materialized: Value<'py> = run.materialization("FAILED")?;
                run.record(SIGINT_EXIT_CODE, "cancelled", materialized, none(py))?;
                complete_run(&sink, "cancelled", SIGINT_EXIT_CODE, None)?;
                quietly(py, || {
                    let message: Value<'py> = native(py, "render_interrupted_build_message", &[])?;
                    print(py, &message, true)
                })?;
                Ok(SIGINT_EXIT_CODE)
            };
            return handle().map_err(|raised| while_handling(py, raised, &error));
        }
        Err(error) => return Err(error),
    };
    let Some(execution) = execution else {
        run.record(1, "cancelled", none(py), none(py))?;
        complete_run(&sink, "cancelled", 1, None)?;
        print_text(py, CANCELLED, false)?;
        return Ok(1);
    };
    let audit_result: Value<'py> = execution.getattr("audit_result")?;
    let exit_code: i64 = if audit_result.getattr("error_failure_count")?.is_truthy()? {
        1
    } else {
        0
    };
    let outcome: &str = if exit_code != 0 {
        "failed"
    } else {
        "succeeded"
    };
    let invocation: Value<'py> = run.invocation(
        exit_code,
        outcome,
        run.materialization("APPLIED")?,
        Some(&audit_result),
        none(py),
    )?;
    let plan: Value<'py> = preview.getattr("plan")?;
    let mut execution_model_names: Vec<Value<'py>> = Vec::new();
    for key in items(&plan.getattr("execution_scope")?)? {
        execution_model_names.push(key.getattr("name")?);
    }
    let all_models: Value<'py> =
        class(py, PLANNER_TYPES, "DirectSelectionMode")?.getattr("ALL_MODELS")?;
    let all_selected: Value<'py> = native(
        py,
        "select_direct_build_audits",
        &[
            (
                "audits",
                preview
                    .getattr("analysis")?
                    .getattr("compiled_project")?
                    .getattr("audits")?,
            ),
            (
                "execution_model_names",
                PyFrozenSet::new(py, execution_model_names)?.into_any(),
            ),
            (
                "full_build",
                plan.getattr("selection_mode")?
                    .rich_compare(&all_models, pyo3::basic::CompareOp::Eq)?,
            ),
        ],
    )?;
    let all_selected: Vec<Value<'py>> = items(&all_selected)?;
    let mut selected: Vec<Value<'py>> = Vec::new();
    for audit in &all_selected {
        if audit.getattr("warmup_seconds")?.eq(0)? {
            selected.push(audit.clone());
        }
    }
    let mut deferred: Vec<Value<'py>> = Vec::new();
    for audit in &all_selected {
        if audit.getattr("warmup_seconds")?.gt(0)? {
            deferred.push(audit.clone());
        }
    }
    let deferred_count: usize = deferred.len();
    let mut node_results: Vec<Value<'py>> =
        run.audit_node_results(&invocation, selected, &audit_result)?;
    node_results.extend(run.deferred_audit_node_results(&invocation, deferred)?);
    run.persist(invocation, tuple(py, node_results)?)?;
    complete_run(&sink, outcome, exit_code, None)?;
    if !options.getattr("events_output")?.is_truthy()? {
        if deferred_count > 0 {
            print_text(
                py,
                &format!("Deferred {deferred_count} audit(s) for post-build warmup."),
                true,
            )?;
        }
        let renderer: &str = if options.getattr("json_output")?.is_truthy()? {
            "render_direct_build_json"
        } else {
            "render_direct_build_text"
        };
        let rendered: Value<'py> = native(
            py,
            renderer,
            &[
                ("result", execution.getattr("build_result")?),
                ("adapter_name", preview.getattr("adapter_name")?),
                ("audit_result", audit_result),
            ],
        )?;
        print(py, &rendered, false)?;
    }
    Ok(exit_code)
}

// Virtual builds.

struct VirtualRun<'a, 'py> {
    py: Python<'py>,
    preparation: &'a Value<'py>,
    preview: Value<'py>,
    options: &'a Value<'py>,
    client: &'a Value<'py>,
    started: &'a Value<'py>,
    sink: Value<'py>,
}

impl<'py> VirtualRun<'_, 'py> {
    /// Whether the build only switches policy on live views, creating no deployment.
    fn policy_only(&self) -> PyResult<bool> {
        Ok(self
            .preview
            .getattr("row_error_policy_changes")?
            .is_truthy()?
            && !self
                .preview
                .getattr("plan")?
                .getattr("rebuild_subtrees")?
                .is_truthy()?)
    }

    fn persist(
        &self,
        outcome: &str,
        exit_code: i64,
        materialized_outcome: Option<&str>,
        error_message: Value<'py>,
    ) -> PyResult<()> {
        let py: Python<'py> = self.py;
        let deployment_id: Value<'py> = if self.policy_only()? {
            none(py)
        } else {
            self.preview.getattr("deployment_id")?
        };
        let invocation: Value<'py> = invocation_record(
            py,
            self.started,
            &[
                ("project_dir", project_dir(self.options)?),
                ("target_identity", self.preview.getattr("database")?),
                ("command", text(py, "build")),
                ("mode", text(py, VIRTUAL_MODE)),
                ("outcome", text(py, outcome)),
                ("exit_code", int(py, exit_code)?),
                (
                    "materialized_outcome",
                    materialized_outcome.map_or_else(|| none(py), |value| text(py, value)),
                ),
                ("deployment_id", deployment_id),
                ("workflow_id", none(py)),
                (
                    "selected_node_count",
                    int(
                        py,
                        self.preview.getattr("run_execution_scope")?.len()? as i64,
                    )?,
                ),
                ("error_message", error_message),
                ("summary", PyDict::new(py).into_any()),
            ],
        )?;
        persist_terminal(
            py,
            self.client,
            self.preview.getattr("metadata_database")?,
            invocation,
            tuple(py, Vec::new())?,
        )
    }

    /// Record a cancelled build and print the interruption message with the cause's notes.
    fn cancel(&self, cause: Option<&Value<'py>>) -> PyResult<i64> {
        let py: Python<'py> = self.py;
        self.persist("cancelled", SIGINT_EXIT_CODE, None, none(py))?;
        complete_run(&self.sink, "cancelled", SIGINT_EXIT_CODE, None)?;
        quietly(py, || {
            let message: Value<'py> = native(py, "render_interrupted_build_message", &[])?;
            print(py, &message, true)?;
            let notes: Value<'py> = match cause {
                Some(cause) => match cause.getattr("__notes__") {
                    Ok(notes) => notes,
                    Err(missing)
                        if missing.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) =>
                    {
                        tuple(py, Vec::new())?
                    }
                    Err(failure) => return Err(failure),
                },
                None => tuple(py, Vec::new())?,
            };
            for note in notes.try_iter()? {
                print(py, &note?, true)?;
            }
            Ok(())
        })?;
        Ok(SIGINT_EXIT_CODE)
    }

    /// `cancel` run inside the handler of `handled`.
    fn cancel_handling(&self, handled: &PyErr, cause: Option<&Value<'py>>) -> PyResult<i64> {
        self.cancel(cause)
            .map_err(|raised| while_handling(self.py, raised, handled))
    }

    fn reset_fresh_landing_offsets(&self) -> PyResult<()> {
        let py: Python<'py> = self.py;
        let pair: Value<'py> = native(
            py,
            "plan_population_sources",
            &[
                ("desired_state", self.preview.getattr("desired_state")?),
                ("default_database", self.preview.getattr("database")?),
                (
                    "existing_relation_names",
                    self.preview.getattr("existing_relation_names")?,
                ),
            ],
        )?;
        let (source_preparation, _): (Value<'py>, Value<'py>) = pair.extract()?;
        reset_fresh_landing_offsets(py, source_preparation)
    }

    /// Execute the published workflow under the lock, running recovery when it fails.
    fn execute(&self, published: Value<'py>, acquire_lock: bool) -> PyResult<Value<'py>> {
        let py: Python<'py> = self.py;
        let workflow: Value<'py> = self.preparation.getattr("workflow")?;
        let manager: Value<'py> = lock_context(
            py,
            self.client,
            self.preparation
                .getattr("request")?
                .getattr("default_database")?,
            acquire_lock,
        )?;
        within(&manager, || {
            self.reset_fresh_landing_offsets()?;
            match call(
                py,
                BUILD_WORKFLOW,
                "execute_build_workflow",
                &[
                    ("published_workflow", published),
                    ("connection", self.client.clone()),
                    ("emitter", self.sink.clone()),
                ],
            ) {
                Ok(execution) => Ok(execution),
                Err(error)
                    if caught(py, &error, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?
                        || caught(py, &error, ADAPTER_EXCEPTIONS, "AdapterError")?
                        || error.is_instance_of::<PyKeyboardInterrupt>(py) =>
                {
                    let recover = || -> PyResult<()> {
                        let raised: Value<'py> = error.value(py).clone().into_any();
                        let workflow_error: bool =
                            is_a(&raised, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?;
                        let cause: Value<'py> = if workflow_error {
                            raised.getattr("cause")?
                        } else {
                            raised.clone()
                        };
                        let start_sequence: Value<'py> =
                            int(py, workflow.getattr("statements")?.len()? as i64 + 1)?;
                        call(
                            py,
                            RECOVERY_STATEMENTS,
                            "execute_recovery_statements",
                            &[
                                ("statements", workflow.getattr("recovery_statements")?),
                                ("connection", self.client.clone()),
                                ("emitter", self.sink.clone()),
                                ("cause", cause),
                                ("start_sequence", start_sequence),
                                ("completed_step_ids", completed_step_ids(py, &raised)?),
                                ("retry_command", text(py, "sqt build")),
                            ],
                        )?;
                        Ok(())
                    };
                    match recover() {
                        Ok(()) => Err(error),
                        Err(raised) => Err(while_handling(py, raised, &error)),
                    }
                }
                Err(error) => Err(error),
            }
        })
    }

    /// The handler for a failed workflow or adapter call: cancel on an interrupted recovery,
    /// otherwise record and print the failure.
    fn fail(&self, error: &PyErr) -> PyResult<i64> {
        let py: Python<'py> = self.py;
        let raised: Value<'py> = error.value(py).clone().into_any();
        let workflow_error: bool = is_a(&raised, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?;
        let cause: Value<'py> = if workflow_error {
            raised.getattr("cause")?
        } else {
            raised.clone()
        };
        if self
            .preparation
            .getattr("workflow")?
            .getattr("recovery_statements")?
            .is_truthy()?
            && cause.is_instance_of::<PyKeyboardInterrupt>()
        {
            return self.cancel(Some(&cause));
        }
        let policy_text: Value<'py> = if workflow_error {
            native(
                py,
                "row_error_policy_failure_message",
                &[
                    ("changes", self.preview.getattr("row_error_policy_changes")?),
                    ("failed_step_id", raised.getattr("failed_step_id")?),
                    ("cause", cause.clone()),
                ],
            )?
        } else {
            none(py)
        };
        let failure_text: Value<'py> = if policy_text.is_truthy()? {
            policy_text
        } else {
            cause.str()?.into_any()
        };
        self.persist("failed", 1, None, failure_text.clone())?;
        complete_run(&self.sink, "failed", 1, Some(failure_text.clone()))?;
        print(py, &failure_text, true)?;
        Ok(1)
    }

    fn succeed(&self) -> PyResult<()> {
        self.persist("succeeded", 0, Some("applied"), none(self.py))
    }
}

/// The steps a failed workflow finished, or `None` when that is unknown.
fn completed_step_ids<'py>(py: Python<'py>, error: &Value<'py>) -> PyResult<Value<'py>> {
    if !is_a(error, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")? {
        return Ok(none(py));
    }
    let partial: Value<'py> = error.getattr("partial_result")?;
    if !is_a(&partial, WORKFLOW_MODELS, "WorkflowExecutionResult")? {
        return Ok(none(py));
    }
    let mut step_ids: Vec<Value<'py>> = Vec::new();
    for result in items(&partial.getattr("statement_results")?)? {
        step_ids.push(result.getattr("step_id")?);
    }
    Ok(PyFrozenSet::new(py, step_ids)?.into_any())
}

/// Confirm and populate one prepared isolated virtual deployment.
#[pyfunction]
#[pyo3(signature = (
    *,
    preparation,
    options,
    client,
    observation_client,
    started,
    confirmation_required=true,
    startup_timings=None,
    _acquire_target_mutation_lock=true,
))]
#[allow(clippy::too_many_arguments)]
fn execute_virtual_build_command<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    started: &Value<'py>,
    confirmation_required: bool,
    startup_timings: Option<Value<'py>>,
    _acquire_target_mutation_lock: bool,
) -> PyResult<i64> {
    let acquire_lock: bool = _acquire_target_mutation_lock;
    let preview: Value<'py> = preparation.getattr("preview")?;
    let sink: Value<'py> = event_sink(py, options, observation_client, &preview, started)?;
    let run: VirtualRun<'_, 'py> = VirtualRun {
        py,
        preparation,
        preview: preview.clone(),
        options,
        client,
        started,
        sink: sink.clone(),
    };
    let begin = || -> PyResult<bool> {
        if !sink.is_none() {
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("command", "build")?;
            arguments.set_item("mode", VIRTUAL_MODE)?;
            arguments.set_item(
                "total_statements",
                preparation
                    .getattr("workflow")?
                    .getattr("statements")?
                    .len()?,
            )?;
            arguments.set_item(
                "selected_node_count",
                preview.getattr("run_execution_scope")?.len()?,
            )?;
            arguments.set_item("selectors", options.getattr("selectors")?)?;
            arguments.set_item("start_time", options.getattr("start_time")?)?;
            arguments.set_item(
                "executed_logical_ids",
                resource_identities(py, preview.getattr("run_execution_scope")?)?,
            )?;
            arguments.set_item(
                "context_logical_ids",
                resource_identities(py, preview.getattr("run_context_scope")?)?,
            )?;
            arguments.set_item(
                "startup_timings",
                startup_timings.clone().unwrap_or_else(|| none(py)),
            )?;
            sink.call_method("run_started", (), Some(&arguments))?;
        }
        if confirmation_required {
            confirm(py, options, preparation)
        } else {
            Ok(true)
        }
    };
    let confirmed: bool = match begin() {
        Ok(confirmed) => confirmed,
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
            return run.cancel_handling(&error, None);
        }
        Err(error) => return Err(error),
    };
    if !confirmed {
        run.persist("cancelled", 1, None, none(py))?;
        complete_run(&sink, "cancelled", 1, None)?;
        print_text(py, CANCELLED, false)?;
        return Ok(1);
    }
    let published: Value<'py> = match call(
        py,
        PUBLISH_BUILD,
        "publish_build_workflow",
        &[
            ("target_dir", target_dir(options)?),
            ("workflow", preparation.getattr("workflow")?),
        ],
    ) {
        Ok(published) => published,
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
            return run.cancel_handling(&error, None);
        }
        Err(error) if error.is_instance_of::<PyOSError>(py) => {
            let handle = || -> PyResult<i64> {
                let message: Value<'py> = error.value(py).str()?.into_any();
                run.persist("failed", 1, None, message)?;
                run_completed(&sink, "failed", 1, Some(error.value(py).str()?.into_any()))?;
                quietly(py, || print(py, error.value(py).str()?.as_any(), true))?;
                Ok(1)
            };
            return handle().map_err(|raised| while_handling(py, raised, &error));
        }
        Err(error) => return Err(error),
    };
    let execution: Value<'py> = match run.execute(published, acquire_lock) {
        Ok(execution) => execution,
        Err(error)
            if caught(py, &error, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?
                || caught(py, &error, ADAPTER_EXCEPTIONS, "AdapterError")? =>
        {
            return run
                .fail(&error)
                .map_err(|raised| while_handling(py, raised, &error));
        }
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
            let cause: Value<'py> = error.value(py).clone().into_any();
            return run.cancel_handling(&error, Some(&cause));
        }
        Err(error) => return Err(error),
    };
    if run.policy_only()? {
        run.succeed()?;
        run_completed(&sink, "succeeded", 0, None)?;
        print(py, &preparation.getattr("plan_text")?, false)?;
        return Ok(0);
    }
    let result: Value<'py> = match native(
        py,
        "build_virtual_execution_result",
        &[
            ("request", preparation.getattr("request")?),
            ("root_reports", preview.getattr("root_reports")?),
            (
                "existing_relation_names",
                preview.getattr("existing_relation_names")?,
            ),
            ("execution", execution),
        ],
    ) {
        Ok(result) => result,
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
            return run.cancel_handling(&error, None);
        }
        Err(error) => return Err(error),
    };
    run.succeed()?;
    complete_run(&sink, "succeeded", 0, None)?;
    let rendered: Value<'py> = native(
        py,
        "render_virtual_build_result",
        &[
            ("result", result),
            ("database", preview.getattr("database")?),
            ("json_output", options.getattr("json_output")?),
        ],
    )?;
    print(py, &rendered, false)?;
    Ok(0)
}

// Mixed builds.

/// One phase of a mixed build, run through `sqltide._native` so tests can replace it.
fn mixed_phase<'py>(
    py: Python<'py>,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    native(py, name, pairs)
}

/// Confirm once, stage virtual work, then apply direct work.
#[pyfunction]
#[pyo3(signature = (*, preparation, options, client, observation_client, started, startup_timings))]
fn execute_mixed_build_command<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    started: &Value<'py>,
    startup_timings: &Value<'py>,
) -> PyResult<Value<'py>> {
    if !confirm(py, options, preparation)? {
        print_text(py, CANCELLED, false)?;
        return int(py, 1);
    }
    let virtual_: Value<'py> = preparation.getattr("virtual")?;
    let manager: Value<'py> = call(
        py,
        MUTATION_LOCK,
        "target_mutation_lock",
        &[
            ("connection", client.clone()),
            (
                "database",
                virtual_.getattr("request")?.getattr("default_database")?,
            ),
            ("ensure_database", boolean(py, true)?),
        ],
    )?;
    within(&manager, || {
        let virtual_phase = || -> PyResult<Value<'py>> {
            mixed_phase(
                py,
                "execute_virtual_build_command",
                &[
                    ("preparation", preparation.getattr("virtual")?),
                    ("options", options.clone()),
                    ("client", client.clone()),
                    ("observation_client", observation_client.clone()),
                    ("started", started.clone()),
                    ("confirmation_required", boolean(py, false)?),
                    ("startup_timings", startup_timings.clone()),
                    ("_acquire_target_mutation_lock", boolean(py, false)?),
                ],
            )
        };
        let direct_phase = || -> PyResult<Value<'py>> {
            let direct: Value<'py> = preparation.getattr("direct")?;
            let started: Value<'py> = call(py, START_INVOCATION, "start_invocation", &[])?;
            mixed_phase(
                py,
                "execute_direct_build_command",
                &[
                    ("preparation", direct),
                    ("options", options.clone()),
                    ("client", client.clone()),
                    ("observation_client", observation_client.clone()),
                    ("started", started),
                    ("confirmation_required", boolean(py, false)?),
                    ("_acquire_target_mutation_lock", boolean(py, false)?),
                ],
            )
        };
        if options.getattr("json_output")?.is_truthy()? {
            return execute_json_build(py, virtual_phase, direct_phase);
        }
        let events_output: bool = options.getattr("events_output")?.is_truthy()?;
        if !events_output {
            print_text(py, "\nPhase 1/2  VIRTUAL - staging deployment", false)?;
        }
        let virtual_exit_code: Value<'py> = virtual_phase()?;
        if virtual_exit_code.is_truthy()? {
            if !events_output {
                print_text(
                    py,
                    "Direct phase skipped because virtual staging failed.",
                    true,
                )?;
            }
            return Ok(virtual_exit_code);
        }
        if !events_output {
            print_text(py, "\nPhase 2/2  DIRECT - applying immediately", false)?;
        }
        let direct_exit_code: Value<'py> = direct_phase()?;
        if !events_output {
            let deployment_id: String = crate::build_cli::formatted(
                &virtual_.getattr("preview")?.getattr("deployment_id")?,
            )?;
            if direct_exit_code.is_truthy()? {
                print_text(
                    py,
                    &format!(
                        "Mixed build failed during the direct phase. The virtual deployment \
                         remains staged as {deployment_id}."
                    ),
                    true,
                )?;
            } else {
                print_text(
                    py,
                    &format!(
                        "\nMixed Build Complete\nDirect changes are live. Virtual changes remain \
                         staged until promoted with:\n  sqt deployment promote {deployment_id}"
                    ),
                    false,
                )?;
            }
        }
        Ok(direct_exit_code)
    })
}

/// Run both phases with their output captured, then print one combined JSON document.
fn execute_json_build<'py>(
    py: Python<'py>,
    virtual_phase: impl FnOnce() -> PyResult<Value<'py>>,
    direct_phase: impl FnOnce() -> PyResult<Value<'py>>,
) -> PyResult<Value<'py>> {
    let captured = |phase: Box<dyn FnOnce() -> PyResult<Value<'py>> + '_>| {
        let output: Value<'py> = py.import("io")?.getattr("StringIO")?.call0()?;
        let manager: Value<'py> = py
            .import("contextlib")?
            .getattr("redirect_stdout")?
            .call1((&output,))?;
        let exit_code: Value<'py> = within(&manager, phase)?;
        PyResult::Ok((exit_code, output))
    };
    let (virtual_exit_code, virtual_output) = captured(Box::new(virtual_phase))?;
    if virtual_exit_code.is_truthy()? {
        return Ok(virtual_exit_code);
    }
    let (direct_exit_code, direct_output) = captured(Box::new(direct_phase))?;
    if direct_exit_code.is_truthy()? {
        return Ok(direct_exit_code);
    }
    let loads: Value<'py> = py.import("json")?.getattr("loads")?;
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("mode", "mixed")?;
    payload.set_item("execution_order", PyList::new(py, ["virtual", "direct"])?)?;
    payload.set_item(
        "virtual",
        loads.call1((virtual_output.call_method0("getvalue")?,))?,
    )?;
    payload.set_item(
        "direct",
        loads.call1((direct_output.call_method0("getvalue")?,))?,
    )?;
    print_text(py, &json_dumps(payload.as_any(), Some(2), false)?, false)?;
    int(py, 0)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_direct_build_command, module)?)?;
    module.add_function(wrap_pyfunction!(execute_virtual_build_command, module)?)?;
    module.add_function(wrap_pyfunction!(execute_mixed_build_command, module)?)
}
