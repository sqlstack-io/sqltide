//! The virtual build over the adapter connection: the deployment identity fixed before
//! confirmation, the bounded-replay fallback decided from the live catalog, and the
//! authoritative workflow assembled once from the confirmed plan. Rendering goes through the
//! Python adapter connection; ordering and SQL are the native workflow planners'.

use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use pyo3::exceptions::{PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFloat, PyList, PyTuple};
use sqltide_executor::deployment_identity::{
    created_at_from_deployment_id, creation_timestamp, generated_deployment_id,
};
use sqltide_executor::direct_workflow;
use sqltide_executor::virtual_workflow::{self, Deployment, Root};
use sqltide_planner::direct::sha256_hex;
use sqltide_planner::naming::is_deployment_id;

use crate::direct_build::{
    PolicyViewSteps, Value, boolean, build, call, class, is_a, items, method, native_statements,
    none, or, replace, string, text, tuple, warehouse_statements,
};
use crate::model_sql::chained;
use crate::planner_support::{
    build_adapter_metadata_state, build_adapter_resource, build_metadata_state,
    build_normalized_fingerprint, build_shadow_adapter_resource,
};
use crate::population::bounded_replay_outcome;
use crate::population_planning::{
    build_population_replay_templates, expand_population_plan, plan_population_objects,
    plan_population_sources, row_error_policy_restore_statements, with_metadata_dead_letter_target,
};
use crate::workflow::replay;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const PLANNER_CONSTANTS: &str = "sqltide.compiler.planner.constants";
const POPULATION_MODELS: &str = "sqltide.executor.population.models";
const BACKFILL_MODELS: &str = "sqltide.executor.backfill.models";
const BACKFILL_EXCEPTIONS: &str = "sqltide.executor.backfill.exceptions";
const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const DEPLOYMENT_STATUS_BACKFILLING: &str = "backfilling";
const DEPLOYMENT_ID_PATTERN_ERROR: &str =
    "Deployment ID must match YYYYMMDDTHHMMSSZ_<alphanumeric-suffix>";
const DEPLOYMENT_ID_TIMESTAMP_ERROR: &str =
    "Deployment ID timestamp must be a valid UTC date and time";
/// Policy view swaps of a virtual build run no audits.
const AUDIT_SAMPLE_LIMIT: usize = 0;

fn backfill_error(py: Python<'_>, message: String) -> PyErr {
    match class(py, BACKFILL_EXCEPTIONS, "BackfillExecutionError")
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn constant<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    class(py, module, name)
}

/// `float(value)`.
fn float(py: Python<'_>, value: &Value<'_>) -> PyResult<f64> {
    py.get_type::<PyFloat>().call1((value,))?.extract()
}

fn is_none(value: &Value<'_>) -> bool {
    value.is_none()
}

/// The first desired table whose key equals `key`.
fn desired_table<'py>(
    desired_state: &Value<'py>,
    key: &Value<'py>,
) -> PyResult<Option<Value<'py>>> {
    for desired in items(&desired_state.getattr("objects")?)? {
        if is_a(&desired, COMPILE_MODELS, "DesiredTable")? && desired.getattr("key")?.eq(key)? {
            return Ok(Some(desired));
        }
    }
    Ok(None)
}

fn column_names<'py>(py: Python<'py>, relation: &Value<'py>) -> PyResult<Value<'py>> {
    let mut names: Vec<Value<'py>> = Vec::new();
    for column in items(&relation.getattr("columns")?)? {
        names.push(column.getattr("name")?);
    }
    tuple(py, names)
}

/// `next(column.type for column in relation.columns if column.name == name)`.
fn column_type<'py>(relation: &Value<'py>, name: &Value<'py>) -> PyResult<Value<'py>> {
    for column in items(&relation.getattr("columns")?)? {
        if column.getattr("name")?.eq(name)? {
            return column.getattr("type");
        }
    }
    Err(PyStopIteration::new_err(()))
}

/// Fix one virtual build identity before connected planning.
#[pyfunction]
#[pyo3(signature = (*, deployment_id))]
fn build_backfill_deployment_identity<'py>(
    py: Python<'py>,
    deployment_id: &Value<'py>,
) -> PyResult<Value<'py>> {
    let micros: i64 = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX)
        });
    let current_timestamp: String = creation_timestamp(micros);
    let resolved: String = if deployment_id.is_truthy()? {
        deployment_id.extract()?
    } else {
        let hex: String = py
            .import("uuid")?
            .call_method0("uuid4")?
            .getattr("hex")?
            .extract()?;
        generated_deployment_id(micros, &hex[..6])
    };
    if !is_deployment_id(&resolved) {
        return Err(backfill_error(py, DEPLOYMENT_ID_PATTERN_ERROR.to_owned()));
    }
    let created_at: String = if is_none(deployment_id) {
        current_timestamp
    } else {
        created_at_from_deployment_id(&resolved).map_err(|message| {
            // `raise BackfillExecutionError(...) from error` over the `strptime` refusal.
            let error: PyErr = backfill_error(py, DEPLOYMENT_ID_TIMESTAMP_ERROR.to_owned());
            let refusal: Value<'_> = PyValueError::new_err(message)
                .into_value(py)
                .into_bound(py)
                .into_any();
            match chained(error.value(py), &refusal, Some(&refusal)) {
                Ok(()) => error,
                Err(failure) => failure,
            }
        })?
    };
    build(
        py,
        BACKFILL_MODELS,
        "BackfillDeploymentIdentity",
        &[
            ("deployment_id", text(py, &resolved)),
            ("created_at", text(py, &created_at)),
        ],
    )
}

