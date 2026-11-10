//! Destruction execution over the adapter connection: dependency-ordered drop workflows, the
//! fresh-plan drift check, and recorded execution of a reviewed, actor-bound plan under the
//! target lock with durable evidence. Statements execute through the Python workflow executor
//! and evidence flows through the Python run event sink; the ordering and drop SQL are the
//! native destruction planner's.

use pyo3::PyTypeInfo;
use pyo3::exceptions::{PyKeyboardInterrupt, PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use sqltide_executor::destruction::{RelationKind, drop_statements};
use sqltide_executor::ordering::{OrderingError, reverse_topological_order};
use sqltide_planner::direct::sha256_hex;

use crate::handling::while_handling;
use crate::model_sql::chained;

const DESTRUCTION_MODELS: &str = "sqltide.executor.destruction.models";
const DESTRUCTION_TYPES: &str = "sqltide.executor.destruction.types";
const DESTRUCTION_EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const WORKFLOW_EXCEPTIONS: &str = "sqltide.executor.workflow.exceptions";
const WAREHOUSE_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_warehouse_workflow";
const OBSERVABILITY_MODELS: &str = "sqltide.executor.observability.models";
const RUN_EVENT_SINK: &str = "sqltide.executor.observability.classes.run_event_sink";
const START_INVOCATION: &str = "sqltide.executor.observability.main.start_invocation";
const INITIALIZE_OBSERVABILITY: &str = "sqltide._native";
const PROJECT_IDENTITY: &str = "sqltide.executor.observability.main.logical_project_identity";
const INVOCATION_RECORD: &str = "sqltide.executor.observability.main.build_invocation_record";
const TERMINAL_OBSERVATIONS: &str =
    "sqltide.executor.observability.main.persist_terminal_observations";
const RESIDUAL_CATALOG_STATUS_OBSERVED: &str = "observed";
const DESTROY_STEP_PREFIX: &str = "destroy_relation_";

type Value<'py> = Bound<'py, PyAny>;

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn keywords<'py>(py: Python<'py>, pairs: &[(&str, Value<'py>)]) -> PyResult<Bound<'py, PyDict>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in pairs {
        arguments.set_item(keyword, value)?;
    }
    Ok(arguments)
}

/// `module.name(**pairs)`, looked up when called.
fn call<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    class(py, module, name)?.call((), Some(&keywords(py, pairs)?))
}

fn method<'py>(
    value: &Value<'py>,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    value.call_method(name, (), Some(&keywords(value.py(), pairs)?))
}

fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn text<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

fn none(py: Python<'_>) -> Value<'_> {
    py.None().into_bound(py)
}

fn int<'py>(py: Python<'py>, value: usize) -> PyResult<Value<'py>> {
    Ok(value.into_pyobject(py)?.into_any())
}

fn boolean(py: Python<'_>, value: bool) -> PyResult<Value<'_>> {
    Ok(value.into_pyobject(py)?.to_owned().into_any())
}

fn optional_text<'py>(py: Python<'py>, value: Option<&str>) -> Value<'py> {
    value.map_or_else(|| none(py), |value| text(py, value))
}

fn string(value: &Value<'_>, name: &str) -> PyResult<String> {
    value.getattr(name)?.extract()
}

fn error(py: Python<'_>, name: &str, message: String) -> PyErr {
    match class(py, DESTRUCTION_EXCEPTIONS, name).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn consistency_error(py: Python<'_>, message: &str) -> PyErr {
    error(py, "DestructionConsistencyError", message.to_owned())
}

/// `raise DestructionConsistencyError(message) from None` while handling the native refusal
/// `ValueError(refusal)`.
fn consistency_error_from_none(py: Python<'_>, message: String, refusal: Value<'_>) -> PyErr {
    let raised = || -> PyResult<PyErr> {
        let translated: Value<'_> =
            class(py, DESTRUCTION_EXCEPTIONS, "DestructionConsistencyError")?.call1((message,))?;
        let context: Value<'_> =
            PyValueError::type_object(py).call1(refusal.downcast::<PyTuple>()?)?;
        chained(&translated, &context, None)?;
        Ok(PyErr::from_value(translated))
    };
    raised().unwrap_or_else(|error| error)
}

/// Whether `error` is an `Exception`, which a Python `except Exception` handler catches.
fn is_exception(py: Python<'_>, error: &PyErr) -> bool {
    error.is_instance_of::<pyo3::exceptions::PyException>(py)
}

/// Relations with dependants before prerequisites and deterministic lexical ties.
fn reverse_topologically_order_relations<'py>(
    py: Python<'py>,
    relations: &[Value<'py>],
) -> PyResult<Vec<Value<'py>>> {
    let mut arguments: Vec<(String, Vec<String>)> = Vec::with_capacity(relations.len());
    for relation in relations {
        arguments.push((
            string(relation, "name")?,
            relation.getattr("dependency_relation_names")?.extract()?,
        ));
    }
    let positions: Vec<usize> = match reverse_topological_order(&arguments) {
        Ok(positions) => positions,
        Err(OrderingError::Duplicate(position)) => {
            let duplicate: Value<'py> = relations[position].getattr("name")?;
            return Err(consistency_error_from_none(
                py,
                format!(
                    "Destruction evidence contains duplicate relation {}",
                    duplicate.repr()?
                ),
                PyTuple::new(py, [text(py, "duplicate"), int(py, position)?])?.into_any(),
            ));
        }
        Err(OrderingError::Cycle(names)) => {
            let unresolved: Value<'py> = PyTuple::new(py, &names)?.into_any();
            return Err(consistency_error_from_none(
                py,
                format!(
                    "Destruction relation dependency cycle blocks ordering: {}",
                    unresolved.repr()?
                ),
                PyTuple::new(py, [text(py, "cycle"), PyList::new(py, &names)?.into_any()])?
                    .into_any(),
            ));
        }
    };
    Ok(positions
        .into_iter()
        .map(|position| relations[position].clone())
        .collect())
}

