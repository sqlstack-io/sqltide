//! The direct build over the adapter connection: assembling the authoritative workflow from the
//! plan, executing it while realizing replay SQL from in-process captures, recovering paused
//! sources after a failure, and persisting logical SQL baselines. Statements execute through the
//! Python workflow executor; the decisions are the native direct planners'.

use pyo3::PyTypeInfo;
use pyo3::exceptions::{PyKeyError, PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFrozenSet, PySet, PyString, PyTuple};
use sqltide_executor::direct_replay;
use sqltide_executor::direct_workflow;
use sqltide_executor::virtual_workflow::Mode;
use sqltide_executor::workflow::{step_segment, terminate_sql};
use sqltide_planner::direct::sha256_hex;

use crate::handling::while_handling;
use crate::planner_support::{DirectModelFingerprint, build_adapter_resource};
use crate::population_planning::{
    build_direct_population_plan, build_population_replay_templates, expand_population_plan,
    plan_population_objects, row_error_policy_restore_statements, with_metadata_dead_letter_target,
};
use crate::sources::{
    direct_build_error, plain_direct_build_error, plan_preserved_managed_sources,
};
use crate::workflow::statement_results;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const AUDITING_CONSTANTS: &str = "sqltide.executor.auditing.constants";
const DIRECT_MODELS: &str = "sqltide.executor.direct.models";
const DIRECT_EXCEPTIONS: &str = "sqltide.executor.direct.exceptions";
const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const WORKFLOW_EXCEPTIONS: &str = "sqltide.executor.workflow.exceptions";
const WORKFLOW_CONSTANTS: &str = "sqltide.executor.workflow.constants";
const WAREHOUSE_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_warehouse_workflow";
const OBSERVATION_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_observation_workflow";
const RECOVERY_WORKFLOW: &str = "sqltide.executor.workflow.main.execute_recovery_statements";
const PAUSED_SOURCE_STEP_PREFIXES: [&str; 2] = ["pause_source_", "clear_staged_source_"];
const RETRY_COMMAND: &str = "sqt build --changed";

pub(crate) type Value<'py> = Bound<'py, PyAny>;
/// `(teardown as (relation name, is a view), realizations as (resource name, SQL))` of in-place
/// row-error policy view swaps.
pub(crate) type PolicyViewSteps = (Vec<(String, bool)>, Vec<(String, String)>);

pub(crate) fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

pub(crate) fn is_a(value: &Value<'_>, module: &str, name: &str) -> PyResult<bool> {
    value.is_instance(&class(value.py(), module, name)?)
}

pub(crate) fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

pub(crate) fn keywords<'py>(
    py: Python<'py>,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Bound<'py, PyDict>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in pairs {
        arguments.set_item(keyword, value)?;
    }
    Ok(arguments)
}

pub(crate) fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    class(py, module, name)?.call((), Some(&keywords(py, pairs)?))
}

pub(crate) fn call<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    build(py, module, name, pairs)
}

pub(crate) fn method<'py>(
    value: &Value<'py>,
    name: &str,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    value.call_method(name, (), Some(&keywords(value.py(), pairs)?))
}

pub(crate) fn replace<'py>(
    value: &Value<'py>,
    pairs: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let py: Python<'py> = value.py();
    py.import("dataclasses")?
        .getattr("replace")?
        .call((value,), Some(&keywords(py, pairs)?))
}

pub(crate) fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

pub(crate) fn text<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

pub(crate) fn boolean(py: Python<'_>, value: bool) -> PyResult<Value<'_>> {
    Ok(value.into_pyobject(py)?.to_owned().into_any())
}

pub(crate) fn string(value: &Value<'_>, name: &str) -> PyResult<String> {
    value.getattr(name)?.extract()
}

/// `value or default`.
pub(crate) fn or<'py>(value: Value<'py>, default: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(if value.is_truthy()? {
        value
    } else {
        default.clone()
    })
}

pub(crate) fn none(py: Python<'_>) -> Value<'_> {
    py.None().into_bound(py)
}

/// `error` raised with `__cause__` set to `cause` while handling `context`.
fn raised_from<'py>(error: &Value<'py>, context: &Value<'py>, cause: &Value<'py>) -> PyResult<()> {
    error.setattr("__context__", context)?;
    error.setattr("__cause__", cause)
}

fn new_error(py: Python<'_>, module: &str, name: &str, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        class(py, module, name)?.call1((message,))?,
    ))
}

/// The `(name, type)` columns of a desired or catalog relation.
fn columns(relation: &Value<'_>) -> PyResult<Vec<(String, String)>> {
    items(&relation.getattr("columns")?)?
        .iter()
        .map(|column| Ok((string(column, "name")?, string(column, "type")?)))
        .collect()
}

/// Every desired object of a realized project's desired state.
fn desired_objects<'py>(request: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    items(
        &request
            .getattr("realized_project")?
            .getattr("desired_state")?
            .getattr("objects")?,
    )
}

fn reject_incapable_adapter(py: Python<'_>, client: &Value<'_>) -> PyResult<()> {
    if client
        .getattr("capabilities")?
        .getattr("direct_rebuild")?
        .is_truthy()?
    {
        return Ok(());
    }
    Err(new_error(
        py,
        ADAPTER_EXCEPTIONS,
        "AdapterCapabilityError",
        format!(
            "Adapter '{}' does not support direct rebuilds",
            client.getattr("adapter_identity")?.getattr("name")?.str()?
        ),
    )?)
}

/// The workflow statements of `(step ID, phase, intent, SQL, continue on error)` tuples,
/// numbered from one.
pub(crate) fn warehouse_statements<'py>(
    py: Python<'py>,
    statements: &[Value<'py>],
) -> PyResult<Value<'py>> {
    let phase: Value<'py> = class(py, WORKFLOW_TYPES, "WorkflowPhase")?;
    let intent: Value<'py> = class(py, WORKFLOW_TYPES, "StatementIntent")?;
    let mut built: Vec<Value<'py>> = Vec::with_capacity(statements.len());
    for (position, statement) in statements.iter().enumerate() {
        let (step_id, phase_value, intent_value, sql, continue_on_error): (
            Value<'py>,
            Value<'py>,
            Value<'py>,
            Value<'py>,
            Value<'py>,
        ) = statement.extract()?;
        built.push(build(
            py,
            WORKFLOW_MODELS,
            "WarehouseStatement",
            &[
                ("sequence", (position + 1).into_pyobject(py)?.into_any()),
                ("step_id", step_id),
                ("phase", phase.call1((phase_value,))?),
                ("intent", intent.call1((intent_value,))?),
                ("sql", sql),
                ("continue_on_error", continue_on_error),
            ],
        )?);
    }
    tuple(py, built)
}