/// Columns of the physical table the logical root binds, or `None` without a binding.
fn live_column_names(
    catalog: &Value<'_>,
    logical_table_name: &Value<'_>,
) -> PyResult<Option<HashSet<String>>> {
    let logical_relation: Value<'_> = catalog.call_method1("relation", (logical_table_name,))?;
    if is_none(&logical_relation) {
        return Ok(None);
    }
    let binding: Value<'_> = logical_relation.getattr("stable_binding_name")?;
    if is_none(&binding) {
        return Ok(None);
    }
    let relation: Value<'_> = catalog.call_method1("relation", (binding,))?;
    if is_none(&relation) {
        return Ok(Some(HashSet::new()));
    }
    items(&relation.getattr("columns")?)?
        .iter()
        .map(|column| string(column, "name"))
        .collect::<PyResult<_>>()
        .map(Some)
}

fn resolve_subtree_behavior<'py>(
    py: Python<'py>,
    catalog: &Value<'py>,
    subtree: &Value<'py>,
    desired_state: &Value<'py>,
    replay_lineage_mode: &Value<'py>,
) -> PyResult<Value<'py>> {
    let seeded: Value<'py> = constant(
        py,
        PLANNER_CONSTANTS,
        "REBUILD_EXECUTION_MODE_SEEDED_BOUNDED",
    )?;
    if subtree.getattr("execution_mode")?.ne(&seeded)? {
        return Ok(subtree.clone());
    }
    let Some(root_table) = desired_table(desired_state, &subtree.getattr("root_key")?)? else {
        return Ok(subtree.clone());
    };
    let fallback_class: Value<'py> = class(py, DISCOVERY_TYPES, "BoundedReplayFallback")?;
    let fallback: Value<'py> =
        fallback_class.call1((root_table.getattr("bounded_replay_fallback")?,))?;
    let mode: String = replay_lineage_mode.str()?.extract()?;
    let outcome: &str = bounded_replay_outcome(
        Some(&mode),
        !is_none(&subtree.getattr("forced_start_time")?),
        live_column_names(catalog, &root_table.getattr("name")?)?,
        fallback.eq(fallback_class.getattr("BOUNDED_WITHOUT_HISTORY")?)?,
    )?;
    match outcome {
        "without_history" => replace(
            subtree,
            &[
                (
                    "execution_mode",
                    constant(
                        py,
                        PLANNER_CONSTANTS,
                        "REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED",
                    )?,
                ),
                ("history_preserving_bounded_supported", boolean(py, false)?),
                ("resolved_bounded_replay_fallback", fallback),
            ],
        ),
        "full" => replace(
            subtree,
            &[
                (
                    "execution_mode",
                    constant(py, PLANNER_CONSTANTS, "REBUILD_EXECUTION_MODE_FULL")?,
                ),
                (
                    "requested_start_time",
                    subtree.getattr("forced_start_time")?,
                ),
                ("forced_start_time", none(py)),
                ("execution_lookback_seconds", none(py)),
                ("history_preserving_bounded_supported", boolean(py, false)?),
                ("resolved_bounded_replay_fallback", fallback),
            ],
        ),
        _ => Ok(subtree.clone()),
    }
}

/// The plan with each seeded bounded rebuild resolved against its live root table.
#[pyfunction]
#[pyo3(signature = (*, catalog, deployment_plan, desired_state, default_database, replay_lineage_mode))]
fn resolve_unsupported_bounded_replay_behavior<'py>(
    py: Python<'py>,
    catalog: &Value<'py>,
    deployment_plan: &Value<'py>,
    desired_state: &Value<'py>,
    default_database: &Value<'py>,
    replay_lineage_mode: &Value<'py>,
) -> PyResult<Value<'py>> {
    let _ = default_database;
    let mut resolved: Vec<Value<'py>> = Vec::new();
    for subtree in items(&deployment_plan.getattr("rebuild_subtrees")?)? {
        resolved.push(resolve_subtree_behavior(
            py,
            catalog,
            &subtree,
            desired_state,
            replay_lineage_mode,
        )?);
    }
    replace(
        deployment_plan,
        &[("rebuild_subtrees", tuple(py, resolved)?)],
    )
}

fn confirmed_plan<'py>(py: Python<'py>, request: &Value<'py>) -> PyResult<Value<'py>> {
    let plan: Value<'py> = request.getattr("confirmed_plan")?;
    if is_none(&plan) {
        return Err(backfill_error(
            py,
            "Virtual workflow assembly requires the confirmed deployment plan".to_owned(),
        ));
    }
    Ok(plan)
}