/// Drop frozen owned relations in stable reverse-dependency order.
#[pyfunction]
#[pyo3(signature = (*, plan, connection = None))]
fn assemble_destruction_workflow<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    connection: Option<&Value<'py>>,
) -> PyResult<Value<'py>> {
    let _ = connection;
    let kind_class: Value<'py> = class(py, DESTRUCTION_TYPES, "DestructionRelationKind")?;
    let mut drops: Vec<(RelationKind, String, String)> = Vec::new();
    for relation in reverse_topologically_order_relations(py, &items(&plan.getattr("relations")?)?)?
    {
        if !relation.getattr("exists")?.is_truthy()? {
            continue;
        }
        let kind: String = kind_class
            .call1((relation.getattr("kind")?,))?
            .str()?
            .extract()?;
        let kind: RelationKind = RelationKind::parse(&kind)
            .ok_or_else(|| PyValueError::new_err(format!("Unknown relation kind '{kind}'")))?;
        drops.push((
            kind,
            string(&relation, "database")?,
            string(&relation, "name")?,
        ));
    }
    let size_override: Option<i128> = plan.getattr("relation_drop_size_override")?.extract()?;
    let statement_class: Value<'py> = class(py, WORKFLOW_MODELS, "WarehouseStatement")?;
    let teardown: Value<'py> = class(py, WORKFLOW_TYPES, "WorkflowPhase")?.getattr("TEARDOWN")?;
    let mutation: Value<'py> = class(py, WORKFLOW_TYPES, "StatementIntent")?.getattr("MUTATION")?;
    let mut statements: Vec<Value<'py>> = Vec::with_capacity(drops.len());
    for (position, statement) in drop_statements(&drops, size_override)
        .into_iter()
        .enumerate()
    {
        statements.push(statement_class.call(
            (),
            Some(&keywords(
                py,
                &[
                    ("sequence", int(py, position + 1)?),
                    ("step_id", text(py, &statement.step_id)),
                    ("phase", teardown.clone()),
                    ("intent", mutation.clone()),
                    ("display_name", text(py, &statement.display_name)),
                    ("sql", text(py, &statement.sql)),
                ],
            )?),
        )?);
    }
    tuple(py, statements)
}

/// Replan immediately and reject manifest or warehouse impact drift.
#[pyfunction]
#[pyo3(signature = (*, frozen_plan, replan))]
fn verify_destruction_drift<'py>(
    py: Python<'py>,
    frozen_plan: &Value<'py>,
    replan: &Value<'py>,
) -> PyResult<Value<'py>> {
    let current_plan: Value<'py> = match replan.call0() {
        Ok(plan) => plan,
        Err(failure) => {
            let drift: Value<'py> = class(py, DESTRUCTION_EXCEPTIONS, "DestructionDriftError")?;
            if failure.is_instance(py, &drift)
                || !failure.is_instance(py, &class(py, DESTRUCTION_EXCEPTIONS, "DestructionError")?)
            {
                return Err(failure);
            }
            let translate = || -> PyResult<PyErr> {
                let handled: Value<'py> = failure.value(py).clone().into_any();
                let translated: Value<'py> = drift.call1((format!(
                    "Current destruction impact can no longer be planned: {}",
                    handled.str()?
                ),))?;
                translated.setattr("__context__", &handled)?;
                translated.setattr("__cause__", &handled)?;
                Ok(PyErr::from_value(translated))
            };
            return Err(translate().unwrap_or_else(|raised| while_handling(py, raised, &failure)));
        }
    };
    if current_plan
        .getattr("manifest_fingerprint")?
        .ne(frozen_plan.getattr("manifest_fingerprint")?)?
    {
        return Err(error(
            py,
            "DestructionDriftError",
            "Current manifest differs from the reviewed destruction plan".to_owned(),
        ));
    }
    if current_plan
        .getattr("plan_fingerprint")?
        .ne(frozen_plan.getattr("plan_fingerprint")?)?
    {
        return Err(error(
            py,
            "DestructionDriftError",
            "Current destruction impact differs from the reviewed plan".to_owned(),
        ));
    }
    Ok(current_plan)
}

fn command(py: Python<'_>, operation: &Value<'_>) -> PyResult<&'static str> {
    let destroy: Value<'_> =
        class(py, DESTRUCTION_TYPES, "DestructionOperation")?.getattr("DESTROY_PIPELINES")?;
    Ok(if operation.eq(destroy)? {
        "destroy pipelines"
    } else {
        "reset target"
    })
}

fn names_list<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(PyList::new(py, items(value)?)?.into_any())
}

fn int_list<'py>(py: Python<'py>, values: &[Value<'py>]) -> PyResult<Value<'py>> {
    Ok(PyList::new(py, values)?.into_any())
}

/// The progress of a recorded destruction for its operation evidence.
struct Progress<'py, 'a> {
    completed_sequences: &'a [Value<'py>],
    pending_sequences: &'a [Value<'py>],
    remaining: Option<&'a [String]>,
    residual_catalog_status: &'a str,
    residual_catalog_error: Option<&'a str>,
    failure_phase: Option<&'a str>,
}