pub(crate) fn native_statements<'py>(
    py: Python<'py>,
    statements: Vec<sqltide_executor::workflow::Statement>,
) -> PyResult<Vec<Value<'py>>> {
    statement_results(statements)
        .into_iter()
        .map(|statement| Ok(statement.into_pyobject(py)?.into_any()))
        .collect()
}

/// `client.render_resource(...)` of a landing view, refusing views that do not realize as
/// materialized views.
fn render_landing_view<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    landing_view: &Value<'py>,
) -> PyResult<(String, String)> {
    let built: Value<'py> = build_adapter_resource(py, landing_view)?;
    let name: Value<'py> = landing_view.getattr("name")?;
    if !is_a(&built, ADAPTER_MODELS, "AdapterMaterializedView")? {
        return Err(plain_direct_build_error(
            py,
            format!(
                "Landing view '{}' did not realize as a materialized view",
                name.str()?
            ),
        ));
    }
    let sql: String = method(
        client,
        "render_resource",
        &[
            ("resource", built),
            (
                "database",
                or(
                    landing_view.getattr("key")?.getattr("database")?,
                    &request.getattr("database")?,
                )?,
            ),
            ("if_not_exists", boolean(py, true)?),
        ],
    )?
    .extract()?;
    Ok((name.extract()?, sql))
}

fn render_landing_views<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    landing_views: &[Value<'py>],
) -> PyResult<Vec<(String, String)>> {
    landing_views
        .iter()
        .map(|landing_view| render_landing_view(py, request, client, landing_view))
        .collect()
}

/// The callable rendering one desired view by name for an in-place row-error policy change,
/// or `None` when the project no longer declares it.
fn policy_view_renderer<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<Value<'py>> {
    let desired_views: Bound<'py, PyDict> = PyDict::new(py);
    for desired in desired_objects(request)? {
        if is_a(&desired, COMPILE_MODELS, "DesiredMaterializedView")? {
            desired_views.set_item(desired.getattr("name")?, desired)?;
        }
    }
    let desired_views: Py<PyDict> = desired_views.unbind();
    let request: Py<PyAny> = request.clone().unbind();
    let client: Py<PyAny> = client.clone().unbind();
    let render = move |arguments: &Bound<'_, PyTuple>,
                       _keywords: Option<&Bound<'_, PyDict>>|
          -> PyResult<Py<PyAny>> {
        let py: Python<'_> = arguments.py();
        let name: Value<'_> = arguments.get_item(0)?;
        let Some(view) = desired_views.bind(py).get_item(&name)? else {
            return Ok(py.None());
        };
        let request: &Value<'_> = request.bind(py);
        let database: Value<'_> = request.getattr("database")?;
        let resource: Value<'_> = with_metadata_dead_letter_target(
            &build_adapter_resource(py, &view)?,
            &database,
            &request.getattr("metadata_database")?,
        )?;
        Ok(method(
            client.bind(py),
            "render_resource",
            &[
                ("resource", resource),
                (
                    "database",
                    or(view.getattr("key")?.getattr("database")?, &database)?,
                ),
            ],
        )?
        .unbind())
    };
    Ok(PyCFunction::new_closure(py, None, None, render)?.into_any())
}

struct PlannedStatements<'py> {
    statements: Value<'py>,
    recovery: Value<'py>,
}

struct Preparation<'py> {
    source_preparation: Value<'py>,
    source_realizations: Vec<Value<'py>>,
    preserved_landing_views: Vec<Value<'py>>,
    realizations: Vec<Value<'py>>,
    runtime_replays: Vec<Value<'py>>,
}

/// The workflow statements and the statements that recover paused sources.
fn assemble_statements<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    preparation: &Preparation<'py>,
) -> PyResult<PlannedStatements<'py>> {
    let plan: Value<'py> = request.getattr("plan")?;
    let changes: Value<'py> = request.getattr("row_error_policy_changes")?;
    if !plan.getattr("execution_scope")?.is_truthy()?
        && !request.getattr("audits")?.is_truthy()?
        && !changes.is_truthy()?
    {
        return Ok(PlannedStatements {
            statements: PyTuple::empty(py).into_any(),
            recovery: PyTuple::empty(py).into_any(),
        });
    }
    let database: Value<'py> = request.getattr("database")?;
    let metadata_database: Value<'py> = request.getattr("metadata_database")?;
    let mut realizations: Vec<(String, String)> = Vec::new();
    for realization in &preparation.realizations {
        let resource: Value<'py> = realization.getattr("resource")?;
        let realization_database: Value<'py> = realization.getattr("database")?;
        let sql: String = method(
            client,
            "render_resource",
            &[
                (
                    "resource",
                    with_metadata_dead_letter_target(
                        &resource,
                        &realization_database,
                        &metadata_database,
                    )?,
                ),
                ("database", realization_database),
            ],
        )?
        .extract()?;
        realizations.push((string(&resource, "name")?, sql));
    }
    let database_text: String = database.extract()?;
    // Looked up on the module at call time, as the Python workflow's import binding was, so a
    // test can substitute it to inject a failing policy view.
    let (policy_teardown, policy_realizations): PolicyViewSteps = call(
        py,
        "sqltide._native",
        "row_error_policy_view_steps",
        &[
            ("changes", changes.clone()),
            ("database", database.clone()),
            ("render_view", policy_view_renderer(py, request, client)?),
        ],
    )?
    .extract()?;
    realizations.extend(policy_realizations);
    let ensure_database_sql: String = client
        .call_method1("render_ensure_database", (&database,))?
        .extract()?;
    let mut source_realizations: Vec<(String, String)> = Vec::new();
    for realization in &preparation.source_realizations {
        let resource: Value<'py> = realization.getattr("resource")?;
        let sql: String = method(
            client,
            "render_resource",
            &[
                ("resource", resource.clone()),
                ("database", realization.getattr("database")?),
                ("if_not_exists", boolean(py, true)?),
            ],
        )?
        .extract()?;
        source_realizations.push((string(&resource, "name")?, sql));
    }
    let source_preparation: &Value<'py> = &preparation.source_preparation;
    let mut recreations: Vec<direct_workflow::Recreation> = Vec::new();
    for recreation in items(&source_preparation.getattr("recreated_sources")?)? {
        let source: Value<'py> = recreation.getattr("managed_source")?;
        let resource: Value<'py> = source.getattr("resource")?;
        let staged_name: Value<'py> = recreation.getattr("staged_relation_name")?;
        let source_database: Value<'py> = source.getattr("database")?;
        let stage_sql: String = method(
            client,
            "render_resource",
            &[
                (
                    "resource",
                    replace(&resource, &[("name", staged_name.clone())])?,
                ),
                ("database", source_database.clone()),
            ],
        )?
        .extract()?;
        recreations.push(direct_workflow::Recreation {
            source_name: string(&resource, "name")?,
            database: source_database.extract()?,
            staged_name: staged_name.extract()?,
            landing_view_name: string(&recreation, "landing_view_name")?,
            stage_sql,
        });
    }
    let landing_views: Vec<Value<'py>> = items(&source_preparation.getattr("landing_views")?)?;
    let bootstrapped_landing_views: Vec<(String, String)> =
        render_landing_views(py, request, client, &landing_views)?;
    let mut runtime_replays: Vec<direct_workflow::RuntimeReplay> = Vec::new();
    for runtime_replay in &preparation.runtime_replays {
        let coverage_request: Value<'py> = build(
            py,
            ADAPTER_MODELS,
            "AdapterReplayCoverageRequest",
            &[
                ("replay", runtime_replay.getattr("replay")?),
                (
                    "boundary_column_type",
                    runtime_replay.getattr("boundary_column_type")?,
                ),
                (
                    "count_anchor_rows",
                    runtime_replay.getattr("managed_landing_anchor")?,
                ),
            ],
        )?;
        runtime_replays.push(direct_workflow::RuntimeReplay {
            model_name: string(runtime_replay, "model_name")?,
            capture_step_id: string(runtime_replay, "capture_step_id")?,
            replay_step_id: string(runtime_replay, "replay_step_id")?,
            coverage_sql: client
                .call_method1("render_replay_coverage_query", (coverage_request,))?
                .extract()?,
        });
    }
    let mut activated: Vec<Value<'py>> = preparation.preserved_landing_views.clone();
    activated.extend(landing_views);
    let activations: Vec<(String, String)> = render_landing_views(py, request, client, &activated)?;
    let view_kind: Value<'py> = class(py, PLANNER_TYPES, "DirectResourceKind")?.getattr("VIEW")?;
    let mut teardown: Vec<(String, bool)> = Vec::new();
    for operation in items(&plan.getattr("teardown_operations")?)? {
        teardown.push((
            string(&operation, "relation_name")?,
            operation.getattr("resource_kind")?.eq(&view_kind)?,
        ));
    }
    teardown.extend(policy_teardown);
    let mut audits: Vec<(String, String)> = Vec::new();
    for audit in items(&request.getattr("audits")?)? {
        audits.push((string(&audit, "name")?, string(&audit, "query")?));
    }
    let (statements, recovery) = direct_workflow::statements(&direct_workflow::Input {
        database: database_text.clone(),
        ensure_database_sql,
        source_realizations,
        recreations,
        preserved_landing_view_names: preparation
            .preserved_landing_views
            .iter()
            .map(|view| string(view, "name"))
            .collect::<PyResult<_>>()?,
        bootstrapped_landing_views,
        activations,
        stabilization_seconds: request.getattr("stabilization_seconds")?.extract()?,
        teardown,
        realizations,
        runtime_replays,
        audits,
        audit_sample_limit: class(py, AUDITING_CONSTANTS, "AUDIT_SAMPLE_LIMIT")?.extract()?,
    });
    let mut recovery_statements: Vec<Value<'py>> = items(&row_error_policy_restore_statements(
        py,
        &changes,
        &client.getattr("render_restore_materialized_view")?,
        &database_text,
    )?)?;
    recovery_statements.extend(native_statements(py, recovery)?);
    Ok(PlannedStatements {
        statements: warehouse_statements(py, &native_statements(py, statements)?)?,
        recovery: warehouse_statements(py, &recovery_statements)?,
    })
}