fn confirmed_deployment_id<'py>(py: Python<'py>, request: &Value<'py>) -> PyResult<Value<'py>> {
    let plan: Value<'py> = confirmed_plan(py, request)?;
    let deployment_id: Value<'py> = request.getattr("deployment_id")?;
    if is_none(&deployment_id) || plan.getattr("deployment_id")?.ne(&deployment_id)? {
        return Err(backfill_error(
            py,
            "Virtual workflow deployment identity does not match the confirmed plan".to_owned(),
        ));
    }
    Ok(deployment_id)
}

fn confirmed_catalog<'py>(
    py: Python<'py>,
    catalog: Value<'py>,
    catalog_name: &str,
) -> PyResult<Value<'py>> {
    if is_none(&catalog) {
        return Err(backfill_error(
            py,
            format!(
                "Virtual workflow assembly requires the pre-confirmation {catalog_name} catalog"
            ),
        ));
    }
    Ok(catalog)
}

fn population_plan<'py>(
    py: Python<'py>,
    deployment_plan: &Value<'py>,
    execution_id: &Value<'py>,
    replay_lineage_mode: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut roots: Vec<Value<'py>> = Vec::new();
    for subtree in items(&deployment_plan.getattr("rebuild_subtrees")?)? {
        if !subtree.getattr("replay_required")?.is_truthy()? {
            continue;
        }
        let field = |name: &str| subtree.getattr(name);
        roots.push(build(
            py,
            POPULATION_MODELS,
            "PopulationRoot",
            &[
                ("root_key", field("root_key")?),
                ("affected_keys", field("affected_keys")?),
                ("upstream_boundary_key", field("upstream_boundary_key")?),
                ("replay_lineage_mode", replay_lineage_mode.clone()),
                ("execution_mode", field("execution_mode")?),
                ("forced_start_time", field("forced_start_time")?),
                (
                    "execution_lookback_seconds",
                    field("execution_lookback_seconds")?,
                ),
            ],
        )?);
    }
    let mut objects: Vec<Value<'py>> = Vec::new();
    for prepared in items(&deployment_plan.getattr("prepared_shadow_objects")?)? {
        objects.push(build(
            py,
            POPULATION_MODELS,
            "PopulationObject",
            &[
                ("logical_key", prepared.getattr("logical_key")?),
                ("physical_name", prepared.getattr("physical_name")?),
            ],
        )?);
    }
    build(
        py,
        POPULATION_MODELS,
        "PopulationPlan",
        &[
            ("execution_id", execution_id.clone()),
            ("roots", tuple(py, roots)?),
            ("objects", tuple(py, objects)?),
        ],
    )
}

fn object_state_records<'py>(
    py: Python<'py>,
    desired_objects: &Value<'py>,
    deployment_id: &Value<'py>,
    recorded_at: &Value<'py>,
) -> PyResult<Value<'py>> {
    let state_kind: Value<'py> = constant(
        py,
        ADAPTER_CONSTANTS,
        "VIRTUAL_OBJECT_STATE_KIND_DEPLOYMENT",
    )?;
    let mut records: Vec<Value<'py>> = Vec::new();
    for desired in items(desired_objects)? {
        let materialized: bool = is_a(&desired, COMPILE_MODELS, "DesiredMaterializedView")?;
        let normalized_query: Value<'py> = if materialized {
            desired.getattr("comparison_spec")?.getattr("query")?
        } else if is_a(&desired, COMPILE_MODELS, "DesiredView")? {
            desired.getattr("query")?
        } else {
            none(py)
        };
        let key: Value<'py> = desired.getattr("key")?;
        let spec: Value<'py> = desired.getattr(if materialized {
            "comparison_spec"
        } else {
            "spec"
        })?;
        records.push(build(
            py,
            PLANNER_MODELS,
            "ObjectStateRecord",
            &[
                ("deployment_id", deployment_id.clone()),
                (
                    "key",
                    build(
                        py,
                        COMPILE_MODELS,
                        "ObjectKey",
                        &[
                            ("database", key.getattr("database")?),
                            ("object_type", key.getattr("object_type")?),
                            ("name", key.getattr("name")?),
                        ],
                    )?,
                ),
                (
                    "normalized_fingerprint",
                    text(py, &build_normalized_fingerprint(&spec)?),
                ),
                ("normalized_query", normalized_query),
                ("recorded_at", recorded_at.clone()),
                ("state_kind", state_kind.clone()),
            ],
        )?);
    }
    tuple(py, records)
}

/// Field values of a deployment, gathered before any warehouse mutation.
struct DeploymentFields<'py> {
    deployment_id: Value<'py>,
    created_at: Value<'py>,
    replay_lineage_mode: Value<'py>,
    workflow_fingerprint: String,
    tool_version: Value<'py>,
}