/// The operation evidence of one destruction attempt, in its persisted key order.
fn operation_evidence<'py>(
    py: Python<'py>,
    context: &Value<'py>,
    progress: &Progress<'py, '_>,
) -> PyResult<Bound<'py, PyDict>> {
    let plan: Value<'py> = context.getattr("plan")?;
    let evidence: Bound<'py, PyDict> = PyDict::new(py);
    let actor: Bound<'py, PyDict> = PyDict::new(py);
    actor.set_item("id", context.getattr("actor_id")?)?;
    actor.set_item("username", context.getattr("actor_name")?)?;
    evidence.set_item("actor", actor)?;
    evidence.set_item("target", plan.getattr("target")?)?;
    evidence.set_item("database", plan.getattr("database")?)?;
    evidence.set_item(
        "operationKind",
        plan.getattr("operation")?.getattr("value")?,
    )?;
    evidence.set_item("planId", plan.getattr("plan_id")?)?;
    evidence.set_item("planFingerprint", plan.getattr("plan_fingerprint")?)?;
    evidence.set_item("manifestFingerprint", plan.getattr("manifest_fingerprint")?)?;
    for (key, attribute) in [
        ("originalSelection", "requested_pipeline_names"),
        (
            "includedDependentPipelines",
            "included_dependent_pipeline_names",
        ),
        ("affectedPipelines", "affected_pipeline_names"),
        ("affectedModels", "affected_model_names"),
        ("affectedSources", "affected_source_names"),
        ("expectedChallenges", "challenges"),
    ] {
        evidence.set_item(key, names_list(py, &plan.getattr(attribute)?)?)?;
    }
    evidence.set_item(
        "submittedChallenges",
        names_list(py, &context.getattr("challenge_responses")?)?,
    )?;
    evidence.set_item(
        "reviewedAt",
        context.getattr("reviewed_at")?.call_method0("isoformat")?,
    )?;
    evidence.set_item(
        "confirmedAt",
        context.getattr("confirmed_at")?.call_method0("isoformat")?,
    )?;
    evidence.set_item("estimatedActivePartBytes", plan.getattr("estimated_bytes")?)?;
    for (key, attribute) in [
        ("relationDropSizeLimit", "relation_drop_size_limit"),
        (
            "relationDropSizeServerLimit",
            "relation_drop_size_server_limit",
        ),
        ("relationDropSizeOverride", "relation_drop_size_override"),
        (
            "relationDropSizePolicyObserved",
            "relation_drop_size_policy_observed",
        ),
        ("includeOrphans", "include_orphans"),
    ] {
        evidence.set_item(key, plan.getattr(attribute)?)?;
    }
    evidence.set_item(
        "managedSourcesIncluded",
        !plan.getattr("preserves_sources")?.is_truthy()?,
    )?;
    evidence.set_item(
        "retainedReplayDataIncluded",
        !plan.getattr("preserves_replay_data")?.is_truthy()?,
    )?;
    evidence.set_item("kafkaOffsetsReset", false)?;
    let planned: Bound<'py, PyList> = PyList::empty(py);
    for relation in items(&plan.getattr("relations")?)? {
        let entry: Bound<'py, PyDict> = PyDict::new(py);
        entry.set_item("database", relation.getattr("database")?)?;
        entry.set_item("name", relation.getattr("name")?)?;
        entry.set_item("kind", relation.getattr("kind")?.str()?)?;
        entry.set_item(
            "catalogFingerprint",
            relation.getattr("catalog_fingerprint")?,
        )?;
        entry.set_item(
            "dependencies",
            names_list(py, &relation.getattr("dependency_relation_names")?)?,
        )?;
        planned.append(entry)?;
    }
    evidence.set_item("plannedRelations", planned)?;
    evidence.set_item(
        "completedStatementSequences",
        int_list(py, progress.completed_sequences)?,
    )?;
    evidence.set_item(
        "pendingStatementSequences",
        int_list(py, progress.pending_sequences)?,
    )?;
    evidence.set_item(
        "remainingObjects",
        match progress.remaining {
            Some(remaining) => PyList::new(py, remaining)?.into_any(),
            None => none(py),
        },
    )?;
    evidence.set_item("residualCatalogStatus", progress.residual_catalog_status)?;
    evidence.set_item(
        "residualCatalogError",
        optional_text(py, progress.residual_catalog_error),
    )?;
    evidence.set_item("failurePhase", optional_text(py, progress.failure_phase))?;
    Ok(evidence)
}

/// The planned relations the warehouse still holds, sorted by name.
fn remaining_relations(context: &Value<'_>) -> PyResult<Vec<String>> {
    let plan: Value<'_> = context.getattr("plan")?;
    let names: Value<'_> = context
        .getattr("connection")?
        .call_method1("load_catalog", (plan.getattr("database")?,))?
        .call_method0("relation_names")?;
    let mut remaining: Vec<String> = Vec::new();
    for relation in items(&plan.getattr("relations")?)? {
        let name: Value<'_> = relation.getattr("name")?;
        if names.contains(&name)? {
            remaining.push(name.extract()?);
        }
    }
    remaining.sort();
    Ok(remaining)
}

fn existing_relation_names(plan: &Value<'_>) -> PyResult<Vec<String>> {
    let mut names: Vec<String> = Vec::new();
    for relation in items(&plan.getattr("relations")?)? {
        if relation.getattr("exists")?.is_truthy()? {
            names.push(string(&relation, "name")?);
        }
    }
    Ok(names)
}

fn verify_completed_prefix(
    py: Python<'_>,
    statements: &[Value<'_>],
    execution: &Value<'_>,
) -> PyResult<()> {
    let results: Vec<Value<'_>> = items(&execution.getattr("statement_results")?)?;
    let expected: Vec<Value<'_>> = statements
        .iter()
        .take(results.len())
        .map(|statement| statement.getattr("step_id"))
        .collect::<PyResult<_>>()?;
    let actual: Vec<Value<'_>> = results
        .iter()
        .map(|result| result.getattr("step_id"))
        .collect::<PyResult<_>>()?;
    if !PyTuple::new(py, actual)?.eq(PyTuple::new(py, expected)?)? {
        return Err(consistency_error(
            py,
            "Destruction execution results are not an exact workflow prefix",
        ));
    }
    Ok(())
}

fn context_statements<'py>(context: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    items(&context.getattr("statements")?)
}

fn sequences<'py>(statements: &[Value<'py>]) -> PyResult<Vec<Value<'py>>> {
    statements
        .iter()
        .map(|statement| statement.getattr("sequence"))
        .collect()
}

/// `f"{message}; {detail}"`, or `fresh` when there is no message yet.
fn appended(message: Option<String>, fresh: String, detail: String) -> String {
    match message {
        None => fresh,
        Some(message) => format!("{message}; {detail}"),
    }
}

/// Persist terminal observations, returning a warning instead of raising.
fn persist_terminal<'py>(
    py: Python<'py>,
    connection: &Value<'py>,
    plan: &Value<'py>,
    invocation: &Value<'py>,
) -> PyResult<Option<String>> {
    match call(
        py,
        TERMINAL_OBSERVATIONS,
        "persist_terminal_observations",
        &[
            ("client", connection.clone()),
            ("database", plan.getattr("metadata_database")?),
            ("invocation", invocation.clone()),
            ("node_results", PyTuple::empty(py).into_any()),
        ],
    ) {
        Ok(warning) if warning.is_none() => Ok(None),
        Ok(warning) => Ok(Some(warning.str()?.extract()?)),
        Err(failure) if is_exception(py, &failure) => {
            let warning = || -> PyResult<Option<String>> {
                Ok(Some(format!(
                    "Terminal observations were not recorded: {}",
                    failure.value(py).str()?
                )))
            };
            warning().map_err(|raised| while_handling(py, raised, &failure))
        }
        Err(failure) => Err(failure),
    }
}