/// One [`DirectRuntimeReplay`] per replay template, resolved against the plan and catalog.
fn runtime_replays<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    snapshot: &Value<'py>,
    replay_templates: &[Value<'py>],
) -> PyResult<Vec<Value<'py>>> {
    let mut entries: Vec<(String, Vec<String>)> = Vec::new();
    for entry in items(&request.getattr("plan")?.getattr("entries")?)? {
        entries.push((
            string(&entry.getattr("model_key")?, "name")?,
            entry.getattr("relation_names")?.extract()?,
        ));
    }
    let mut replays: Vec<direct_replay::Replay> = Vec::new();
    for replay in replay_templates {
        let mode: String = replay.getattr("mode")?.str()?.extract()?;
        let relations: Value<'py> = replay.getattr("relations")?;
        let columns: Value<'py> = replay.getattr("columns")?;
        let Some(mode) = Mode::parse(&mode) else {
            // The native binding refused an unknown mode with `ValueError(message)`, which the
            // workflow translated like an exhausted resolution.
            return Err(stop_iteration_from_none(
                py,
                &[&format!("Unknown replay mode '{mode}'")],
            ));
        };
        replays.push(direct_replay::Replay {
            mode,
            anchor: string(&relations, "anchor")?,
            target: string(&relations, "target")?,
            timestamp_column: string(&columns, "timestamp")?,
            landed_at_column: string(&columns, "landed_at")?,
            cursor_column: string(&columns, "cursor")?,
        });
    }
    let mut desired_tables: Vec<direct_replay::Relation> = Vec::new();
    for desired in desired_objects(request)? {
        if is_a(&desired, COMPILE_MODELS, "DesiredTable")? {
            desired_tables.push(direct_replay::Relation {
                name: string(&desired, "name")?,
                columns: columns(&desired)?,
            });
        }
    }
    let mut catalog: Vec<direct_replay::Relation> = Vec::new();
    for relation in items(&snapshot.getattr("catalog")?.getattr("relations")?)? {
        catalog.push(direct_replay::Relation {
            name: string(&relation, "name")?,
            columns: columns(&relation)?,
        });
    }
    let resolved: Vec<direct_replay::RuntimeReplay> =
        direct_replay::resolve(&direct_replay::ResolveInput {
            entries,
            replays,
            forced_start_time: !request.getattr("effective_start_time")?.is_none(),
            desired_tables,
            catalog,
        })
        .map_err(|error| match error {
            direct_replay::ResolveError::Build(message) => {
                direct_build_error(py, message.clone(), &["build", &message])
            }
            direct_replay::ResolveError::Exhausted => stop_iteration_from_none(py, &["exhausted"]),
        })?;
    let desired_state: Value<'py> = request
        .getattr("realized_project")?
        .getattr("desired_state")?;
    let mut runtime_replays: Vec<Value<'py>> = Vec::with_capacity(resolved.len());
    for (replay, resolved) in replay_templates.iter().zip(resolved) {
        let anchor: Value<'py> = replay.getattr("relations")?.getattr("anchor")?;
        runtime_replays.push(build(
            py,
            DIRECT_MODELS,
            "DirectRuntimeReplay",
            &[
                ("model_name", text(py, &resolved.model_name)),
                ("capture_step_id", text(py, &resolved.capture_step_id)),
                ("replay_step_id", text(py, &resolved.replay_step_id)),
                ("replay", replay.clone()),
                (
                    "boundary_column_type",
                    resolved.boundary_column_type.into_pyobject(py)?.into_any(),
                ),
                (
                    "managed_landing_anchor",
                    desired_state.call_method1("is_managed_landing_table", (anchor,))?,
                ),
            ],
        )?);
    }
    Ok(runtime_replays)
}

