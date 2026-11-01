//! Python bindings for population replay planning.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::population::{
    BoundaryMode, BoundedOutcome, ExecutionMode, ExternalColumns, PopulationInput, ReplayPlan,
    Root, Table, View, bounded_outcome, physical_table_mappings, replay_plans, template_watermarks,
};
use sqltide_executor::population_scope;

/// `(root key, anchor key, boundary mode, execution mode, forced start time, lookback seconds)`.
type RootArgument = (usize, usize, String, String, Option<String>, Option<i64>);
/// `(key, name, database, column names, replay settings)`.
type TableArgument = (
    usize,
    String,
    Option<String>,
    Vec<String>,
    Vec<(String, String)>,
);
/// `(anchor key, [partition, offset, timestamp, landed at, cursor] column overrides)`.
type ExternalArgument = (usize, [Option<String>; 5]);
/// `(boundary key, cutoff value, partition value)`.
type BoundaryResult = (String, String, Option<String>);
/// `(root key, mode, database, (root, source, anchor, target), view index, boundaries,
/// [partition, offset, timestamp, landed at, cursor], (lower bound mode, boundary time, forced
/// start time, lookback seconds), seeds history, target column names, settings, filter boundaries
/// at source)`.
type PlanResult = (
    usize,
    &'static str,
    String,
    (String, String, String, String),
    usize,
    Vec<BoundaryResult>,
    [String; 5],
    (&'static str, String, Option<String>, Option<i64>),
    bool,
    Vec<String>,
    Vec<(String, String)>,
    bool,
);

fn boundary_mode(text: &str) -> PyResult<BoundaryMode> {
    BoundaryMode::parse(text)
        .ok_or_else(|| PyValueError::new_err(format!("Unknown replay boundary mode '{text}'")))
}

fn execution_mode(text: &str) -> PyResult<ExecutionMode> {
    Ok(match text {
        "full_rebuild" => ExecutionMode::Full,
        "seeded_bounded_rebuild" => ExecutionMode::SeededBounded,
        "unseeded_bounded_rebuild" => ExecutionMode::UnseededBounded,
        other => {
            return Err(PyValueError::new_err(format!(
                "Unknown rebuild execution mode '{other}'"
            )));
        }
    })
}

fn plan_result(plan: ReplayPlan) -> PlanResult {
    (
        plan.root,
        plan.mode.as_str(),
        plan.database,
        plan.relations,
        plan.view,
        plan.boundaries
            .into_iter()
            .map(|boundary| {
                (
                    boundary.boundary_key,
                    boundary.cutoff_value,
                    boundary.partition_value,
                )
            })
            .collect(),
        plan.columns,
        (
            plan.lower_bound_mode.as_str(),
            plan.boundary_time,
            plan.forced_start_time,
            plan.lookback_seconds,
        ),
        plan.seeds_history,
        plan.target_column_names,
        plan.settings,
        plan.filter_boundaries_at_source,
    )
}

/// `(physical table mappings, replay plans)` for population roots. Without `watermarks`, each
/// root gets one full-replay template boundary.
#[pyfunction]
#[pyo3(signature = (
    *,
    roots,
    tables,
    views,
    key_names,
    physical_names,
    external_columns,
    replay_anchor_keys,
    default_database,
    boundary_time,
    watermarks,
))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn plan_population_replays(
    roots: Vec<RootArgument>,
    tables: Vec<TableArgument>,
    views: Vec<(String, String)>,
    key_names: Vec<String>,
    physical_names: Vec<(usize, String)>,
    external_columns: Vec<ExternalArgument>,
    replay_anchor_keys: HashSet<usize>,
    default_database: &str,
    boundary_time: &str,
    watermarks: Option<Vec<(usize, String, String)>>,
) -> PyResult<(Vec<(String, String)>, Vec<PlanResult>)> {
    let count = key_names.len();
    let in_range = |key: &usize| *key < count;
    if !roots
        .iter()
        .all(|(key, anchor, ..)| in_range(key) && in_range(anchor))
        || !tables.iter().all(|(key, ..)| in_range(key))
        || !physical_names.iter().all(|(key, _)| in_range(key))
        || !external_columns.iter().all(|(key, _)| in_range(key))
    {
        return Err(PyValueError::new_err("Population key index out of range"));
    }
    let roots: Vec<Root> = roots
        .into_iter()
        .map(
            |(key, anchor, mode, execution, forced_start_time, lookback_seconds)| {
                Ok(Root {
                    key,
                    anchor,
                    mode: boundary_mode(&mode)?,
                    execution_mode: execution_mode(&execution)?,
                    forced_start_time,
                    lookback_seconds,
                })
            },
        )
        .collect::<PyResult<_>>()?;
    let tables: Vec<Table> = tables
        .into_iter()
        .map(
            |(key, name, database, column_names, replay_settings)| Table {
                key,
                name,
                database,
                column_names,
                replay_settings,
            },
        )
        .collect();
    let views: Vec<View> = views
        .into_iter()
        .map(|(target, source)| View { target, source })
        .collect();
    let external_columns: Vec<(usize, ExternalColumns)> = external_columns
        .into_iter()
        .map(|(key, [partition, offset, timestamp, landed_at, cursor])| {
            (
                key,
                ExternalColumns {
                    partition,
                    offset,
                    timestamp,
                    landed_at,
                    cursor,
                },
            )
        })
        .collect();
    let input = PopulationInput {
        roots: &roots,
        tables: &tables,
        views: &views,
        key_names: &key_names,
        physical_names: &physical_names,
        external_columns: &external_columns,
        replay_anchor_keys: &replay_anchor_keys,
        default_database,
        boundary_time,
    };
    let watermarks = watermarks.unwrap_or_else(|| template_watermarks(&roots));
    let plans = replay_plans(&input, &watermarks).map_err(PyValueError::new_err)?;
    Ok((
        physical_table_mappings(&input),
        plans.into_iter().map(plan_result).collect(),
    ))
}

