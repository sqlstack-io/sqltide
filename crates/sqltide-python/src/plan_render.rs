//! Python bindings for rendering virtual deployment plans and direct plans.

use pyo3::prelude::*;
use sqltide_planner::cli_style::CliStyle;
use sqltide_planner::direct_render::{
    DirectPlan, Entry, LogicalKey, Prerequisite, RelationOperation, ReplayColumns, ReplayRoot,
    RowErrorPolicyChange, SourceDriftSelection, SourceRecreation, SqlChange,
};
use sqltide_planner::plan_render::{
    Change, Desired, DesiredObjects, Key, Plan, ShadowObject, SqlDiff, Step, Subtree, Warning,
    render_plan_json as render_json, render_plan_text as render_text,
};

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(value.str()?.to_string())
}

fn optional_text(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        Ok(None)
    } else {
        text(value).map(Some)
    }
}

fn field(object: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    text(&object.getattr(name)?)
}

fn optional_field(object: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<String>> {
    optional_text(&object.getattr(name)?)
}

fn key(value: &Bound<'_, PyAny>) -> PyResult<Key> {
    Ok(Key {
        database: value.getattr("database")?.extract()?,
        object_type: field(value, "object_type")?,
        name: value.getattr("name")?.extract()?,
    })
}

fn optional_key(value: &Bound<'_, PyAny>) -> PyResult<Option<Key>> {
    if value.is_none() {
        Ok(None)
    } else {
        key(value).map(Some)
    }
}

fn items<T>(
    object: &Bound<'_, PyAny>,
    name: &str,
    convert: impl Fn(&Bound<'_, PyAny>) -> PyResult<T>,
) -> PyResult<Vec<T>> {
    object
        .getattr(name)?
        .try_iter()?
        .map(|item| convert(&item?))
        .collect()
}

fn warning(warning: &Bound<'_, PyAny>) -> PyResult<Warning> {
    Ok(Warning {
        warning_code: warning.getattr("warning_code")?.extract()?,
        message: warning.getattr("message")?.extract()?,
        root_key: key(&warning.getattr("root_key")?)?,
        target_key: optional_key(&warning.getattr("target_key")?)?,
    })
}

fn plan(plan: &Bound<'_, PyAny>) -> PyResult<Plan> {
    Ok(Plan {
        deployment_id: optional_field(plan, "deployment_id")?,
        object_changes: items(plan, "object_changes", |change| {
            Ok(Change {
                key: key(&change.getattr("key")?)?,
                change_type: field(change, "change_type")?,
                force_full_refresh: change.getattr("force_full_refresh")?.extract()?,
                forced_start_time: optional_field(change, "forced_start_time")?,
                schema_change_kind: optional_field(change, "schema_change_kind")?,
                seed_compatibility: optional_field(change, "seed_compatibility")?,
            })
        })?,
        rebuild_subtrees: items(plan, "rebuild_subtrees", |subtree| {
            Ok(Subtree {
                root_key: key(&subtree.getattr("root_key")?)?,
                affected_keys: items(subtree, "affected_keys", key)?,
                upstream_boundary_key: key(&subtree.getattr("upstream_boundary_key")?)?,
                strategy: field(subtree, "strategy")?,
                replay_required: subtree.getattr("replay_required")?.extract()?,
                execution_mode: field(subtree, "execution_mode")?,
                forced_full_refresh: subtree.getattr("forced_full_refresh")?.extract()?,
                forced_start_time: optional_field(subtree, "forced_start_time")?,
                requested_start_time: optional_field(subtree, "requested_start_time")?,
                configured_backfill_mode: optional_field(subtree, "configured_backfill_mode")?,
                execution_lookback_seconds: subtree
                    .getattr("execution_lookback_seconds")?
                    .extract()?,
                history_preserving_bounded_supported: subtree
                    .getattr("history_preserving_bounded_supported")?
                    .extract()?,
                resolved_bounded_replay_fallback: optional_field(
                    subtree,
                    "resolved_bounded_replay_fallback",
                )?,
            })
        })?,
        steps: items(plan, "steps", |step| {
            Ok(Step {
                step_id: step.getattr("step_id")?.extract()?,
                phase: field(step, "phase")?,
                action: field(step, "action")?,
                root_key: key(&step.getattr("root_key")?)?,
                target_key: optional_key(&step.getattr("target_key")?)?,
                physical_name: step.getattr("physical_name")?.extract()?,
            })
        })?,
        prepared_shadow_objects: items(plan, "prepared_shadow_objects", |prepared| {
            Ok(ShadowObject {
                logical_key: key(&prepared.getattr("logical_key")?)?,
                physical_name: prepared.getattr("physical_name")?.extract()?,
                logical_model_name: prepared.getattr("logical_model_name")?.extract()?,
            })
        })?,
        warnings: items(plan, "warnings", warning)?,
        sql_diffs: items(plan, "sql_diffs", |sql_diff| {
            Ok(SqlDiff {
                key: key(&sql_diff.getattr("key")?)?,
                object_type: sql_diff.getattr("object_type")?.extract()?,
                name: sql_diff.getattr("name")?.extract()?,
                diff_lines: sql_diff.getattr("diff_lines")?.extract()?,
            })
        })?,
    })
}

/// The desired tables, materialized views and views plan text reads.
fn desired_objects(py: Python<'_>, desired_state: &Bound<'_, PyAny>) -> PyResult<DesiredObjects> {
    let models: Bound<'_, PyModule> = py.import(COMPILE_MODELS)?;
    let table = models.getattr("DesiredTable")?;
    let materialized_view = models.getattr("DesiredMaterializedView")?;
    let view = models.getattr("DesiredView")?;
    let mut objects: Vec<(Key, Desired)> = Vec::new();
    for object in desired_state.getattr("objects")?.try_iter()? {
        let object: Bound<'_, PyAny> = object?;
        let desired: Desired = if object.is_instance(&table)? {
            Desired::Table
        } else if object.is_instance(&materialized_view)? {
            Desired::MaterializedView {
                target_table_name: object.getattr("target_table_name")?.extract()?,
            }
        } else if object.is_instance(&view)? {
            Desired::View
        } else {
            continue;
        };
        objects.push((key(&object.getattr("key")?)?, desired));
    }
    Ok(DesiredObjects::new(objects))
}

/// The human `sqt plan` text for `plan`, coloured when `use_color` is set.
#[pyfunction]
#[pyo3(signature = (*, plan, desired_state, database, verbose, use_color))]
fn render_plan_text(
    py: Python<'_>,
    plan: &Bound<'_, PyAny>,
    desired_state: &Bound<'_, PyAny>,
    database: &str,
    verbose: bool,
    use_color: bool,
) -> PyResult<String> {
    Ok(render_text(
        &self::plan(plan)?,
        &desired_objects(py, desired_state)?,
        database,
        verbose,
        CliStyle { use_color },
    ))
}

/// The `sqt plan --json` document for `plan`, with a trailing newline.
#[pyfunction]
#[pyo3(signature = (*, plan, adapter_name))]
fn render_plan_json(plan: &Bound<'_, PyAny>, adapter_name: &str) -> PyResult<String> {
    Ok(render_json(&self::plan(plan)?, adapter_name))
}

fn logical_key(value: &Bound<'_, PyAny>) -> PyResult<LogicalKey> {
    Ok(LogicalKey {
        resource_type: field(value, "resource_type")?,
        name: value.getattr("name")?.extract()?,
    })
}

fn optional_logical_key(value: &Bound<'_, PyAny>) -> PyResult<Option<LogicalKey>> {
    if value.is_none() {
        Ok(None)
    } else {
        logical_key(value).map(Some)
    }
}

fn logical_keys(object: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<LogicalKey>> {
    items(object, name, logical_key)
}

fn texts(object: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<String>> {
    items(object, name, text)
}

fn relation_operation(operation: &Bound<'_, PyAny>) -> PyResult<RelationOperation> {
    Ok(RelationOperation {
        relation_name: operation.getattr("relation_name")?.extract()?,
        action: field(operation, "action")?,
        model_key: logical_key(&operation.getattr("model_key")?)?,
        resource_kind: field(operation, "resource_kind")?,
    })
}

fn sql_change(change: &Bound<'_, PyAny>) -> PyResult<SqlChange> {
    Ok(SqlChange {
        status: field(change, "status")?,
        current_sql: change.getattr("current_sql")?.extract()?,
        current_hash: change.getattr("current_hash")?.extract()?,
        previous_sql: change.getattr("previous_sql")?.extract()?,
        previous_hash: change.getattr("previous_hash")?.extract()?,
        unified_diff: change.getattr("unified_diff")?.extract()?,
        warning: change.getattr("warning")?.extract()?,
    })
}

fn direct_plan(plan: &Bound<'_, PyAny>) -> PyResult<DirectPlan> {
    Ok(DirectPlan {
        database: plan.getattr("database")?.extract()?,
        user_scope: logical_keys(plan, "user_scope")?,
        execution_scope: logical_keys(plan, "execution_scope")?,
        prerequisite_scope: items(plan, "prerequisite_scope", |prerequisite| {
            Ok(Prerequisite {
                key: logical_key(&prerequisite.getattr("key")?)?,
                relation_names: texts(prerequisite, "relation_names")?,
                present: prerequisite.getattr("present")?.extract()?,
                framework_managed: prerequisite.getattr("framework_managed")?.extract()?,
            })
        })?,
        entries: items(plan, "entries", |entry| {
            let change: Bound<'_, PyAny> = entry.getattr("sql_change")?;
            Ok(Entry {
                model_key: logical_key(&entry.getattr("model_key")?)?,
                reason: field(entry, "reason")?,
                relation_names: texts(entry, "relation_names")?,
                resource_kinds: texts(entry, "resource_kinds")?,
                driving_input_key: optional_logical_key(&entry.getattr("driving_input_key")?)?,
                is_replay_root: entry.getattr("is_replay_root")?.extract()?,
                sql_change: if change.is_none() {
                    None
                } else {
                    Some(sql_change(&change)?)
                },
            })
        })?,
        replay_roots: items(plan, "replay_roots", |root| {
            let columns: Bound<'_, PyAny> = root.getattr("driving_input_replay_columns")?;
            Ok(ReplayRoot {
                model_key: logical_key(&root.getattr("model_key")?)?,
                driving_input_key: logical_key(&root.getattr("driving_input_key")?)?,
                driving_input_relation_name: root
                    .getattr("driving_input_relation_name")?
                    .extract()?,
                driving_input_replay_columns: ReplayColumns {
                    partition: columns.getattr("partition")?.extract()?,
                    offset: columns.getattr("offset")?.extract()?,
                    timestamp: columns.getattr("timestamp")?.extract()?,
                    landed_at: columns.getattr("landed_at")?.extract()?,
                    cursor: columns.getattr("cursor")?.extract()?,
                },
                replay_boundary_mode: field(root, "replay_boundary_mode")?,
                propagated_model_keys: logical_keys(root, "propagated_model_keys")?,
                has_aggregate_semantics: root.getattr("has_aggregate_semantics")?.extract()?,
                replay_settings: root.getattr("replay_settings")?.extract()?,
            })
        })?,
        teardown_operations: items(plan, "teardown_operations", relation_operation)?,
        creation_operations: items(plan, "creation_operations", relation_operation)?,
        selection_mode: field(plan, "selection_mode")?,
        prerequisite_execution_scope: logical_keys(plan, "prerequisite_execution_scope")?,
        warnings: items(plan, "warnings", warning)?,
        effective_start_time: plan.getattr("effective_start_time")?.extract()?,
        source_drift_selections: items(plan, "source_drift_selections", |selection| {
            Ok(SourceDriftSelection {
                source_key: logical_key(&selection.getattr("source_key")?)?,
                pipeline_name: selection.getattr("pipeline_name")?.extract()?,
                reader_pipeline_names: texts(selection, "reader_pipeline_names")?,
                drifted_setting_names: texts(selection, "drifted_setting_names")?,
                model_keys: logical_keys(selection, "model_keys")?,
            })
        })?,
    })
}

fn source_recreation(recreation: &Bound<'_, PyAny>) -> PyResult<SourceRecreation> {
    Ok(SourceRecreation {
        relation_name: recreation
            .getattr("managed_source")?
            .getattr("resource")?
            .getattr("name")?
            .extract()?,
        landing_view_name: recreation.getattr("landing_view_name")?.extract()?,
        landing_relation_name: recreation.getattr("landing_relation_name")?.extract()?,
        consumer_group: recreation.getattr("consumer_group")?.extract()?,
        drifted_setting_names: texts(recreation, "drifted_setting_names")?,
    })
}

fn row_error_policy_change(change: &Bound<'_, PyAny>) -> PyResult<RowErrorPolicyChange> {
    Ok(RowErrorPolicyChange {
        model_key: logical_key(&change.getattr("model_key")?)?,
        view_name: change.getattr("view_name")?.extract()?,
        dead_letter_view_name: change.getattr("dead_letter_view_name")?.extract()?,
        desired_policy: field(change, "desired_policy")?,
        landing_view_names: texts(change, "landing_view_names")?,
        landing_repair_only: change.getattr("landing_repair_only")?.extract()?,
    })
}

fn each<T>(
    values: &Bound<'_, PyAny>,
    convert: impl Fn(&Bound<'_, PyAny>) -> PyResult<T>,
) -> PyResult<Vec<T>> {
    values.try_iter()?.map(|item| convert(&item?)).collect()
}

/// The operator-facing text of one direct plan, coloured when `use_color` is set.
#[pyfunction]
#[pyo3(signature = (
    *, plan, adapter_name, use_color, verbose=false, source_recreations=None,
    row_error_policy_changes=None,
))]
fn render_direct_plan_text(
    plan: &Bound<'_, PyAny>,
    adapter_name: &str,
    use_color: bool,
    verbose: bool,
    source_recreations: Option<&Bound<'_, PyAny>>,
    row_error_policy_changes: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    Ok(sqltide_planner::direct_render::render_direct_plan_text(
        &direct_plan(plan)?,
        adapter_name,
        verbose,
        &source_recreations.map_or(Ok(Vec::new()), |values| each(values, source_recreation))?,
        &row_error_policy_changes.map_or(Ok(Vec::new()), |values| {
            each(values, row_error_policy_change)
        })?,
        CliStyle { use_color },
    ))
}

/// The direct plan as deterministic JSON, with a trailing newline.
#[pyfunction]
#[pyo3(signature = (*, plan, adapter_name, source_recreations=None, row_error_policy_changes=None))]
fn render_direct_plan_json(
    plan: &Bound<'_, PyAny>,
    adapter_name: &str,
    source_recreations: Option<&Bound<'_, PyAny>>,
    row_error_policy_changes: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    Ok(sqltide_planner::direct_render::render_direct_plan_json(
        &direct_plan(plan)?,
        adapter_name,
        &source_recreations.map_or(Ok(Vec::new()), |values| each(values, source_recreation))?,
        &row_error_policy_changes.map_or(Ok(Vec::new()), |values| {
            each(values, row_error_policy_change)
        })?,
    ))
}

/// The single plan line describing one managed source recreation.
#[pyfunction]
fn render_source_recreation_notice(recreation: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(
        sqltide_planner::direct_render::render_source_recreation_notice(&source_recreation(
            recreation,
        )?),
    )
}

/// The plan line for one in-place row-error policy change.
#[pyfunction]
fn render_row_error_policy_line(change: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(
        sqltide_planner::direct_render::render_row_error_policy_line(&row_error_policy_change(
            change,
        )?),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_plan_text, module)?)?;
    module.add_function(wrap_pyfunction!(render_plan_json, module)?)?;
    module.add_function(wrap_pyfunction!(render_direct_plan_text, module)?)?;
    module.add_function(wrap_pyfunction!(render_direct_plan_json, module)?)?;
    module.add_function(wrap_pyfunction!(render_source_recreation_notice, module)?)?;
    module.add_function(wrap_pyfunction!(render_row_error_policy_line, module)?)
}