/// Resolve preconditions and assemble every direct lifecycle statement once.
#[pyfunction]
#[pyo3(signature = (*, request, client, snapshot, plan_json))]
fn assemble_direct_build_workflow<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    snapshot: &Value<'py>,
    plan_json: &Value<'py>,
) -> PyResult<Value<'py>> {
    reject_incapable_adapter(py, client)?;
    let realized_project: Value<'py> = request.getattr("realized_project")?;
    let desired_state: Value<'py> = realized_project.getattr("desired_state")?;
    let database: Value<'py> = request.getattr("database")?;
    let (source_preparation, source_realizations): (Value<'py>, Value<'py>) =
        plan_preserved_managed_sources(
            py,
            &request.getattr("plan")?,
            &realized_project,
            &snapshot.getattr("catalog")?,
            &database,
        )?
        .extract()?;
    let policy_landing_view_names: Bound<'py, PySet> = PySet::empty(py)?;
    for change in items(&request.getattr("row_error_policy_changes")?)? {
        for name in items(&change.getattr("landing_view_names")?)? {
            policy_landing_view_names.add(name)?;
        }
    }
    let preserved_names: Value<'py> = source_preparation.getattr("preserved_relation_names")?;
    let mut preserved_landing_views: Vec<Value<'py>> = Vec::new();
    for desired in desired_objects(request)? {
        if !is_a(&desired, COMPILE_MODELS, "DesiredMaterializedView")? {
            continue;
        }
        let name: Value<'py> = desired.getattr("name")?;
        if preserved_names.contains(&name)? || policy_landing_view_names.contains(&name)? {
            preserved_landing_views.push(desired);
        }
    }
    let population_plan: Value<'py> = expand_population_plan(
        py,
        &build_direct_population_plan(py, &request.getattr("plan")?, &realized_project)?,
        &desired_state,
    )?;
    let realizations: Vec<Value<'py>> = items(&plan_population_objects(
        py,
        &population_plan,
        &desired_state,
        &database,
    )?)?;
    let mut replay_templates: Vec<Value<'py>> = Vec::new();
    for pair in items(&build_population_replay_templates(
        py,
        &population_plan,
        &desired_state,
        &database.extract::<String>()?,
    )?)? {
        replay_templates.push(pair.get_item(1)?);
    }
    let preparation: Preparation<'py> = Preparation {
        runtime_replays: runtime_replays(py, request, snapshot, &replay_templates)?,
        source_preparation,
        source_realizations: items(&source_realizations)?,
        preserved_landing_views,
        realizations,
    };
    let PlannedStatements {
        statements,
        recovery,
    } = assemble_statements(py, request, client, &preparation)?;
    let mut step_ids: Vec<String> = Vec::new();
    for statement in items(&statements)? {
        step_ids.push(string(&statement, "step_id")?);
    }
    let mut recreated_sources: Vec<Value<'py>> = Vec::new();
    for recreation in items(
        &preparation
            .source_preparation
            .getattr("recreated_sources")?,
    )? {
        let name: String = string(
            &recreation.getattr("managed_source")?.getattr("resource")?,
            "name",
        )?;
        let swap_step: String = format!("swap_source_{}", step_segment(&name));
        if step_ids.contains(&swap_step) {
            recreated_sources.push(recreation);
        }
    }
    let template: Value<'py> = build(
        py,
        WORKFLOW_MODELS,
        "BuildWorkflow",
        &[
            (
                "mode",
                class(py, WORKFLOW_TYPES, "WorkflowMode")?.getattr("DIRECT")?,
            ),
            ("plan_json", plan_json.clone()),
            ("statements", statements),
        ],
    )?;
    build(
        py,
        DIRECT_MODELS,
        "DirectBuildWorkflow",
        &[
            ("template", template),
            ("runtime_replays", tuple(py, preparation.runtime_replays)?),
            ("workflow_id", request.getattr("workflow_id")?),
            ("source_recovery_statements", recovery),
            ("recreated_sources", tuple(py, recreated_sources)?),
        ],
    )
}

/// `raise StopIteration from None` while handling the native refusal `ValueError(*refusal)`.
fn stop_iteration_from_none<'py>(py: Python<'py>, refusal: &[&str]) -> PyErr {
    let raised = || -> PyResult<PyErr> {
        let error: Value<'py> = PyStopIteration::type_object(py).call0()?;
        let context: Value<'py> =
            PyValueError::type_object(py).call1(PyTuple::new(py, refusal)?)?;
        error.setattr("__context__", context)?;
        error.setattr("__cause__", none(py))?;
        Ok(PyErr::from_value(error))
    };
    raised().unwrap_or_else(|error| error)
}

/// Python's `int(value)`.
fn python_int<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Value<'py>> {
    py.import("builtins")?.getattr("int")?.call1((value,))
}

/// Whether the anchor holds more rows than the captured ranges have distinct offsets.
fn capture_has_duplicate_offsets<'py>(py: Python<'py>, capture: &Value<'py>) -> PyResult<bool> {
    let offsets: Value<'py> =
        class(py, DISCOVERY_TYPES, "ReplayLineageMode")?.getattr("OFFSETS")?;
    let ranges: Vec<Value<'py>> = items(&capture.getattr("ranges")?)?;
    if !capture.getattr("boundary_mode")?.is(&offsets) || ranges.is_empty() {
        return Ok(false);
    }
    let anchor_row_count: Value<'py> = ranges[0].getattr("anchor_row_count")?;
    if anchor_row_count.is_none() {
        return Ok(false);
    }
    let mut distinct_offsets: Value<'py> = 0_i64.into_pyobject(py)?.into_any();
    for replay_range in &ranges {
        let span: Value<'py> = python_int(py, &replay_range.getattr("upper_value")?)?
            .sub(python_int(py, &replay_range.getattr("lower_value")?)?)?
            .add(1)?;
        distinct_offsets = distinct_offsets.add(span)?;
    }
    anchor_row_count.gt(distinct_offsets)
}