/// `"keep"`, `"without_history"`, or `"full"` for a seeded bounded rebuild.
#[pyfunction]
#[pyo3(signature = (mode, forced_start_time, live_columns, fallback_without_history))]
fn bounded_replay_outcome(
    mode: Option<&str>,
    forced_start_time: bool,
    live_columns: Option<HashSet<String>>,
    fallback_without_history: bool,
) -> PyResult<&'static str> {
    let mode = mode.map(boundary_mode).transpose()?;
    Ok(
        match bounded_outcome(
            mode,
            forced_start_time,
            live_columns.as_ref(),
            fallback_without_history,
        ) {
            BoundedOutcome::Keep => "keep",
            BoundedOutcome::WithoutHistory => "without_history",
            BoundedOutcome::Full => "full",
        },
    )
}

fn scope_error(error: population_scope::ScopeError) -> PyErr {
    match error {
        population_scope::ScopeError::MissingKey(key) => PyValueError::new_err(("key", key)),
        population_scope::ScopeError::MissingName(name) => PyValueError::new_err(("name", name)),
        population_scope::ScopeError::Exhausted => PyValueError::new_err(("exhausted",)),
    }
}

/// Planned keys in creation order, from `(key, dependency keys)` in desired-state order.
/// Raises `ValueError(("key", key))` for a planned dependency without a desired object.
#[pyfunction]
fn population_creation_order(
    objects: Vec<(usize, Vec<usize>)>,
    planned: HashSet<usize>,
) -> PyResult<Vec<usize>> {
    population_scope::creation_order(&objects, &planned).map_err(scope_error)
}

/// `(owner root position, root key, (target key, view key), upstream boundary key)`.
type FanInResult = (usize, usize, (usize, usize), usize);

/// The [`FanInResult`] of each
/// fan-in catch-up root, from `(root key, affected keys)` roots, `(name, key)` desired tables,
/// and `(key, source name, target name, dependency keys)` materialized views.
#[pyfunction]
fn population_fan_in_roots(
    planned: HashSet<usize>,
    roots: Vec<(usize, Vec<usize>)>,
    tables: Vec<(String, usize)>,
    views: Vec<(usize, String, String, Vec<usize>)>,
) -> PyResult<Vec<FanInResult>> {
    let views: Vec<population_scope::MaterializedView> = views
        .into_iter()
        .map(|(key, source_table_name, target_table_name, deps)| {
            population_scope::MaterializedView {
                key,
                source_table_name,
                target_table_name,
                deps,
            }
        })
        .collect();
    let added =
        population_scope::fan_in_roots(&planned, &roots, &tables, &views).map_err(scope_error)?;
    Ok(added
        .into_iter()
        .map(|root| {
            (
                root.owner,
                root.root_key,
                (root.affected_keys[0], root.affected_keys[1]),
                root.upstream_boundary_key,
            )
        })
        .collect())
}