/// The complete candidate metadata batch.
fn deployment_metadata_state<'py>(
    py: Python<'py>,
    deployment_plan: &Value<'py>,
    desired_objects: &Value<'py>,
    fields: DeploymentFields<'py>,
) -> PyResult<Value<'py>> {
    let object_states: Value<'py> = object_state_records(
        py,
        desired_objects,
        &fields.deployment_id,
        &fields.created_at,
    )?;
    let mut selected_root_keys: Vec<Value<'py>> = Vec::new();
    for subtree in items(&deployment_plan.getattr("rebuild_subtrees")?)? {
        selected_root_keys.push(subtree.getattr("root_key")?);
    }
    let mut warning_codes: Vec<Value<'py>> = Vec::new();
    for warning in items(&deployment_plan.getattr("warnings")?)? {
        warning_codes.push(warning.getattr("warning_code")?);
    }
    let mut mappings: Vec<Value<'py>> = Vec::new();
    for prepared in items(&deployment_plan.getattr("prepared_shadow_objects")?)? {
        mappings.push(build(
            py,
            PLANNER_MODELS,
            "PreparedObjectMapping",
            &[
                ("logical_key", prepared.getattr("logical_key")?),
                ("physical_name", prepared.getattr("physical_name")?),
                (
                    "logical_model_name",
                    prepared.getattr("logical_model_name")?,
                ),
            ],
        )?);
    }
    let deployment: Value<'py> = build(
        py,
        PLANNER_MODELS,
        "DeploymentRecord",
        &[
            ("deployment_id", fields.deployment_id),
            ("created_at", fields.created_at),
            ("status", text(py, DEPLOYMENT_STATUS_BACKFILLING)),
            ("replay_lineage_mode", fields.replay_lineage_mode),
            ("selected_root_keys", tuple(py, selected_root_keys)?),
            ("warning_codes", tuple(py, warning_codes)?),
            ("prepared_object_mappings", tuple(py, mappings)?),
            (
                "workflow_fingerprint",
                text(py, &fields.workflow_fingerprint),
            ),
            ("tool_version", fields.tool_version),
        ],
    )?;
    let empty: Value<'py> = PyTuple::empty(py).into_any();
    let state: Value<'py> = build_metadata_state(
        py,
        &object_states,
        &tuple(py, vec![deployment])?,
        &empty,
        &empty,
    )?;
    build_adapter_metadata_state(py, &state)
}

/// `(resource name, SQL)` of each realization, created only when absent.
fn rendered_realizations<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    realizations: &Value<'py>,
    metadata_database: &Value<'py>,
) -> PyResult<Vec<(String, String)>> {
    let mut rendered: Vec<(String, String)> = Vec::new();
    for realization in items(realizations)? {
        let resource: Value<'py> = realization.getattr("resource")?;
        let database: Value<'py> = realization.getattr("database")?;
        let name: String = string(&resource, "name")?;
        let sql: String = method(
            client,
            "render_resource",
            &[
                (
                    "resource",
                    with_metadata_dead_letter_target(&resource, &database, metadata_database)?,
                ),
                ("database", database),
                ("if_not_exists", boolean(py, true)?),
            ],
        )?
        .extract()?;
        rendered.push((name, sql));
    }
    Ok(rendered)
}

fn render_landing_view<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    landing_view: &Value<'py>,
) -> PyResult<String> {
    let built: Value<'py> = build_adapter_resource(py, landing_view)?;
    if !is_a(&built, ADAPTER_MODELS, "AdapterMaterializedView")? {
        return Err(backfill_error(
            py,
            format!(
                "Landing view '{}' did not realize as a materialized view",
                landing_view.getattr("name")?.str()?
            ),
        ));
    }
    method(
        client,
        "render_resource",
        &[
            ("resource", built),
            (
                "database",
                or(
                    landing_view.getattr("key")?.getattr("database")?,
                    &request.getattr("default_database")?,
                )?,
            ),
            ("if_not_exists", boolean(py, true)?),
        ],
    )?
    .extract()
}

/// The type of the replay's position column: the declared anchor's, the cataloged anchor's,
/// or the generic type of the boundary.
fn boundary_column_type<'py>(
    py: Python<'py>,
    desired_state: &Value<'py>,
    target_catalog: &Value<'py>,
    replay: &Value<'py>,
    anchor_key: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mode: Value<'py> = replay.getattr("mode")?;
    let modes: Value<'py> = class(py, ADAPTER_TYPES, "AdapterReplayBoundaryMode")?;
    if mode.eq(modes.getattr("OFFSETS")?)? {
        return Ok(none(py));
    }
    let columns: Value<'py> = replay.getattr("columns")?;
    let positions: Bound<'py, PyDict> = PyDict::new(py);
    positions.set_item(modes.getattr("CURSOR")?, columns.getattr("cursor")?)?;
    positions.set_item(modes.getattr("TIMESTAMP")?, columns.getattr("timestamp")?)?;
    positions.set_item(modes.getattr("LANDED_AT")?, columns.getattr("landed_at")?)?;
    let position_column: Value<'py> = positions.as_any().get_item(&mode)?;
    if let Some(desired) = desired_table(desired_state, anchor_key)? {
        return column_type(&desired, &position_column);
    }
    // An external source's replay anchor is its own table, so its separate lookup of the same
    // relation never found more than this one.
    let relation: Value<'py> = target_catalog.call_method1(
        "relation",
        (replay.getattr("relations")?.getattr("anchor")?,),
    )?;
    if !is_none(&relation) {
        return column_type(&relation, &position_column);
    }
    Ok(text(
        py,
        if mode.eq(modes.getattr("CURSOR")?)? {
            "UInt64"
        } else {
            "DateTime64(3, 'UTC')"
        },
    ))
}