/// The capture's replay inputs realized natively, translating capture failures like the
/// workflow always has: a missing partition refuses the build, and a non-integer offset raises
/// the error `int()` gives for it.
fn realize_capture<'py>(
    py: Python<'py>,
    capture: &Value<'py>,
) -> PyResult<direct_replay::Realized> {
    let mut ranges: Vec<direct_replay::CapturedRange> = Vec::new();
    for replay_range in items(&capture.getattr("ranges")?)? {
        ranges.push(direct_replay::CapturedRange {
            partition: replay_range.getattr("partition_value")?.extract()?,
            lower: string(&replay_range, "lower_value")?,
            cutoff: string(&replay_range, "replay_cutoff_value")?,
            cutoff_inclusive: replay_range.getattr("cutoff_inclusive")?.extract()?,
        });
    }
    let mode: String = capture.getattr("boundary_mode")?.str()?.extract()?;
    match direct_replay::realize(&mode, &ranges) {
        Ok(realized) => Ok(realized),
        Err(direct_replay::CaptureError::MissingPartition) => Err(direct_build_error(
            py,
            "Captured offset replay range has no partition".to_owned(),
            &["partition"],
        )),
        Err(direct_replay::CaptureError::NotInteger(value)) => {
            let native: PyErr = PyValueError::new_err(("integer", value.clone()));
            match python_int(py, &text(py, &value)) {
                Ok(_) => Err(native),
                Err(error) => {
                    error.value(py).setattr("__context__", native.value(py))?;
                    Err(error)
                }
            }
        }
    }
}

/// Replace one direct replay template with SQL from its in-memory capture.
fn realize_replay_statement<'py>(
    py: Python<'py>,
    template_statement: &Value<'py>,
    runtime_replay: &Value<'py>,
    capture: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<Value<'py>> {
    let realized: direct_replay::Realized = realize_capture(py, capture)?;
    let mut boundaries: Vec<Value<'py>> = Vec::new();
    for boundary in realized.boundaries {
        boundaries.push(build(
            py,
            ADAPTER_MODELS,
            "AdapterReplayBoundary",
            &[
                ("boundary_key", text(py, &boundary.key)),
                ("cutoff_value", text(py, &boundary.cutoff)),
                ("cutoff_inclusive", boolean(py, boundary.cutoff_inclusive)?),
                (
                    "partition_value",
                    boundary.partition.into_pyobject(py)?.into_any(),
                ),
            ],
        )?);
    }
    let dedupe_offsets: bool = runtime_replay
        .getattr("managed_landing_anchor")?
        .is_truthy()?
        && capture_has_duplicate_offsets(py, capture)?;
    let replay: Value<'py> = replace(
        &runtime_replay.getattr("replay")?,
        &[
            ("boundaries", tuple(py, boundaries)?),
            ("dedupe_offsets", boolean(py, dedupe_offsets)?),
        ],
    )?;
    let mut lower_bounds: Vec<Value<'py>> = Vec::new();
    for bound in realized.lower_bounds {
        lower_bounds.push(build(
            py,
            ADAPTER_MODELS,
            "AdapterReplayLowerBound",
            &[
                ("value", text(py, &bound.value)),
                (
                    "partition_value",
                    bound.partition.into_pyobject(py)?.into_any(),
                ),
            ],
        )?);
    }
    let captured_request: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterCapturedReplayRequest",
        &[
            ("replay", replay.clone()),
            (
                "boundary_column_type",
                runtime_replay.getattr("boundary_column_type")?,
            ),
            ("lower_bounds", tuple(py, lower_bounds)?),
        ],
    )?;
    let rendered: String = client
        .call_method1("render_replay_from_capture", (captured_request,))?
        .extract()?;
    let progress: Value<'py> = if realized.offset_ranges.is_empty() {
        none(py)
    } else {
        let mut ranges: Vec<Value<'py>> = Vec::new();
        for span in realized.offset_ranges {
            ranges.push(build(
                py,
                ADAPTER_MODELS,
                "AdapterReplayOffsetRange",
                &[
                    ("partition", span.partition.into_pyobject(py)?.into_any()),
                    ("lower_offset", span.lower.into_pyobject(py)?.into_any()),
                    ("upper_offset", span.upper.into_pyobject(py)?.into_any()),
                ],
            )?);
        }
        let relations: Value<'py> = replay.getattr("relations")?;
        let columns: Value<'py> = replay.getattr("columns")?;
        build(
            py,
            ADAPTER_MODELS,
            "AdapterReplayOffsetProgressRequest",
            &[
                ("database", replay.getattr("database")?),
                ("relation", relations.getattr("target")?),
                ("partition_column", columns.getattr("partition")?),
                ("offset_column", columns.getattr("offset")?),
                ("ranges", tuple(py, ranges)?),
            ],
        )?
    };
    replace(
        template_statement,
        &[
            ("sql", text(py, &terminate_sql(&rendered))),
            ("replay_offset_progress", progress),
        ],
    )
}

fn emit_workflow_prepared<'py>(
    py: Python<'py>,
    emitter: &Value<'py>,
    statements: &[Value<'py>],
) -> PyResult<()> {
    if emitter.is_none() {
        return Ok(());
    }
    let mut sqls: Vec<String> = Vec::with_capacity(statements.len());
    for statement in statements {
        sqls.push(string(statement, "sql")?);
    }
    method(
        emitter,
        "workflow_prepared",
        &[
            ("statements", tuple(py, statements.to_vec())?),
            ("workflow_sha256", text(py, &sha256_hex(&sqls.join("\n")))),
        ],
    )?;
    Ok(())
}

/// A capture row's column as text, `None` when absent, null or empty.
fn optional_text(row: &Value<'_>, column: &str) -> PyResult<Option<String>> {
    let value: Value<'_> = row.call_method1("get", (column,))?;
    if value.is_none() {
        return Ok(None);
    }
    let rendered: String = value.str()?.extract()?;
    Ok((!rendered.is_empty()).then_some(rendered))
}

fn required_text(py: Python<'_>, row: &Value<'_>, column: &str) -> PyResult<String> {
    optional_text(row, column)?.ok_or_else(|| {
        result_error(
            py,
            format!("Replay capture row is missing required column '{column}'"),
        )
    })
}

fn result_error(py: Python<'_>, message: String) -> PyErr {
    match new_error(py, ADAPTER_EXCEPTIONS, "AdapterResultError", message) {
        Ok(error) | Err(error) => error,
    }
}

fn validate_capture_row(
    py: Python<'_>,
    runtime_replay: &Value<'_>,
    row: &Value<'_>,
) -> PyResult<()> {
    let model_name: String = string(runtime_replay, "model_name")?;
    let replay: Value<'_> = runtime_replay.getattr("replay")?;
    let expected_relation: String = string(&replay.getattr("relations")?, "anchor")?;
    let actual_relation: String = required_text(py, row, "driving_input_relation_name")?;
    if actual_relation != expected_relation {
        return Err(result_error(
            py,
            format!(
                "Replay capture for '{model_name}' returned driving input '{actual_relation}' \
                 instead of '{expected_relation}'"
            ),
        ));
    }
    let mode: Value<'_> = replay.getattr("mode")?;
    let expected_mode: String = mode.str()?.extract()?;
    let actual_mode: String = required_text(py, row, "replay_boundary_mode")?;
    if actual_mode != expected_mode {
        return Err(result_error(
            py,
            format!(
                "Replay capture for '{model_name}' returned boundary mode '{actual_mode}' \
                 instead of '{expected_mode}'"
            ),
        ));
    }
    let partition_value: Option<String> = optional_text(row, "partition_value")?;
    let offsets: Value<'_> =
        class(py, ADAPTER_TYPES, "AdapterReplayBoundaryMode")?.getattr("OFFSETS")?;
    let expected_key: String = if mode.eq(offsets)? {
        let Some(partition_value) = partition_value else {
            return Err(result_error(
                py,
                format!(
                    "Replay capture for '{model_name}' returned an offset boundary without a \
                     partition"
                ),
            ));
        };
        format!("_replay_partition={partition_value}")
    } else {
        if let Some(partition_value) = partition_value {
            return Err(result_error(
                py,
                format!(
                    "Replay capture for '{model_name}' returned scalar boundary mode \
                     '{expected_mode}' with partition '{partition_value}'"
                ),
            ));
        }
        format!("_replay_{expected_mode}")
    };
    let actual_key: String = required_text(py, row, "boundary_key")?;
    if actual_key != expected_key {
        return Err(result_error(
            py,
            format!(
                "Replay capture for '{model_name}' returned boundary key '{actual_key}' instead \
                 of '{expected_key}'"
            ),
        ));
    }
    Ok(())
}