/// The terminal state of one recorded destruction.
struct Terminal<'a> {
    remaining: Option<&'a [String]>,
    residual_catalog_status: &'a str,
    residual_catalog_error: Option<&'a str>,
    error_message: Option<String>,
    failure_phase: Option<String>,
}

/// Record the terminal evidence of a destruction attempt and return its result.
fn record_terminal<'py>(
    py: Python<'py>,
    context: &Value<'py>,
    execution: &Value<'py>,
    terminal: Terminal<'_>,
) -> PyResult<Value<'py>> {
    let Terminal {
        remaining,
        residual_catalog_status,
        residual_catalog_error,
        mut error_message,
        mut failure_phase,
    } = terminal;
    let statements: Vec<Value<'py>> = context_statements(context)?;
    verify_completed_prefix(py, &statements, execution)?;
    let results: Vec<Value<'py>> = items(&execution.getattr("statement_results")?)?;
    let completed_count: usize = results.len().min(statements.len());
    let completed_sequences: Vec<Value<'py>> = sequences(&statements[..completed_count])?;
    let pending_sequences: Vec<Value<'py>> = sequences(&statements[completed_count..])?;
    let succeeded: bool = error_message.is_none()
        && residual_catalog_status == RESIDUAL_CATALOG_STATUS_OBSERVED
        && remaining.is_some_and(<[String]>::is_empty)
        && pending_sequences.is_empty();
    let mut outcome: &str = if succeeded { "succeeded" } else { "failed" };
    let mut exit_code: usize = usize::from(!succeeded);
    let sink: Value<'py> = context.getattr("sink")?;
    sink.call_method1(
        "set_operation_evidence",
        (operation_evidence(
            py,
            context,
            &Progress {
                completed_sequences: &completed_sequences,
                pending_sequences: &pending_sequences,
                remaining,
                residual_catalog_status,
                residual_catalog_error,
                failure_phase: failure_phase.as_deref(),
            },
        )?,),
    )?;
    let mut run_completed_error: Option<String> = None;
    let completion: PyResult<Value<'py>> = method(
        &sink,
        "run_completed",
        &[
            ("outcome", text(py, outcome)),
            ("exit_code", int(py, exit_code)?),
            ("error_message", optional_text(py, error_message.as_deref())),
        ],
    );
    let pending: Option<PyErr> = match completion {
        Ok(_) => None,
        Err(failure) if is_exception(py, &failure) => {
            let handle = || -> PyResult<String> { failure.value(py).str()?.extract() };
            let detail: String = handle().map_err(|raised| {
                let raised: PyErr = while_handling(py, raised, &failure);
                // The handler's own failure still runs the `finally` stop.
                match sink.call_method0("stop") {
                    Ok(_) => raised,
                    Err(stop) => while_handling(py, stop, &raised),
                }
            })?;
            run_completed_error = Some(detail.clone());
            outcome = "failed";
            exit_code = 1;
            error_message = Some(appended(
                error_message,
                format!("Run completion evidence failed: {detail}"),
                format!("run completion evidence failed: {detail}"),
            ));
            if failure_phase.is_none() {
                failure_phase = Some("run_completed".to_owned());
            }
            None
        }
        Err(failure) => Some(failure),
    };
    if let Err(stop) = sink.call_method0("stop") {
        return Err(match pending {
            Some(pending) => while_handling(py, stop, &pending),
            None => stop,
        });
    }
    if let Some(pending) = pending {
        return Err(pending);
    }
    let summary: Bound<'py, PyDict> = operation_evidence(
        py,
        context,
        &Progress {
            completed_sequences: &completed_sequences,
            pending_sequences: &pending_sequences,
            remaining,
            residual_catalog_status,
            residual_catalog_error,
            failure_phase: failure_phase.as_deref(),
        },
    )?;
    summary.set_item(
        "runCompletedError",
        optional_text(py, run_completed_error.as_deref()),
    )?;
    let plan: Value<'py> = context.getattr("plan")?;
    let mut applied: bool = false;
    for result in &results {
        if string(result, "step_id")?.starts_with(DESTROY_STEP_PREFIX) {
            applied = true;
            break;
        }
    }
    let terminal_invocation: Value<'py> = call(
        py,
        OBSERVABILITY_MODELS,
        "TerminalInvocation",
        &[
            ("project_dir", context.getattr("project_dir")?),
            ("target_identity", plan.getattr("target")?),
            (
                "command",
                text(py, command(py, &plan.getattr("operation")?)?),
            ),
            ("mode", text(py, "destructive")),
            ("outcome", text(py, outcome)),
            ("exit_code", int(py, exit_code)?),
            (
                "materialized_outcome",
                optional_text(py, applied.then_some("applied")),
            ),
            ("deployment_id", none(py)),
            ("workflow_id", plan.getattr("plan_fingerprint")?),
            (
                "selected_node_count",
                int(py, plan.getattr("affected_model_names")?.len()?)?,
            ),
            ("error_message", optional_text(py, error_message.as_deref())),
            ("summary", summary.into_any()),
        ],
    )?;
    let invocation: Value<'py> = call(
        py,
        INVOCATION_RECORD,
        "build_invocation_record",
        &[
            ("started", context.getattr("started")?),
            ("terminal", terminal_invocation),
        ],
    )?;
    let connection: Value<'py> = context.getattr("connection")?;
    let observation_connection: Value<'py> = context.getattr("observation_connection")?;
    let shared: bool = connection.is(&observation_connection);
    let primary_warning: Option<String> =
        persist_terminal(py, &observation_connection, &plan, &invocation)?;
    let mut fallback_warning: Option<String> = None;
    if primary_warning.is_some() && !shared {
        fallback_warning = persist_terminal(py, &connection, &plan, &invocation)?;
    }
    if let Some(primary_warning) = primary_warning
        && (shared || fallback_warning.is_some())
    {
        let detail: String = match fallback_warning {
            Some(fallback_warning) => {
                format!("{primary_warning}; fallback failed: {fallback_warning}")
            }
            None => primary_warning,
        };
        return Err(error(py, "DestructionRecordingError", detail));
    }
    call(
        py,
        DESTRUCTION_MODELS,
        "DestructionExecutionResult",
        &[
            ("invocation_id", context.getattr("started")?.get_item(0)?),
            ("outcome", text(py, outcome)),
            (
                "completed_statement_sequences",
                tuple(py, completed_sequences)?,
            ),
            ("pending_statement_sequences", tuple(py, pending_sequences)?),
            (
                "remaining_relation_names",
                match remaining {
                    Some(remaining) => PyTuple::new(py, remaining)?.into_any(),
                    None => none(py),
                },
            ),
            ("error_message", optional_text(py, error_message.as_deref())),
            ("residual_catalog_status", text(py, residual_catalog_status)),
            (
                "residual_catalog_error",
                optional_text(py, residual_catalog_error),
            ),
        ],
    )
}