fn active_relation_name<'py>(
    target_catalog: &Value<'py>,
    replay: &Value<'py>,
) -> PyResult<Value<'py>> {
    let root: Value<'py> = replay.getattr("relations")?.getattr("root")?;
    let relation: Value<'py> = target_catalog.call_method1("relation", (&root,))?;
    if is_none(&relation) {
        return Ok(root);
    }
    let binding: Value<'py> = relation.getattr("stable_binding_name")?;
    Ok(if is_none(&binding) { root } else { binding })
}

fn catalog_column_names<'py>(
    py: Python<'py>,
    target_catalog: &Value<'py>,
    relation_name: &Value<'py>,
) -> PyResult<Value<'py>> {
    let relation: Value<'py> = target_catalog.call_method1("relation", (relation_name,))?;
    if is_none(&relation) {
        return Ok(PyTuple::empty(py).into_any());
    }
    column_names(py, &relation)
}

fn anchor_column_names<'py>(
    py: Python<'py>,
    desired_state: &Value<'py>,
    target_catalog: &Value<'py>,
    replay: &Value<'py>,
    anchor_key: &Value<'py>,
) -> PyResult<Value<'py>> {
    if let Some(desired) = desired_table(desired_state, anchor_key)? {
        return column_names(py, &desired);
    }
    catalog_column_names(
        py,
        target_catalog,
        &replay.getattr("relations")?.getattr("anchor")?,
    )
}

/// `anchor_exists and offsets and managed landing anchor and repeated offsets`, with Python's
/// `and` returning the first falsy operand or the last one.
fn dedupe_offsets<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    replay: &Value<'py>,
    anchor_exists: bool,
) -> PyResult<Value<'py>> {
    if !anchor_exists {
        return boolean(py, false);
    }
    let offsets: Value<'py> =
        class(py, ADAPTER_TYPES, "AdapterReplayBoundaryMode")?.getattr("OFFSETS")?;
    if !replay.getattr("mode")?.is(&offsets) {
        return boolean(py, false);
    }
    let relations: Value<'py> = replay.getattr("relations")?;
    let managed: Value<'py> = request
        .getattr("desired_state")?
        .call_method1("is_managed_landing_table", (relations.getattr("anchor")?,))?;
    if !managed.is_truthy()? {
        return Ok(managed);
    }
    let columns: Value<'py> = replay.getattr("columns")?;
    method(
        client,
        "replay_anchor_has_repeated_offsets",
        &[
            ("database", replay.getattr("database")?),
            ("relation", relations.getattr("anchor")?),
            ("partition_column", columns.getattr("partition")?),
            ("offset_column", columns.getattr("offset")?),
        ],
    )
}

fn native_root<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    target_catalog: &Value<'py>,
    root: &Value<'py>,
    replay_request: &Value<'py>,
) -> PyResult<Root> {
    let anchor: Value<'py> = replay_request.getattr("relations")?.getattr("anchor")?;
    let anchor_exists = || -> PyResult<bool> {
        Ok(!is_none(
            &target_catalog.call_method1("relation", (&anchor,))?,
        ))
    };
    let desired_state: Value<'py> = request.getattr("desired_state")?;
    let anchor_key: Value<'py> = root.getattr("upstream_boundary_key")?;
    let dedupe: Value<'py> = dedupe_offsets(py, request, client, replay_request, anchor_exists()?)?;
    let deployment_request: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterDeploymentReplayRequest",
        &[
            (
                "replay",
                replace(replay_request, &[("dedupe_offsets", dedupe)])?,
            ),
            ("metadata_database", request.getattr("metadata_database")?),
            ("deployment_id", confirmed_deployment_id(py, request)?),
            (
                "boundary_column_type",
                boundary_column_type(
                    py,
                    &desired_state,
                    target_catalog,
                    replay_request,
                    &anchor_key,
                )?,
            ),
            (
                "active_relation_name",
                active_relation_name(target_catalog, replay_request)?,
            ),
            (
                "active_column_names",
                catalog_column_names(
                    py,
                    target_catalog,
                    &active_relation_name(target_catalog, replay_request)?,
                )?,
            ),
            (
                "anchor_column_names",
                anchor_column_names(
                    py,
                    &desired_state,
                    target_catalog,
                    replay_request,
                    &anchor_key,
                )?,
            ),
        ],
    )?;
    let rendered: Vec<String> =
        items(&client.call_method1("render_replay_from_deployment", (deployment_request,))?)?
            .iter()
            .map(|sql| sql.extract())
            .collect::<PyResult<_>>()?;
    Ok(Root {
        name: string(&root.getattr("root_key")?, "name")?,
        replay: replay(replay_request)?,
        persist_watermarks: root.getattr("persist_watermarks")?.extract()?,
        boundary_key: (
            anchor_key.getattr("database")?.extract()?,
            anchor_key.getattr("object_type")?.str()?.extract()?,
            string(&anchor_key, "name")?,
        ),
        anchor_existed_at_confirmation: anchor_exists()?,
        rendered,
    })
}