fn optional<'py>(py: Python<'py>, value: Option<String>) -> PyResult<Value<'py>> {
    Ok(value.into_pyobject(py)?.into_any())
}

fn decode_range<'py>(py: Python<'py>, row: &Value<'py>) -> PyResult<Value<'py>> {
    let partition_value: Value<'py> = optional(py, optional_text(row, "partition_value")?)?;
    let source_partition_column_name: Value<'py> =
        optional(py, optional_text(row, "source_partition_column_name")?)?;
    let source_position_column_name: Value<'py> =
        text(py, &required_text(py, row, "source_position_column_name")?);
    let source_timestamp_column_name: Value<'py> =
        optional(py, optional_text(row, "source_timestamp_column_name")?)?;
    let lower_value: Value<'py> = text(py, &required_text(py, row, "lower_value")?);
    let upper_value: Value<'py> = text(py, &required_text(py, row, "upper_value")?);
    let replay_cutoff_value: Value<'py> = text(py, &required_text(py, row, "replay_cutoff_value")?);
    let cutoff_inclusive: bool = row
        .call_method1("get", ("cutoff_inclusive", boolean(py, true)?))?
        .is_truthy()?;
    let anchor_row_count: Value<'py> = match optional_text_raw(row, "anchor_row_count")? {
        None => none(py),
        Some(value) => python_int(py, &value.str()?.into_any())?,
    };
    build(
        py,
        DIRECT_MODELS,
        "DirectReplayRange",
        &[
            ("partition_value", partition_value),
            ("source_partition_column_name", source_partition_column_name),
            ("source_position_column_name", source_position_column_name),
            ("source_timestamp_column_name", source_timestamp_column_name),
            ("lower_value", lower_value),
            ("upper_value", upper_value),
            ("replay_cutoff_value", replay_cutoff_value),
            ("cutoff_inclusive", boolean(py, cutoff_inclusive)?),
            ("anchor_row_count", anchor_row_count),
        ],
    )
}

/// A row's raw column value, `None` when absent or null.
fn optional_text_raw<'py>(row: &Value<'py>, column: &str) -> PyResult<Option<Value<'py>>> {
    let value: Value<'py> = row.call_method1("get", (column,))?;
    Ok((!value.is_none()).then_some(value))
}

fn decode_capture<'py>(
    py: Python<'py>,
    workflow_id: &Value<'py>,
    runtime_replay: &Value<'py>,
    result: &Value<'py>,
) -> PyResult<Value<'py>> {
    if result.is_none() {
        return Err(result_error(
            py,
            format!(
                "Replay capture '{}' returned no query result",
                runtime_replay.getattr("capture_step_id")?.str()?
            ),
        ));
    }
    let rows: Vec<Value<'py>> = items(&result.call_method0("named_rows")?)?;
    for row in &rows {
        validate_capture_row(py, runtime_replay, row)?;
    }
    let mut ranges: Vec<Value<'py>> = Vec::with_capacity(rows.len());
    for row in &rows {
        ranges.push(decode_range(py, row)?);
    }
    let captured_at: Value<'py> = match rows.first() {
        Some(row) => optional(py, optional_text(row, "captured_at")?)?,
        None => none(py),
    };
    let ranges: Value<'py> = tuple(py, ranges)?;
    let model_name: Value<'py> = runtime_replay.getattr("model_name")?;
    let identity: String = tuple(
        py,
        vec![
            workflow_id.clone(),
            model_name.clone(),
            captured_at.clone(),
            ranges.clone(),
        ],
    )?
    .repr()?
    .extract()?;
    let replay: Value<'py> = runtime_replay.getattr("replay")?;
    build(
        py,
        DIRECT_MODELS,
        "DirectReplayCapture",
        &[
            ("capture_id", text(py, &sha256_hex(&identity))),
            ("workflow_id", workflow_id.clone()),
            ("target_database", replay.getattr("database")?),
            ("logical_model_name", model_name),
            (
                "driving_input_relation_name",
                replay.getattr("relations")?.getattr("anchor")?,
            ),
            ("boundary_mode", replay.getattr("mode")?),
            ("captured_at", captured_at),
            ("ranges", ranges),
        ],
    )
}

fn exact_workflow<'py>(
    py: Python<'py>,
    workflow: &Value<'py>,
    statements: &[Value<'py>],
) -> PyResult<Value<'py>> {
    let template: Value<'py> = workflow.getattr("template")?;
    build(
        py,
        WORKFLOW_MODELS,
        "BuildWorkflow",
        &[
            ("mode", template.getattr("mode")?),
            ("plan_json", template.getattr("plan_json")?),
            ("statements", tuple(py, statements.to_vec())?),
        ],
    )
}

fn has_paused_prefix(step_id: &str) -> bool {
    PAUSED_SOURCE_STEP_PREFIXES
        .iter()
        .any(|prefix| step_id.starts_with(prefix))
}

struct Failure<'py> {
    cause: Value<'py>,
    failed_step_id: String,
}