/// The workflow result with a synthetic success for an interrupted DROP the catalog shows done.
fn recover_interrupted_drop<'py>(
    py: Python<'py>,
    context: &Value<'py>,
    execution: Value<'py>,
    failed_step_id: &str,
) -> PyResult<Value<'py>> {
    let Some(position) = failed_step_id.strip_prefix(DESTROY_STEP_PREFIX) else {
        return Ok(execution);
    };
    let relation_index: Value<'py> = py
        .import("builtins")?
        .getattr("int")?
        .call1((position,))?
        .sub(1)?;
    let mut ordered: Vec<Value<'py>> = Vec::new();
    for relation in reverse_topologically_order_relations(
        py,
        &items(&context.getattr("plan")?.getattr("relations")?)?,
    )? {
        if relation.getattr("exists")?.is_truthy()? {
            ordered.push(relation);
        }
    }
    // Python indexing, so a non-positive position counts from the end.
    let relation: Value<'py> = PyTuple::new(py, ordered)?
        .as_any()
        .get_item(relation_index)?;
    let remaining: Vec<String> = remaining_relations(context)?;
    if remaining.contains(&string(&relation, "name")?) {
        return Ok(execution);
    }
    let statements: Vec<Value<'py>> = context_statements(context)?;
    let mut failed_statement_index: Option<usize> = None;
    for (index, statement) in statements.iter().enumerate() {
        if string(statement, "step_id")? == failed_step_id {
            failed_statement_index = Some(index);
            break;
        }
    }
    let Some(failed_statement_index) = failed_statement_index else {
        return Err(PyStopIteration::new_err(()));
    };
    let results: Vec<Value<'py>> = items(&execution.getattr("statement_results")?)?;
    if results.len() != failed_statement_index {
        return Err(consistency_error(
            py,
            "Interrupted DROP did not align with the confirmed workflow prefix",
        ));
    }
    let synthetic_drop: Value<'py> = call(
        py,
        WORKFLOW_MODELS,
        "WorkflowStatementResult",
        &[
            ("step_id", text(py, failed_step_id)),
            ("query_result", none(py)),
            (
                "mutation_result",
                class(py, ADAPTER_MODELS, "AdapterMutationResult")?.call0()?,
            ),
            ("error_message", none(py)),
        ],
    )?;
    let mut statement_results: Vec<Value<'py>> = results;
    statement_results.push(synthetic_drop);
    call(
        py,
        WORKFLOW_MODELS,
        "WorkflowExecutionResult",
        &[("statement_results", tuple(py, statement_results)?)],
    )
}

fn empty_execution(py: Python<'_>) -> PyResult<Value<'_>> {
    call(
        py,
        WORKFLOW_MODELS,
        "WorkflowExecutionResult",
        &[("statement_results", PyTuple::empty(py).into_any())],
    )
}

/// The outcome of the workflow run: the exact (partial) result, failure detail and any
/// interruption to re-raise once evidence is recorded.
struct WorkflowRun<'py> {
    execution: Value<'py>,
    error_message: Option<String>,
    failure_phase: Option<String>,
    interruption: Option<Value<'py>>,
}

/// The handler for a failed workflow: its exact partial result, reconciled for an interrupted
/// DROP the warehouse completed.
fn workflow_failure<'py>(
    py: Python<'py>,
    context: &Value<'py>,
    failure: &PyErr,
) -> PyResult<WorkflowRun<'py>> {
    let handled: Value<'py> = failure.value(py).clone().into_any();
    let partial_result: Value<'py> = handled.getattr("partial_result")?;
    if !partial_result.is_instance(&class(py, WORKFLOW_MODELS, "WorkflowExecutionResult")?)? {
        let translated: Value<'py> =
            class(py, DESTRUCTION_EXCEPTIONS, "DestructionConsistencyError")?
                .call1(("Destruction workflow did not provide an exact partial result",))?;
        translated.setattr("__context__", &handled)?;
        translated.setattr("__cause__", &handled)?;
        return Err(PyErr::from_value(translated));
    }
    let mut execution: Value<'py> = partial_result;
    verify_completed_prefix(py, &context_statements(context)?, &execution)?;
    let cause: Value<'py> = handled.getattr("cause")?;
    let interrupted: bool = cause.is_instance_of::<PyKeyboardInterrupt>();
    let mut recovery_error: Option<String> = None;
    if interrupted {
        let failed_step_id: String = string(&handled, "failed_step_id")?;
        match recover_interrupted_drop(py, context, execution.clone(), &failed_step_id) {
            Ok(recovered) => execution = recovered,
            Err(recovery_failure) if is_exception(py, &recovery_failure) => {
                recovery_error = Some(recovery_failure.value(py).str()?.extract()?);
            }
            Err(recovery_failure) => return Err(recovery_failure),
        }
    }
    let mut error_message: String = handled.str()?.extract()?;
    let mut failure_phase: &str = "warehouse_execution";
    if let Some(recovery_error) = recovery_error {
        error_message =
            format!("{error_message}; interrupted DROP reconciliation failed: {recovery_error}");
        failure_phase = "drop_reconciliation";
    }
    Ok(WorkflowRun {
        execution,
        error_message: Some(error_message),
        failure_phase: Some(failure_phase.to_owned()),
        interruption: interrupted.then_some(cause),
    })
}