/// The confirmed catalogs and plans every virtual statement is assembled from.
struct Confirmed<'py> {
    target_catalog: Value<'py>,
    metadata_catalog: Value<'py>,
    metadata_state: Value<'py>,
    source_preparation: Value<'py>,
    source_realizations: Value<'py>,
    population_plan: Value<'py>,
    replay_templates: Vec<Value<'py>>,
}

fn assemble_statements<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    confirmed: &Confirmed<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let target_catalog: &Value<'py> = &confirmed.target_catalog;
    let default_database: Value<'py> = request.getattr("default_database")?;
    let metadata_database: Value<'py> = request.getattr("metadata_database")?;
    let desired_state: Value<'py> = request.getattr("desired_state")?;
    let metadata_names: Value<'py> = confirmed.metadata_catalog.call_method0("relation_names")?;
    let deployment: Value<'py> = confirmed
        .metadata_state
        .getattr("deployments")?
        .get_item(0)?;
    let mut prepared: Vec<(String, Option<String>)> = Vec::new();
    for shadow in items(&confirmed_plan(py, request)?.getattr("prepared_shadow_objects")?)? {
        let name: Value<'py> = shadow.getattr("physical_name")?;
        let relation: Value<'py> = target_catalog.call_method1("relation", (&name,))?;
        let definition: Option<String> = if is_none(&relation) {
            None
        } else {
            Some(or(relation.getattr("definition_sql")?, &text(py, ""))?.extract()?)
        };
        prepared.push((name.extract()?, definition));
    }
    let ensure_database_sql: String = client
        .call_method1("render_ensure_database", (&default_database,))?
        .extract()?;
    let migrate_metadata_sql: Vec<String> =
        items(&client.call_method1("render_migrate_metadata_state", (&metadata_database,))?)?
            .iter()
            .map(|sql| sql.extract())
            .collect::<PyResult<_>>()?;
    let source_realizations: Vec<(String, String)> = rendered_realizations(
        py,
        client,
        &confirmed.source_realizations,
        &metadata_database,
    )?;
    let persist_metadata_sql: Vec<String> = items(&method(
        client,
        "render_persist_metadata_state",
        &[
            ("database", metadata_database.clone()),
            ("state", confirmed.metadata_state.clone()),
        ],
    )?)?
    .iter()
    .map(|sql| sql.extract())
    .collect::<PyResult<_>>()?;
    let realizations: Vec<(String, String)> = rendered_realizations(
        py,
        client,
        &plan_population_objects(
            py,
            &confirmed.population_plan,
            &desired_state,
            &default_database,
            Some(target_catalog),
        )?,
        &metadata_database,
    )?;
    let mut landing_views: Vec<(String, String)> = Vec::new();
    for landing_view in items(&confirmed.source_preparation.getattr("landing_views")?)? {
        landing_views.push((
            string(&landing_view, "name")?,
            render_landing_view(py, request, client, &landing_view)?,
        ));
    }
    let root_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for root in items(&confirmed.population_plan.getattr("roots")?)? {
        root_by_key.set_item(root.getattr("root_key")?, root)?;
    }
    let mut roots: Vec<Root> = Vec::new();
    for template in &confirmed.replay_templates {
        let (root_key, replay_request): (Value<'py>, Value<'py>) = template.extract()?;
        roots.push(native_root(
            py,
            request,
            client,
            target_catalog,
            &root_by_key.as_any().get_item(root_key)?,
            &replay_request,
        )?);
    }
    let field = |name: &str| -> PyResult<String> { deployment.getattr(name)?.str()?.extract() };
    let statements = virtual_workflow::statements(&virtual_workflow::Input {
        metadata_database: metadata_database.extract()?,
        default_database: default_database.extract()?,
        deployment_id: confirmed_deployment_id(py, request)?.extract()?,
        deployment: Deployment {
            deployment_id: deployment.getattr("deployment_id")?.extract()?,
            created_at: deployment.getattr("created_at")?.extract()?,
            replay_lineage_mode: field("replay_lineage_mode")?,
            workflow_fingerprint: deployment.getattr("workflow_fingerprint")?.extract()?,
            tool_version: deployment.getattr("tool_version")?.extract()?,
        },
        boundary_time: request.getattr("boundary_time")?.extract()?,
        deployments_table_exists: metadata_names.contains(constant(
            py,
            ADAPTER_CONSTANTS,
            "METADATA_DEPLOYMENTS_TABLE_NAME",
        )?)?,
        publish_table_exists: metadata_names.contains(constant(
            py,
            ADAPTER_CONSTANTS,
            "METADATA_PUBLISH_HISTORY_TABLE_NAME",
        )?)?,
        prepared,
        ensure_database_sql,
        migrate_metadata_sql,
        source_realizations,
        persist_metadata_sql,
        realizations,
        landing_views,
        stabilization_seconds: float(py, &request.getattr("stabilization_seconds")?)?,
        roots,
    });
    native_statements(py, statements)
}