fn recover_managed_sources<'py>(
    py: Python<'py>,
    workflow: &Value<'py>,
    connection: &Value<'py>,
    emitter: &Value<'py>,
    failure: &Failure<'py>,
    completed_step_ids: &[String],
    start_sequence: usize,
) -> PyResult<Vec<Value<'py>>> {
    let recovery: Vec<Value<'py>> = items(&workflow.getattr("source_recovery_statements")?)?;
    let view_prefix: String =
        class(py, WORKFLOW_CONSTANTS, "RECOVER_MODEL_VIEW_STEP_PREFIX")?.extract()?;
    let mut restores_views: bool = false;
    for statement in &recovery {
        if string(statement, "step_id")?.starts_with(&view_prefix) {
            restores_views = true;
            break;
        }
    }
    let source_was_paused: bool = has_paused_prefix(&failure.failed_step_id)
        || completed_step_ids
            .iter()
            .any(|step_id| has_paused_prefix(step_id));
    if !source_was_paused && !restores_views {
        return Ok(Vec::new());
    }
    let mut pending: Vec<Value<'py>> = Vec::new();
    for statement in recovery {
        let activation: String =
            string(&statement, "step_id")?.replacen("recover_", "activate_", 1);
        if !completed_step_ids.contains(&activation) {
            pending.push(statement);
        }
    }
    let completed: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, completed_step_ids)?;
    items(&call(
        py,
        RECOVERY_WORKFLOW,
        "execute_recovery_statements",
        &[
            ("statements", tuple(py, pending)?),
            ("connection", connection.clone()),
            ("emitter", emitter.clone()),
            ("cause", failure.cause.clone()),
            (
                "start_sequence",
                start_sequence.into_pyobject(py)?.into_any(),
            ),
            ("completed_step_ids", completed.into_any()),
            ("retry_command", text(py, RETRY_COMMAND)),
        ],
    )?)
}

/// Whether `error` is one a failed step translates into a direct workflow failure.
fn is_step_failure(py: Python<'_>, error: &PyErr) -> PyResult<bool> {
    Ok(
        error.is_instance(py, &class(py, ADAPTER_EXCEPTIONS, "AdapterError")?)
            || error.is_instance_of::<PyKeyError>(py)
            || error.is_instance_of::<pyo3::exceptions::PyKeyboardInterrupt>(py)
            || error.is_instance(
                py,
                &class(py, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?,
            ),
    )
}

struct Execution<'py> {
    prepared: Vec<Value<'py>>,
    attempted: Vec<Value<'py>>,
    results: Vec<Value<'py>>,
    captures: Vec<Value<'py>>,
    captures_by_model: Bound<'py, PyDict>,
    completed_step_ids: Vec<String>,
}

/// Execute one template statement, realizing its replay SQL and decoding its capture.
#[allow(clippy::too_many_arguments)]
fn execute_step<'py>(
    py: Python<'py>,
    workflow: &Value<'py>,
    connection: &Value<'py>,
    emitter: &Value<'py>,
    execution: &mut Execution<'py>,
    statement: &mut Value<'py>,
    runtime_by_replay_step: &Bound<'py, PyDict>,
    runtime_by_capture_step: &Bound<'py, PyDict>,
) -> PyResult<()> {
    let step_id: Value<'py> = statement.getattr("step_id")?;
    if let Some(runtime_replay) = runtime_by_replay_step.get_item(&step_id)? {
        let model_name: Value<'py> = runtime_replay.getattr("model_name")?;
        let Some(capture) = execution.captures_by_model.get_item(&model_name)? else {
            return Err(PyKeyError::new_err(model_name.unbind()));
        };
        *statement =
            realize_replay_statement(py, statement, &runtime_replay, &capture, connection)?;
        let sequence: usize = statement.getattr("sequence")?.extract()?;
        execution.prepared[sequence - 1] = statement.clone();
        emit_workflow_prepared(py, emitter, &execution.prepared)?;
    }
    execution.attempted.push(statement.clone());
    let step_execution: Value<'py> = call(
        py,
        WAREHOUSE_WORKFLOW,
        "execute_warehouse_workflow",
        &[
            ("statements", tuple(py, vec![statement.clone()])?),
            ("connection", connection.clone()),
            ("emitter", emitter.clone()),
        ],
    )?;
    let statement_results: Vec<Value<'py>> = items(&step_execution.getattr("statement_results")?)?;
    execution.results.extend(statement_results.iter().cloned());
    let step_id: String = string(statement, "step_id")?;
    if !execution.completed_step_ids.contains(&step_id) {
        execution.completed_step_ids.push(step_id.clone());
    }
    if let Some(capture_runtime) = runtime_by_capture_step.get_item(&step_id)? {
        let first_result: Value<'py> = statement_results
            .first()
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("tuple index out of range"))?
            .clone();
        let capture: Value<'py> = decode_capture(
            py,
            &workflow.getattr("workflow_id")?,
            &capture_runtime,
            &first_result.getattr("query_result")?,
        )?;
        execution.captures.push(capture.clone());
        execution
            .captures_by_model
            .set_item(capture.getattr("logical_model_name")?, capture)?;
    }
    Ok(())
}

/// Execute a direct template while retaining every exact adapter statement.
#[pyfunction]
#[pyo3(signature = (*, workflow, connection, emitter = None))]
fn execute_direct_build_workflow<'py>(
    py: Python<'py>,
    workflow: &Value<'py>,
    connection: &Value<'py>,
    emitter: Option<&Value<'py>>,
) -> PyResult<Value<'py>> {
    let emitter: Value<'py> = emitter.cloned().unwrap_or_else(|| none(py));
    let mut execution: Execution<'py> = Execution {
        prepared: items(&workflow.getattr("statements")?)?,
        attempted: Vec::new(),
        results: Vec::new(),
        captures: Vec::new(),
        captures_by_model: PyDict::new(py),
        completed_step_ids: Vec::new(),
    };
    emit_workflow_prepared(py, &emitter, &execution.prepared)?;
    let runtime_by_replay_step: Bound<'py, PyDict> = PyDict::new(py);
    let runtime_by_capture_step: Bound<'py, PyDict> = PyDict::new(py);
    for runtime in items(&workflow.getattr("runtime_replays")?)? {
        runtime_by_replay_step.set_item(runtime.getattr("replay_step_id")?, &runtime)?;
        runtime_by_capture_step.set_item(runtime.getattr("capture_step_id")?, &runtime)?;
    }
    let workflow_error: Value<'py> = class(py, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?;
    for template_statement in items(&workflow.getattr("template")?.getattr("statements")?)? {
        let mut statement: Value<'py> = template_statement;
        let Err(error) = execute_step(
            py,
            workflow,
            connection,
            &emitter,
            &mut execution,
            &mut statement,
            &runtime_by_replay_step,
            &runtime_by_capture_step,
        ) else {
            continue;
        };
        if !is_step_failure(py, &error)? {
            return Err(error);
        }
        let translate = || -> PyResult<PyErr> {
            let handled: Value<'py> = error.value(py).clone().into_any();
            let workflow_failure: bool = handled.is_instance(&workflow_error)?;
            let failure: Failure<'py> = Failure {
                cause: if workflow_failure {
                    handled.getattr("cause")?
                } else {
                    handled.clone()
                },
                failed_step_id: if workflow_failure {
                    string(&handled, "failed_step_id")?
                } else {
                    string(&statement, "step_id")?
                },
            };
            let recovery: Vec<Value<'py>> = recover_managed_sources(
                py,
                workflow,
                connection,
                &emitter,
                &failure,
                &execution.completed_step_ids,
                execution.attempted.len() + 1,
            )?;
            execution.attempted.extend(recovery);
            let partial_result: Value<'py> = build(
                py,
                WORKFLOW_MODELS,
                "WorkflowExecutionResult",
                &[("statement_results", tuple(py, execution.results)?)],
            )?;
            let raised: Value<'py> = build(
                py,
                DIRECT_EXCEPTIONS,
                "DirectWorkflowExecutionError",
                &[
                    (
                        "workflow",
                        exact_workflow(py, workflow, &execution.attempted)?,
                    ),
                    ("partial_result", partial_result),
                    ("captures", tuple(py, execution.captures)?),
                    ("failed_step_id", text(py, &failure.failed_step_id)),
                    ("cause", failure.cause.clone()),
                ],
            )?;
            raised_from(&raised, &handled, &failure.cause)?;
            Ok(PyErr::from_value(raised))
        };
        return Err(translate().unwrap_or_else(|raised| while_handling(py, raised, &error)));
    }
    let result: Value<'py> = build(
        py,
        WORKFLOW_MODELS,
        "WorkflowExecutionResult",
        &[("statement_results", tuple(py, execution.results)?)],
    )?;
    build(
        py,
        DIRECT_MODELS,
        "DirectRuntimeExecution",
        &[
            (
                "workflow",
                exact_workflow(py, workflow, &execution.attempted)?,
            ),
            ("execution", result),
            ("captures", tuple(py, execution.captures)?),
        ],
    )
}