/// `skip`, `preserve`, `create`, or `land` for each `(kind, name, landing target)` object,
/// where kind is `kafka`, `table`, `materialized_view`, or `view`.
#[pyfunction]
fn population_source_actions(
    objects: Vec<(String, String, Option<String>)>,
    raw_prefix: &str,
    existing_names: HashSet<String>,
) -> PyResult<Vec<&'static str>> {
    let objects: Vec<population_scope::SourceObject> = objects
        .into_iter()
        .map(|(kind, name, target_table_name)| {
            Ok(match (kind.as_str(), target_table_name) {
                ("kafka", _) => population_scope::SourceObject::Kafka { name },
                ("table", _) => population_scope::SourceObject::Table { name },
                ("materialized_view", Some(target_table_name)) => {
                    population_scope::SourceObject::MaterializedView {
                        name,
                        target_table_name,
                    }
                }
                ("view", _) => population_scope::SourceObject::View,
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown source object kind '{kind}'"
                    )));
                }
            })
        })
        .collect::<PyResult<_>>()?;
    Ok(
        population_scope::source_actions(&objects, raw_prefix, &existing_names)
            .into_iter()
            .map(|action| match action {
                population_scope::SourceAction::Skip => "skip",
                population_scope::SourceAction::Preserve => "preserve",
                population_scope::SourceAction::Create => "create",
                population_scope::SourceAction::Land => "land",
            })
            .collect(),
    )
}

/// `(model index, propagated model indexes, driving input relation name)`.
type DirectRootArgument = (usize, Vec<usize>, String);
/// `(planned keys, ((root key, affected keys, upstream boundary key), ...))`.
type DirectPopulationResult = (Vec<usize>, Vec<(usize, Vec<usize>, usize)>);

/// A direct closure's planned keys and roots. Resources are `(kind, name)` with kind
/// `table`, `materialized_view`, `view`, or anything else to ignore.
#[pyfunction]
fn direct_population_scope(
    desired: Vec<(String, usize)>,
    resources: Vec<Vec<(String, String)>>,
    execution_scope: Vec<usize>,
    roots: Vec<DirectRootArgument>,
    external_tables: Vec<(String, usize)>,
) -> PyResult<DirectPopulationResult> {
    let resources: Vec<Vec<(population_scope::ResourceKind, String)>> = resources
        .into_iter()
        .map(|model| {
            model
                .into_iter()
                .map(|(kind, name)| {
                    let kind = match kind.as_str() {
                        "table" => population_scope::ResourceKind::Table,
                        "materialized_view" => population_scope::ResourceKind::MaterializedView,
                        "view" => population_scope::ResourceKind::View,
                        _ => population_scope::ResourceKind::Other,
                    };
                    (kind, name)
                })
                .collect()
        })
        .collect();
    let roots: Vec<population_scope::DirectRoot> = roots
        .into_iter()
        .map(|(model, propagated_models, driving_input_relation_name)| {
            population_scope::DirectRoot {
                model,
                propagated_models,
                driving_input_relation_name,
            }
        })
        .collect();
    let population = population_scope::direct_population(
        &desired,
        &resources,
        &execution_scope,
        &roots,
        &external_tables,
    )
    .map_err(scope_error)?;
    Ok((population.planned_keys, population.roots))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(population_creation_order, module)?)?;
    module.add_function(wrap_pyfunction!(population_fan_in_roots, module)?)?;
    module.add_function(wrap_pyfunction!(population_source_actions, module)?)?;
    module.add_function(wrap_pyfunction!(direct_population_scope, module)?)?;
    module.add_function(wrap_pyfunction!(plan_population_replays, module)?)?;
    module.add_function(wrap_pyfunction!(bounded_replay_outcome, module)?)
}