/// The callable rendering one logical policy view as its shadow under the active deployment's
/// physical tables, or `None` when the project no longer declares it.
fn policy_view_renderer<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    views: &Bound<'py, PyDict>,
    target_catalog: &Value<'py>,
) -> PyResult<Value<'py>> {
    let physical_table_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for desired in items(&request.getattr("desired_state")?.getattr("objects")?)? {
        if !is_a(&desired, COMPILE_MODELS, "DesiredTable")? {
            continue;
        }
        let relation: Value<'py> =
            target_catalog.call_method1("relation", (desired.getattr("name")?,))?;
        if is_none(&relation) {
            continue;
        }
        let binding: Value<'py> = relation.getattr("stable_binding_name")?;
        if !is_none(&binding) {
            physical_table_by_key.set_item(desired.getattr("key")?, binding)?;
        }
    }
    let physical_by_view: Bound<'py, PyDict> = PyDict::new(py);
    for change in items(&request.getattr("row_error_policy_changes")?)? {
        physical_by_view.set_item(
            change.getattr("logical_view_name")?,
            change.getattr("view_name")?,
        )?;
        physical_by_view.set_item(
            change.getattr("logical_dead_letter_view_name")?,
            change.getattr("dead_letter_view_name")?,
        )?;
    }
    let views: Py<PyDict> = views.clone().unbind();
    let physical_table_by_key: Py<PyDict> = physical_table_by_key.unbind();
    let physical_by_view: Py<PyDict> = physical_by_view.unbind();
    let request: Py<PyAny> = request.clone().unbind();
    let client: Py<PyAny> = client.clone().unbind();
    let render = move |arguments: &Bound<'_, PyTuple>,
                       _keywords: Option<&Bound<'_, PyDict>>|
          -> PyResult<Py<PyAny>> {
        let py: Python<'_> = arguments.py();
        let name: Value<'_> = arguments.get_item(0)?;
        let Some(view) = views.bind(py).get_item(&name)? else {
            return Ok(py.None());
        };
        let request: &Value<'_> = request.bind(py);
        let database: Value<'_> = request.getattr("default_database")?;
        let shadow: Value<'_> = build_shadow_adapter_resource(
            py,
            &view,
            &physical_by_view.bind(py).as_any().get_item(&name)?,
            physical_table_by_key.bind(py).as_any(),
        )?;
        let resource: Value<'_> = with_metadata_dead_letter_target(
            &shadow,
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

/// Pause, view swap and resume statements for row-error policy changes on a live virtual
/// deployment, and the statements that recover from a failed swap.
fn policy_statements<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    target_catalog: &Value<'py>,
) -> PyResult<(Vec<Value<'py>>, Vec<Value<'py>>)> {
    let changes: Value<'py> = request.getattr("row_error_policy_changes")?;
    let default_database: Value<'py> = request.getattr("default_database")?;
    let views: Bound<'py, PyDict> = PyDict::new(py);
    for desired in items(&request.getattr("desired_state")?.getattr("objects")?)? {
        if is_a(&desired, COMPILE_MODELS, "DesiredMaterializedView")? {
            views.set_item(desired.getattr("name")?, desired)?;
        }
    }
    // Looked up on the module at call time, as the Python workflow's import binding was, so a
    // test can substitute it to inject a failing policy view.
    let (teardown, realizations): PolicyViewSteps = call(
        py,
        "sqltide._native",
        "row_error_policy_view_steps",
        &[
            ("changes", changes.clone()),
            ("database", default_database.clone()),
            (
                "render_view",
                policy_view_renderer(py, request, client, &views, target_catalog)?,
            ),
        ],
    )?
    .extract()?;
    let mut landing_names: Vec<String> = Vec::new();
    for change in items(&changes)? {
        for name in items(&change.getattr("landing_view_names")?)? {
            landing_names.push(name.extract()?);
        }
    }
    landing_names.sort();
    landing_names.dedup();
    let mut activations: Vec<(String, String)> = Vec::new();
    for name in &landing_names {
        let view: Value<'py> = views.as_any().get_item(name)?;
        let sql: String = method(
            client,
            "render_resource",
            &[
                ("resource", build_adapter_resource(py, &view)?),
                (
                    "database",
                    or(view.getattr("key")?.getattr("database")?, &default_database)?,
                ),
                ("if_not_exists", boolean(py, true)?),
            ],
        )?
        .extract()?;
        activations.push((name.clone(), sql));
    }
    let mut migrations: Vec<(String, String)> = Vec::new();
    for (position, statement) in items(&client.call_method1(
        "render_migrate_metadata_state",
        (request.getattr("metadata_database")?,),
    )?)?
    .iter()
    .enumerate()
    {
        migrations.push((format!("metadata_{}", position + 1), statement.extract()?));
    }
    let database: String = default_database.extract()?;
    let (statements, recovery) = direct_workflow::statements(&direct_workflow::Input {
        database: database.clone(),
        ensure_database_sql: client
            .call_method1("render_ensure_database", (&default_database,))?
            .extract()?,
        source_realizations: migrations,
        recreations: Vec::new(),
        preserved_landing_view_names: landing_names,
        bootstrapped_landing_views: Vec::new(),
        activations,
        stabilization_seconds: float(py, &request.getattr("stabilization_seconds")?)?,
        teardown,
        realizations,
        runtime_replays: Vec::new(),
        audits: Vec::new(),
        audit_sample_limit: AUDIT_SAMPLE_LIMIT,
    });
    let mut restores: Vec<Value<'py>> = items(&row_error_policy_restore_statements(
        py,
        &changes,
        &client.getattr("render_restore_materialized_view")?,
        &database,
    )?)?;
    restores.extend(native_statements(py, recovery)?);
    Ok((native_statements(py, statements)?, restores))
}