fn execute_recorded_plan<'py>(py: Python<'py>, context: &Value<'py>) -> PyResult<Value<'py>> {
    let run: WorkflowRun<'py> = match call(
        py,
        WAREHOUSE_WORKFLOW,
        "execute_warehouse_workflow",
        &[
            ("statements", context.getattr("statements")?),
            ("connection", context.getattr("connection")?),
            ("emitter", context.getattr("sink")?),
        ],
    ) {
        Ok(execution) => WorkflowRun {
            execution,
            error_message: None,
            failure_phase: None,
            interruption: None,
        },
        Err(failure)
            if failure.is_instance(
                py,
                &class(py, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?,
            ) =>
        {
            workflow_failure(py, context, &failure)
                .map_err(|raised| while_handling(py, raised, &failure))?
        }
        Err(failure) if is_exception(py, &failure) => {
            let handle = || -> PyResult<WorkflowRun<'py>> {
                Ok(WorkflowRun {
                    execution: empty_execution(py)?,
                    error_message: Some(failure.value(py).str()?.extract()?),
                    failure_phase: Some("warehouse_execution".to_owned()),
                    interruption: None,
                })
            };
            handle().map_err(|raised| while_handling(py, raised, &failure))?
        }
        Err(failure) => return Err(failure),
    };
    let WorkflowRun {
        execution,
        mut error_message,
        mut failure_phase,
        interruption,
    } = run;
    verify_completed_prefix(py, &context_statements(context)?, &execution)?;
    let remaining: Option<Vec<String>>;
    let residual_catalog_status: &str;
    let mut residual_catalog_error: Option<String> = None;
    match remaining_relations(context) {
        Ok(names) => {
            remaining = Some(names);
            residual_catalog_status = RESIDUAL_CATALOG_STATUS_OBSERVED;
        }
        Err(failure) if is_exception(py, &failure) => {
            let detail: String = failure
                .value(py)
                .str()
                .and_then(|detail| detail.extract())
                .map_err(|raised| while_handling(py, raised, &failure))?;
            remaining = None;
            residual_catalog_status = "unavailable";
            residual_catalog_error = Some(detail.clone());
            error_message = Some(appended(
                error_message,
                format!("Residual catalog inspection failed: {detail}"),
                format!("residual catalog inspection failed: {detail}"),
            ));
            failure_phase = Some("residual_catalog".to_owned());
        }
        Err(failure) => return Err(failure),
    }
    if error_message.is_none() && remaining.as_ref().is_some_and(|names| !names.is_empty()) {
        error_message =
            Some("Planned relations remain after destructive workflow completion".to_owned());
        failure_phase = Some("residual_catalog".to_owned());
    }
    let result: Value<'py> = record_terminal(
        py,
        context,
        &execution,
        Terminal {
            remaining: remaining.as_deref(),
            residual_catalog_status,
            residual_catalog_error: residual_catalog_error.as_deref(),
            error_message,
            failure_phase,
        },
    )?;
    if let Some(interruption) = interruption {
        return Err(PyErr::from_value(interruption));
    }
    Ok(result)
}

/// What a recorded destruction attempt reached before it failed, for its terminal evidence.
struct Attempt<'py> {
    sink: Option<Value<'py>>,
    lock: Option<Value<'py>>,
    recording_context: Option<Value<'py>>,
    has_statements: bool,
    execution_dispatched: bool,
    failure_phase: &'static str,
}

#[allow(clippy::too_many_arguments)]
fn dispatch<'py>(
    py: Python<'py>,
    attempt: &mut Attempt<'py>,
    frozen_plan: &Value<'py>,
    actor: &Value<'py>,
    challenge_responses: &Value<'py>,
    reviewed_at: &Value<'py>,
    store: &Value<'py>,
    connection: &Value<'py>,
    observation_connection: &Value<'py>,
    project_dir: &Value<'py>,
    replan: &Value<'py>,
    started: &Value<'py>,
    confirmed_at: &Value<'py>,
) -> PyResult<Value<'py>> {
    let invocation_id: Value<'py> = started.get_item(0)?;
    call(
        py,
        INITIALIZE_OBSERVABILITY,
        "initialize_observability",
        &[
            ("connection", observation_connection.clone()),
            ("database", frozen_plan.getattr("metadata_database")?),
        ],
    )?;
    let current_plan: &Value<'py> = frozen_plan;
    let statements: Value<'py> = method(
        &py.import("sqltide._native")?.into_any(),
        "assemble_destruction_workflow",
        &[
            ("plan", current_plan.clone()),
            ("connection", connection.clone()),
        ],
    )?;
    let statement_list: Vec<Value<'py>> = items(&statements)?;
    attempt.has_statements = statements.is_truthy()?;
    let project_identity: Value<'py> = call(
        py,
        PROJECT_IDENTITY,
        "logical_project_identity",
        &[("project_dir", project_dir.clone())],
    )?;
    let sink: Value<'py> = call(
        py,
        RUN_EVENT_SINK,
        "RunEventSink",
        &[
            ("connection", observation_connection.clone()),
            ("database", current_plan.getattr("metadata_database")?),
            ("invocation_id", invocation_id.clone()),
            ("project_identity", project_identity),
            ("strict_persistence", boolean(py, true)?),
        ],
    )?;
    attempt.sink = Some(sink.clone());
    let context: Value<'py> = call(
        py,
        DESTRUCTION_MODELS,
        "DestructionRecordingContext",
        &[
            ("plan", current_plan.clone()),
            ("actor_id", actor.getattr("actor_id")?),
            ("actor_name", actor.getattr("actor_name")?),
            ("reviewed_at", reviewed_at.clone()),
            ("confirmed_at", confirmed_at.clone()),
            ("challenge_responses", challenge_responses.clone()),
            ("connection", connection.clone()),
            ("observation_connection", observation_connection.clone()),
            ("project_dir", project_dir.clone()),
            ("started", started.clone()),
            ("statements", statements.clone()),
            ("sink", sink.clone()),
        ],
    )?;
    attempt.recording_context = Some(context.clone());
    let remaining: Vec<String> = existing_relation_names(current_plan)?;
    let pending: Vec<Value<'py>> = sequences(&statement_list)?;
    let initial_evidence: Bound<'py, PyDict> = operation_evidence(
        py,
        &context,
        &Progress {
            completed_sequences: &[],
            pending_sequences: &pending,
            remaining: Some(&remaining),
            residual_catalog_status: "not_mutated",
            residual_catalog_error: None,
            failure_phase: None,
        },
    )?;
    sink.call_method1("set_operation_evidence", (initial_evidence,))?;
    let model_names: Vec<String> = current_plan.getattr("affected_model_names")?.extract()?;
    let pipeline_names: Vec<String> = current_plan
        .getattr("requested_pipeline_names")?
        .extract()?;
    method(
        &sink,
        "run_started",
        &[
            (
                "command",
                text(py, command(py, &current_plan.getattr("operation")?)?),
            ),
            ("mode", text(py, "destructive")),
            ("total_statements", int(py, statements.len()?)?),
            ("selected_node_count", int(py, model_names.len())?),
            (
                "selectors",
                PyTuple::new(
                    py,
                    pipeline_names.iter().map(|name| format!("pipeline:{name}")),
                )?
                .into_any(),
            ),
            (
                "executed_logical_ids",
                PyTuple::new(py, model_names.iter().map(|name| format!("model:{name}")))?
                    .into_any(),
            ),
        ],
    )?;
    let mut sqls: Vec<String> = Vec::with_capacity(statement_list.len());
    for statement in &statement_list {
        sqls.push(string(statement, "sql")?);
    }
    let workflow_sha256: String = sha256_hex(&sqls.join("\n"));
    attempt.failure_phase = "workflow_prepared";
    method(
        &sink,
        "workflow_prepared",
        &[
            ("statements", statements.clone()),
            ("workflow_sha256", text(py, &workflow_sha256)),
        ],
    )?;
    attempt.failure_phase = "target_lock";
    attempt.lock = Some(method(
        connection,
        "acquire_target_mutation_lock",
        &[
            ("database", frozen_plan.getattr("database")?),
            ("owner_id", invocation_id),
        ],
    )?);
    attempt.failure_phase = "locked_replan";
    verify_destruction_drift(py, frozen_plan, replan)?;
    attempt.failure_phase = "plan_consumption";
    let consumed_plan: Value<'py> = method(
        store,
        "consume",
        &[
            ("plan_id", frozen_plan.getattr("plan_id")?),
            ("challenge_responses", challenge_responses.clone()),
            ("actor", actor.getattr("actor_id")?),
        ],
    )?;
    if consumed_plan
        .getattr("plan_fingerprint")?
        .ne(current_plan.getattr("plan_fingerprint")?)?
    {
        return Err(consistency_error(
            py,
            "Consumed destruction plan differs from the locked replan",
        ));
    }
    attempt.execution_dispatched = true;
    execute_recorded_plan(py, &context)
}