fn fingerprint_records<'py>(
    py: Python<'py>,
    models: &[Value<'py>],
    database: &Value<'py>,
    workflow_id: &Value<'py>,
    tool_version: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut records: Vec<Value<'py>> = Vec::with_capacity(models.len());
    for model in models {
        records.push(DirectModelFingerprint::record(
            py,
            model,
            database,
            workflow_id,
            tool_version,
        )?);
    }
    tuple(py, records)
}

/// Render and execute fingerprint records through the observation workflow.
fn persist_fingerprint_records<'py>(
    py: Python<'py>,
    records: Value<'py>,
    database: Value<'py>,
    connection: &Value<'py>,
) -> PyResult<()> {
    let rendered: Vec<String> = method(
        connection,
        "render_direct_fingerprint_observations",
        &[("database", database), ("fingerprints", records)],
    )?
    .extract()?;
    let finalization: Value<'py> =
        class(py, WORKFLOW_TYPES, "WorkflowPhase")?.getattr("FINALIZATION")?;
    let mutation: Value<'py> = class(py, WORKFLOW_TYPES, "StatementIntent")?.getattr("MUTATION")?;
    let mut statements: Vec<Value<'py>> = Vec::with_capacity(rendered.len());
    for (position, sql) in rendered.iter().enumerate() {
        let sequence: usize = position + 1;
        statements.push(build(
            py,
            WORKFLOW_MODELS,
            "WarehouseStatement",
            &[
                ("sequence", sequence.into_pyobject(py)?.into_any()),
                (
                    "step_id",
                    text(py, &format!("record_direct_fingerprint_{sequence}")),
                ),
                ("phase", finalization.clone()),
                ("intent", mutation.clone()),
                ("sql", text(py, &terminate_sql(sql))),
            ],
        )?);
    }
    call(
        py,
        OBSERVATION_WORKFLOW,
        "execute_observation_workflow",
        &[
            ("statements", tuple(py, statements)?),
            ("connection", connection.clone()),
        ],
    )?;
    Ok(())
}

/// The applied models' baselines written to the metadata database.
fn persist_applied_fingerprints<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    connection: &Value<'py>,
    applied_model_names: &Value<'py>,
) -> PyResult<()> {
    let model_by_name: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(
        &request
            .getattr("realized_project")?
            .getattr("project")?
            .getattr("models")?,
    )? {
        model_by_name.set_item(model.getattr("key")?.getattr("name")?, model)?;
    }
    let mut models: Vec<Value<'py>> = Vec::new();
    for entry in items(&request.getattr("plan")?.getattr("entries")?)? {
        let name: Value<'py> = entry.getattr("model_key")?.getattr("name")?;
        if !applied_model_names.is_none() && !applied_model_names.contains(&name)? {
            continue;
        }
        match model_by_name.get_item(&name)? {
            Some(model) => models.push(model),
            None => return Err(PyKeyError::new_err(name.unbind())),
        }
    }
    let records: Value<'py> = fingerprint_records(
        py,
        &models,
        &request.getattr("database")?,
        &request.getattr("workflow_id")?,
        &request.getattr("tool_version")?,
    )?;
    persist_fingerprint_records(
        py,
        records,
        request.getattr("metadata_database")?,
        connection,
    )
}

/// Best-effort write applied logical SQL without changing the command outcome, returning a
/// warning when the baseline was not recorded.
#[pyfunction]
#[pyo3(signature = (*, request, connection, applied_model_names = None))]
fn persist_direct_fingerprints<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    connection: &Value<'py>,
    applied_model_names: Option<&Value<'py>>,
) -> PyResult<Option<String>> {
    let applied_model_names: Value<'py> = applied_model_names.cloned().unwrap_or_else(|| none(py));
    let Err(error) = persist_applied_fingerprints(py, request, connection, &applied_model_names)
    else {
        return Ok(None);
    };
    let workflow_error: Value<'py> = class(py, WORKFLOW_EXCEPTIONS, "WorkflowExecutionError")?;
    let cause: Value<'py> = if error.is_instance(py, &workflow_error) {
        error.value(py).getattr("cause")?
    } else if error.is_instance(py, &class(py, ADAPTER_EXCEPTIONS, "AdapterError")?) {
        error.value(py).clone().into_any()
    } else {
        return Err(error);
    };
    Ok(Some(format!(
        "Direct SQL baseline was not recorded: {}",
        cause.str()?
    )))
}

/// Persist verified direct model baselines without changing warehouse relations.
#[pyfunction]
#[pyo3(signature = (
    *, models, target_database, metadata_database, workflow_id, tool_version, connection
))]
fn persist_reconciled_direct_fingerprints<'py>(
    py: Python<'py>,
    models: &Value<'py>,
    target_database: &Value<'py>,
    metadata_database: &Value<'py>,
    workflow_id: &Value<'py>,
    tool_version: &Value<'py>,
    connection: &Value<'py>,
) -> PyResult<()> {
    let records: Value<'py> = fingerprint_records(
        py,
        &items(models)?,
        target_database,
        workflow_id,
        tool_version,
    )?;
    persist_fingerprint_records(py, records, metadata_database.clone(), connection)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(assemble_direct_build_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(execute_direct_build_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(persist_direct_fingerprints, module)?)?;
    module.add_function(wrap_pyfunction!(
        persist_reconciled_direct_fingerprints,
        module
    )?)
}