/// Resolve preconditions and assemble every virtual build statement once.
#[pyfunction]
#[pyo3(signature = (*, request, client, plan_json))]
fn assemble_virtual_build_workflow<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
    plan_json: &Value<'py>,
) -> PyResult<Value<'py>> {
    let deployment_plan: Value<'py> = confirmed_plan(py, request)?;
    let deployment_id: Value<'py> = confirmed_deployment_id(py, request)?;
    let target_catalog: Value<'py> =
        confirmed_catalog(py, request.getattr("confirmed_target_catalog")?, "target")?;
    let metadata_catalog: Value<'py> = confirmed_catalog(
        py,
        request.getattr("confirmed_metadata_catalog")?,
        "metadata",
    )?;
    let desired_state: Value<'py> = request.getattr("desired_state")?;
    let default_database: Value<'py> = request.getattr("default_database")?;
    let (source_preparation, source_realizations): (Value<'py>, Value<'py>) =
        plan_population_sources(
            py,
            &desired_state,
            &default_database,
            target_catalog.call_method0("relation_names")?.extract()?,
        )?
        .extract()?;
    let replay_lineage_mode: Value<'py> = request.getattr("replay_lineage_mode")?;
    let population_plan: Value<'py> = expand_population_plan(
        py,
        &population_plan(
            py,
            &deployment_plan,
            &deployment_id,
            &replay_lineage_mode.str()?.into_any(),
        )?,
        &desired_state,
    )?;
    let replay_templates: Vec<Value<'py>> = items(&build_population_replay_templates(
        py,
        &population_plan,
        &desired_state,
        &default_database.extract::<String>()?,
    )?)?;
    let created_at: Value<'py> = request.getattr("created_at")?;
    if is_none(&created_at) {
        return Err(backfill_error(
            py,
            "Virtual workflow assembly requires a fixed creation timestamp".to_owned(),
        ));
    }
    let fields: DeploymentFields<'py> = DeploymentFields {
        deployment_id,
        created_at,
        replay_lineage_mode: class(py, DISCOVERY_TYPES, "ReplayLineageMode")?
            .call1((replay_lineage_mode,))?,
        workflow_fingerprint: sha256_hex(&plan_json.extract::<String>()?),
        tool_version: py
            .import("importlib.metadata")?
            .call_method1("version", ("sqltide",))?,
    };
    let metadata_state: Value<'py> = deployment_metadata_state(
        py,
        &deployment_plan,
        &desired_state.getattr("objects")?,
        fields,
    )?;
    let confirmed: Confirmed<'py> = Confirmed {
        target_catalog,
        metadata_catalog,
        metadata_state,
        source_preparation,
        source_realizations,
        population_plan,
        replay_templates,
    };
    let mut statements: Vec<Value<'py>> = assemble_statements(py, request, client, &confirmed)?;
    let mut recovery: Vec<Value<'py>> = Vec::new();
    if request.getattr("row_error_policy_changes")?.is_truthy()? {
        let (policy, policy_recovery) =
            policy_statements(py, request, client, &confirmed.target_catalog)?;
        recovery = policy_recovery;
        if !deployment_plan.getattr("rebuild_subtrees")?.is_truthy()? {
            statements.clear();
        }
        statements.extend(policy);
    }
    build(
        py,
        WORKFLOW_MODELS,
        "BuildWorkflow",
        &[
            (
                "mode",
                class(py, WORKFLOW_TYPES, "WorkflowMode")?.getattr("VIRTUAL_ENVIRONMENT")?,
            ),
            ("plan_json", plan_json.clone()),
            ("statements", warehouse_statements(py, &statements)?),
            ("recovery_statements", warehouse_statements(py, &recovery)?),
        ],
    )
}

/// The preflight and preparation prefix of a virtual workflow, which candidate bootstrap runs.
#[pyfunction]
#[pyo3(signature = (*, workflow))]
fn build_virtual_bootstrap_workflow<'py>(
    py: Python<'py>,
    workflow: &Value<'py>,
) -> PyResult<Value<'py>> {
    let phases: Value<'py> = class(py, WORKFLOW_TYPES, "WorkflowPhase")?;
    let kept: Bound<'py, PyList> = PyList::empty(py);
    for phase in ["PREFLIGHT", "PREPARATION"] {
        kept.append(phases.getattr(phase)?)?;
    }
    let mut statements: Vec<Value<'py>> = Vec::new();
    for statement in items(&workflow.getattr("statements")?)? {
        if kept.contains(statement.getattr("phase")?)? {
            statements.push(statement);
        }
    }
    build(
        py,
        WORKFLOW_MODELS,
        "BuildWorkflow",
        &[
            ("mode", workflow.getattr("mode")?),
            ("plan_json", workflow.getattr("plan_json")?),
            ("statements", tuple(py, statements)?),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        build_backfill_deployment_identity,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        resolve_unsupported_bounded_replay_behavior,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(assemble_virtual_build_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(build_virtual_bootstrap_workflow, module)?)
}