/// Record exact intent, consume once under the target lock, and execute.
#[pyfunction]
#[pyo3(signature = (
    *, frozen_plan, actor, challenge_responses, reviewed_at, store, connection,
    observation_connection, project_dir, replan, invocation_id = None
))]
#[allow(clippy::too_many_arguments)]
fn execute_destruction<'py>(
    py: Python<'py>,
    frozen_plan: &Value<'py>,
    actor: &Value<'py>,
    challenge_responses: &Value<'py>,
    reviewed_at: &Value<'py>,
    store: &Value<'py>,
    connection: &Value<'py>,
    observation_connection: &Value<'py>,
    project_dir: &Value<'py>,
    replan: &Value<'py>,
    invocation_id: Option<&Value<'py>>,
) -> PyResult<Value<'py>> {
    let started: Value<'py> = call(
        py,
        START_INVOCATION,
        "start_invocation",
        &[(
            "invocation_id",
            invocation_id.cloned().unwrap_or_else(|| none(py)),
        )],
    )?;
    let datetime: Bound<'py, PyModule> = py.import("datetime")?;
    let confirmed_at: Value<'py> = datetime.getattr("datetime")?.call_method(
        "now",
        (),
        Some(&keywords(py, &[("tz", datetime.getattr("UTC")?)])?),
    )?;
    let mut attempt: Attempt<'py> = Attempt {
        sink: None,
        lock: None,
        recording_context: None,
        has_statements: false,
        execution_dispatched: false,
        failure_phase: "pre_execution",
    };
    let outcome: PyResult<Value<'py>> = dispatch(
        py,
        &mut attempt,
        frozen_plan,
        actor,
        challenge_responses,
        reviewed_at,
        store,
        connection,
        observation_connection,
        project_dir,
        replan,
        &started,
        &confirmed_at,
    );
    let outcome: PyResult<Value<'py>> = match outcome {
        Err(failure)
            if (is_exception(py, &failure)
                || failure.is_instance_of::<PyKeyboardInterrupt>(py))
                && attempt.has_statements
                && !attempt.execution_dispatched =>
        {
            match &attempt.recording_context {
                Some(context) => {
                    let record = || -> PyResult<()> {
                        let execution: Value<'py> = empty_execution(py)?;
                        let remaining: Vec<String> = existing_relation_names(frozen_plan)?;
                        record_terminal(
                            py,
                            context,
                            &execution,
                            Terminal {
                                remaining: Some(&remaining),
                                residual_catalog_status: "not_mutated",
                                residual_catalog_error: None,
                                error_message: Some(failure.value(py).str()?.extract()?),
                                failure_phase: Some(attempt.failure_phase.to_owned()),
                            },
                        )?;
                        Ok(())
                    };
                    match record() {
                        Ok(()) => Err(failure),
                        Err(raised) => Err(while_handling(py, raised, &failure)),
                    }
                }
                None => Err(failure),
            }
        }
        outcome => outcome,
    };
    // The `finally` block: stop the sink, then release the lock; a failure here replaces the
    // pending outcome and skips the rest.
    let pending: Option<&PyErr> = outcome.as_ref().err();
    for (resource, release) in [(&attempt.sink, None), (&attempt.lock, Some(connection))] {
        let Some(resource) = resource else { continue };
        let released: PyResult<Value<'py>> = match release {
            None => resource.call_method0("stop"),
            Some(connection) => {
                connection.call_method1("release_target_mutation_lock", (resource,))
            }
        };
        if let Err(failure) = released {
            return Err(match pending {
                Some(pending) => while_handling(py, failure, pending),
                None => failure,
            });
        }
    }
    outcome
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(assemble_destruction_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(verify_destruction_drift, module)?)?;
    module.add_function(wrap_pyfunction!(execute_destruction, module)?)?;
    module.add_class::<InMemoryDestructionPlanStore>()
}

/// One frozen plan with its actor and review state.
struct StoredPlan {
    plan: Py<PyAny>,
    actor: Py<PyAny>,
    reviewed_at: Option<Py<PyAny>>,
}

