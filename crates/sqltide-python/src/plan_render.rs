//! Python bindings for rendering one virtual deployment plan for `sqt plan`.

use pyo3::prelude::*;
use sqltide_planner::cli_style::CliStyle;
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
        warnings: items(plan, "warnings", |warning| {
            Ok(Warning {
                warning_code: warning.getattr("warning_code")?.extract()?,
                message: warning.getattr("message")?.extract()?,
                root_key: key(&warning.getattr("root_key")?)?,
                target_key: optional_key(&warning.getattr("target_key")?)?,
            })
        })?,
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

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_plan_text, module)?)?;
    module.add_function(wrap_pyfunction!(render_plan_json, module)?)
}