/// Thread-safe, process-local storage with mandatory review and challenge gates. A Python lock
/// serializes every operation, so waiting threads release the interpreter as they always have;
/// the plan map itself is only touched while that lock is held.
#[pyclass(module = "sqltide._native")]
pub(crate) struct InMemoryDestructionPlanStore {
    clock: Option<Py<PyAny>>,
    lock: Py<PyAny>,
    plans: std::sync::Mutex<std::collections::HashMap<String, StoredPlan>>,
}

impl InMemoryDestructionPlanStore {
    fn now<'py>(&self, py: Python<'py>) -> PyResult<Value<'py>> {
        match &self.clock {
            Some(clock) => clock.bind(py).call0(),
            None => {
                let datetime: Bound<'py, PyModule> = py.import("datetime")?;
                datetime.getattr("datetime")?.call_method(
                    "now",
                    (),
                    Some(&keywords(py, &[("tz", datetime.getattr("UTC")?)])?),
                )
            }
        }
    }

    /// Run `operation` while holding the store lock.
    fn locked<'py, T>(
        &self,
        py: Python<'py>,
        operation: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let lock: &Bound<'py, PyAny> = self.lock.bind(py);
        lock.call_method0("acquire")?;
        let result: PyResult<T> = operation();
        let released: PyResult<Value<'py>> = lock.call_method0("release");
        let value: T = result?;
        released?;
        Ok(value)
    }

    fn plans(&self) -> std::sync::MutexGuard<'_, std::collections::HashMap<String, StoredPlan>> {
        self.plans
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The current plan for `actor`, expiring it when its time has passed.
    fn require<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Value<'py>,
        actor: &Value<'py>,
    ) -> PyResult<(Value<'py>, Option<Value<'py>>)> {
        let key: String = plan_id.extract()?;
        let found: Option<(Value<'py>, Value<'py>, Option<Value<'py>>)> =
            self.plans().get(&key).map(|stored| {
                (
                    stored.plan.bind(py).clone(),
                    stored.actor.bind(py).clone(),
                    stored
                        .reviewed_at
                        .as_ref()
                        .map(|reviewed_at| reviewed_at.bind(py).clone()),
                )
            });
        let Some((plan, stored_actor, reviewed_at)) = found else {
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                format!("Destruction plan {} was not found", plan_id.repr()?),
            ));
        };
        if self.now(py)?.ge(plan.getattr("expires_at")?)? {
            self.plans().remove(&key);
            return Err(error(
                py,
                "DestructionPlanExpiredError",
                format!("Destruction plan {} has expired", plan_id.repr()?),
            ));
        }
        if stored_actor.ne(actor)? {
            return Err(error(
                py,
                "DestructionPlanNotFoundError",
                "Destruction plan was not found for this actor".to_owned(),
            ));
        }
        Ok((plan, reviewed_at))
    }

    fn not_reviewed(py: Python<'_>, plan_id: &Value<'_>) -> PyResult<PyErr> {
        Ok(error(
            py,
            "DestructionPlanNotReviewedError",
            format!("Destruction plan {} has not been reviewed", plan_id.repr()?),
        ))
    }
}

#[pymethods]
impl InMemoryDestructionPlanStore {
    #[new]
    #[pyo3(signature = (*, clock = None))]
    fn new(py: Python<'_>, clock: Option<Py<PyAny>>) -> PyResult<Self> {
        Ok(Self {
            clock: clock.filter(|clock| clock.bind(py).is_truthy().unwrap_or(true)),
            lock: py.import("threading")?.getattr("Lock")?.call0()?.unbind(),
            plans: std::sync::Mutex::new(std::collections::HashMap::new()),
        })
    }

    /// Match the durable store lifecycle without external resources.
    fn close(&self) {}

    #[pyo3(signature = (*, plan, actor))]
    fn save<'py>(&self, py: Python<'py>, plan: &Value<'py>, actor: &Value<'py>) -> PyResult<()> {
        self.locked(py, || {
            let plan_id: Value<'py> = plan.getattr("plan_id")?;
            let key: String = plan_id.extract()?;
            if self.plans().contains_key(&key) {
                return Err(error(
                    py,
                    "DestructionValidationError",
                    format!("Destruction plan {} already exists", plan_id.repr()?),
                ));
            }
            if !actor.is_truthy()? {
                return Err(error(
                    py,
                    "DestructionValidationError",
                    "Destruction plan actor must not be empty".to_owned(),
                ));
            }
            self.plans().insert(
                key,
                StoredPlan {
                    plan: plan.clone().unbind(),
                    actor: actor.clone().unbind(),
                    reviewed_at: None,
                },
            );
            Ok(())
        })
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Value<'py>,
        actor: &Value<'py>,
    ) -> PyResult<Value<'py>> {
        self.locked(py, || Ok(self.require(py, plan_id, actor)?.0))
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn mark_reviewed<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Value<'py>,
        actor: &Value<'py>,
    ) -> PyResult<Value<'py>> {
        self.locked(py, || {
            self.require(py, plan_id, actor)?;
            let reviewed_at: Value<'py> = self.now(py)?;
            if let Some(stored) = self.plans().get_mut(&plan_id.extract::<String>()?) {
                stored.reviewed_at = Some(reviewed_at.clone().unbind());
            }
            Ok(reviewed_at)
        })
    }

    #[pyo3(signature = (*, plan_id, actor))]
    fn reviewed_at<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Value<'py>,
        actor: &Value<'py>,
    ) -> PyResult<Value<'py>> {
        self.locked(py, || match self.require(py, plan_id, actor)?.1 {
            Some(reviewed_at) => Ok(reviewed_at),
            None => Err(Self::not_reviewed(py, plan_id)?),
        })
    }

    #[pyo3(signature = (*, plan_id, challenge_responses, actor))]
    fn consume<'py>(
        &self,
        py: Python<'py>,
        plan_id: &Value<'py>,
        challenge_responses: &Value<'py>,
        actor: &Value<'py>,
    ) -> PyResult<Value<'py>> {
        self.locked(py, || {
            let (plan, reviewed_at) = self.require(py, plan_id, actor)?;
            if reviewed_at.is_none() {
                return Err(Self::not_reviewed(py, plan_id)?);
            }
            if challenge_responses.ne(plan.getattr("challenges")?)? {
                return Err(error(
                    py,
                    "DestructionChallengeError",
                    "Challenge responses must exactly match the frozen plan in order".to_owned(),
                ));
            }
            self.plans().remove(&plan_id.extract::<String>()?);
            Ok(plan)
        })
    }
}
