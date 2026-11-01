//! Pure executor decisions: readiness audits, rollback targets, janitor retention, and
//! destructive ordering.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::deployment_diff::{
    RelationPair, Side, count_sql, identifier_error, parse_comparison, relation_pairs, status,
};
use sqltide_executor::inventory::{Evidence, Record, deployment_summaries};
use sqltide_executor::janitor::{
    Decision, Deployment, Mapping, Publication as RetainedPublication, Retention, binding_activity,
    preview,
};
use sqltide_executor::ordering::{OrderingError, reverse_topological_order};
use sqltide_executor::promotion::{
    Preview, Staged, inspected_candidates, obsolete_removals, publish_bindings, replacement_preview,
};
use sqltide_executor::publications::{
    Binding, Publication, Rank, RollbackError, RollbackTarget, select_rollback,
};
use sqltide_executor::readiness::{Audit, Observation, OffsetSummary, Thresholds, audit};
use sqltide_executor::reconcile::{Key, Object, Outcome, Table, Transform, classify_targets};
use sqltide_planner::diff::Column;
use sqltide_planner::naming::{is_deployment_id, is_deployment_physical_name};

/// `(database, publishes, logical name, physical name, logical model name)`.
type MappingArgument = (Option<String>, bool, String, String, String);

/// `(deployment ID, created at, incomplete, mappings)`.
type DeploymentArgument = (String, String, bool, Vec<MappingArgument>);

/// `(deployment ID, (rank microseconds, rank ID), published at microseconds, logical view
/// names, bindings)`.
type RetainedPublicationArgument = (String, (i64, String), i64, Vec<String>, Vec<Binding>);

/// `(deployment ID, (rank microseconds, rank ID) | None, bindings)`.
type PublicationArgument = (String, Option<(i64, String)>, Vec<Binding>);

/// `(database, retention days, cutoff microseconds, minimum rollback deployments)`.
type RetentionArgument = (String, i64, i64, usize);

/// `(active partitions, partitions compared, missing staged partitions, missing freshness
/// partitions, has lag boundary column, max lag seconds)`.
type OffsetSummaryArgument = (i64, i64, i64, i64, bool, Option<f64>);

/// `(root name, staged exists, active exists, active rows, staged rows, offset lineage, offset
/// summary, scalar lag seconds, replay source name, replay source rows)`.
type ObservationArgument = (
    String,
    bool,
    bool,
    Option<i64>,
    Option<i64>,
    bool,
    Option<OffsetSummaryArgument>,
    Option<f64>,
    Option<String>,
    Option<i64>,
);

/// Audit one staged root under `(maximum lag seconds, minimum staged row ratio)`, returning
/// `(assessment, row delta, row ratio, warnings)`.
#[pyfunction]
fn audit_readiness(
    observation: ObservationArgument,
    thresholds: (f64, f64),
) -> (&'static str, Option<i64>, Option<f64>, Vec<String>) {
    let (
        root_name,
        staged_exists,
        active_exists,
        active_row_count,
        staged_row_count,
        offset_lineage,
        offset_summary,
        scalar_lag_seconds,
        replay_source_name,
        replay_source_row_count,
    ) = observation;
    let Audit {
        assessment,
        row_delta,
        row_ratio,
        warnings,
    } = audit(
        &Observation {
            root_name,
            staged_exists,
            active_exists,
            active_row_count,
            staged_row_count,
            offset_lineage,
            offset_summary: offset_summary.map(
                |(
                    active_partition_count,
                    partitions_compared,
                    missing_staged_partition_count,
                    missing_freshness_partition_count,
                    has_lag_boundary_column,
                    max_lag_seconds,
                )| OffsetSummary {
                    active_partition_count,
                    partitions_compared,
                    missing_staged_partition_count,
                    missing_freshness_partition_count,
                    has_lag_boundary_column,
                    max_lag_seconds,
                },
            ),
            scalar_lag_seconds,
            replay_source_name,
            replay_source_row_count,
        },
        &Thresholds {
            maximum_lag_seconds: thresholds.0,
            minimum_staged_row_ratio: thresholds.1,
        },
    );
    (assessment.as_str(), row_delta, row_ratio, warnings)
}

/// Positions of `(name, dependency names)` relations with dependants first. Raises
/// `ValueError(("duplicate", position))` or `ValueError(("cycle", names))`.
#[pyfunction]
fn order_relations_for_drop(relations: Vec<(String, Vec<String>)>) -> PyResult<Vec<usize>> {
    reverse_topological_order(&relations).map_err(|error| match error {
        OrderingError::Duplicate(position) => PyValueError::new_err(("duplicate", position)),
        OrderingError::Cycle(names) => PyValueError::new_err(("cycle", names)),
    })
}

/// `(current, target)` positions of the active publication and the one a rollback restores:
/// the previous publication, or the latest of `deployment_id`. Raises
/// `ValueError(("refused", message))` or `ValueError(("unranked", position))`.
#[pyfunction]
#[pyo3(signature = (publications, active, deployment_id))]
fn select_rollback_publications(
    publications: Vec<PublicationArgument>,
    active: Vec<Binding>,
    deployment_id: Option<String>,
) -> PyResult<(usize, usize)> {
    let publications: Vec<Publication> = publications
        .into_iter()
        .map(|(deployment_id, rank, bindings)| Publication {
            deployment_id,
            rank: rank.map(|(micros, id)| Rank { micros, id }),
            bindings,
        })
        .collect();
    let target: RollbackTarget<'_> = deployment_id
        .as_deref()
        .map_or(RollbackTarget::Previous, RollbackTarget::Deployment);
    select_rollback(&publications, &active, target).map_err(|error| match error {
        RollbackError::Refused(message) => PyValueError::new_err(("refused", message)),
        RollbackError::Unranked(position) => PyValueError::new_err(("unranked", position)),
    })
}

fn mapping(argument: MappingArgument) -> Mapping {
    let (database, publishes, logical_name, physical_name, logical_model_name) = argument;
    Mapping {
        database,
        publishes,
        logical_name,
        physical_name,
        logical_model_name,
    }
}

fn deployments(arguments: Vec<DeploymentArgument>) -> Vec<Deployment> {
    arguments
        .into_iter()
        .map(
            |(deployment_id, created_at, incomplete, mappings)| Deployment {
                deployment_id,
                created_at,
                incomplete,
                mappings: mappings.into_iter().map(mapping).collect(),
            },
        )
        .collect()
}

fn retained_publications(arguments: Vec<RetainedPublicationArgument>) -> Vec<RetainedPublication> {
    arguments
        .into_iter()
        .map(
            |(deployment_id, (micros, id), published_at, logical_view_names, bindings)| {
                RetainedPublication {
                    deployment_id,
                    rank: Rank { micros, id },
                    published_at,
                    logical_view_names,
                    bindings,
                }
            },
        )
        .collect()
}

/// The physical names active bindings protect, and the superseded `(database, logical name)`
/// bindings to remove.
#[pyfunction]
fn janitor_binding_activity(
    deployments_argument: Vec<DeploymentArgument>,
    publications: Vec<RetainedPublicationArgument>,
    active: Vec<Binding>,
) -> (HashSet<String>, Vec<(String, String)>) {
    binding_activity(
        &deployments(deployments_argument),
        &retained_publications(publications),
        &active,
    )
}

/// `(deployment position, deletable, reason)` for every deployment, newest created first.
#[pyfunction]
fn janitor_preview(
    deployments_argument: Vec<DeploymentArgument>,
    publications: Vec<RetainedPublicationArgument>,
    active: Vec<Binding>,
    physical_relations: HashSet<(String, String)>,
    retention: RetentionArgument,
) -> Vec<(usize, bool, String)> {
    let (database, retention_days, cutoff, minimum_rollback_deployments) = retention;
    preview(
        &deployments(deployments_argument),
        &retained_publications(publications),
        &active,
        &physical_relations,
        &Retention {
            database,
            retention_days,
            cutoff,
            minimum_rollback_deployments,
        },
    )
    .into_iter()
    .map(
        |Decision {
             deployment,
             deletable,
             reason,
         }| (deployment, deletable, reason),
    )
    .collect()
}

/// Whether `text` is a `YYYYMMDDTHHMMSSZ_<alphanumeric suffix>` deployment ID.
#[pyfunction(name = "is_deployment_id")]
fn py_is_deployment_id(text: &str) -> bool {
    is_deployment_id(text)
}

/// Whether `physical_name` is a logical name suffixed with `__<deployment ID>`.
#[pyfunction(name = "is_deployment_physical_name")]
fn py_is_deployment_physical_name(physical_name: &str) -> bool {
    is_deployment_physical_name(physical_name)
}

/// Positions of the inspected `(physical name, object type)` candidates staged by
/// `deployment_id` that a publish binds. Raises `ValueError(message)` when there are none.
#[pyfunction]
fn inspected_publish_candidates(
    candidates: Vec<(String, String)>,
    deployment_id: &str,
) -> PyResult<Vec<usize>> {
    inspected_candidates(&candidates, deployment_id).map_err(PyValueError::new_err)
}

/// The bindings publishing `deployment_id` writes from staged `(database, object type, logical
/// name, physical name)` relations. Raises `ValueError(message)` when a relation is missing or
/// nothing can be bound.
#[pyfunction]
fn publish_stable_bindings(
    relations: Vec<(Option<String>, String, String, String)>,
    existing: HashSet<String>,
    default_database: &str,
    deployment_id: &str,
) -> PyResult<Vec<Binding>> {
    let relations: Vec<Staged> = relations
        .into_iter()
        .map(
            |(database, object_type, logical_name, physical_name)| Staged {
                database,
                object_type,
                logical_name,
                physical_name,
            },
        )
        .collect();
    publish_bindings(&relations, &existing, default_database, deployment_id)
        .map_err(PyValueError::new_err)
}

/// The sorted `(database, logical name)` stale bindings a publish of `current` removes.
#[pyfunction]
fn obsolete_binding_removals(
    current: Vec<MappingArgument>,
    deployments_argument: Vec<DeploymentArgument>,
    publications: Vec<(String, Vec<String>)>,
    active: Vec<Binding>,
    default_database: &str,
) -> Vec<(String, String)> {
    let current: Vec<Mapping> = current.into_iter().map(mapping).collect();
    obsolete_removals(
        &current,
        &deployments(deployments_argument),
        &publications,
        &active,
        default_database,
    )
}

/// `(initial publish, additions, (binding, replaced physical name), (removal, unbound physical
/// name), orphaned (database, physical name))`, by request position.
type PreviewResult = (
    bool,
    Vec<usize>,
    Vec<(usize, String)>,
    Vec<(usize, String)>,
    Vec<(String, String)>,
);

/// The live effects of replacing `active` bindings with `bindings` and `removals`.
#[pyfunction]
fn binding_replacement_preview(
    bindings: Vec<Binding>,
    removals: Vec<(String, String)>,
    active: Vec<Binding>,
) -> PreviewResult {
    let Preview {
        initial_publish,
        additions,
        replacements,
        removals,
        orphaned,
    } = replacement_preview(&bindings, &removals, &active);
    (initial_publish, additions, replacements, removals, orphaned)
}

/// `(engine, order by, partition by, columns as (name, type, default))`.
type TableArgument = (
    String,
    Vec<String>,
    Option<String>,
    Vec<(String, String, Option<String>)>,
);

/// `(object type, key, table, (transform source, transform target))`.
type ReconcileObjectArgument = (String, Key, Option<TableArgument>, Option<(String, String)>);

/// `(key, name, (table position, transform position) when eligible, rejection reasons)`.
type ReconcileOutcomeResult = (
    Key,
    String,
    Option<(usize, Option<usize>)>,
    Vec<&'static str>,
);

fn reconcile_object(argument: ReconcileObjectArgument) -> Object {
    match argument {
        (kind, key, Some((engine, order_by, partition_by, columns)), _) if kind == "table" => {
            Object::Table(
                key,
                Table {
                    engine,
                    order_by,
                    partition_by,
                    columns: columns
                        .into_iter()
                        .map(|(name, type_sql, default)| Column {
                            name,
                            type_sql,
                            default,
                        })
                        .collect(),
                },
            )
        }
        (kind, key, _, Some((source, target))) if kind == "materialized_view" => {
            Object::MaterializedView(key, Transform { source, target })
        }
        (kind, key, _, _) if kind == "view" => Object::View(key),
        _ => Object::Other,
    }
}

/// A reconcile verdict per target. A type that cannot be normalized raises
/// `ValueError(message)`.
#[pyfunction]
fn classify_reconcile_targets(
    py: Python<'_>,
    desired: Vec<ReconcileObjectArgument>,
    actual: Vec<ReconcileObjectArgument>,
    selected: Vec<Key>,
) -> PyResult<Vec<ReconcileOutcomeResult>> {
    let desired: Vec<Object> = desired.into_iter().map(reconcile_object).collect();
    let actual: Vec<Object> = actual.into_iter().map(reconcile_object).collect();
    let outcomes = py
        .detach(|| classify_targets(&desired, &actual, &selected))
        .map_err(|error| PyValueError::new_err(error.message))?;
    Ok(outcomes
        .into_iter()
        .map(|(key, outcome)| match outcome {
            Outcome::Eligible { table, transform } => {
                let name: String = key.2.clone();
                (key, name, Some((table, transform)), Vec::new())
            }
            Outcome::Rejected { name, reasons } => (key, name, None, reasons),
        })
        .collect())
}

/// `(from, to)` endpoints of a `DEPLOYMENT` or `FROM:TO` comparison, after checking the
/// database identifiers. Raises `ValueError(message)`.
#[pyfunction]
fn parse_deployment_diff(comparison: &str, identifiers: Vec<String>) -> PyResult<(String, String)> {
    if let Some(error) = identifiers
        .iter()
        .find_map(|identifier| identifier_error(identifier))
    {
        return Err(PyValueError::new_err(error));
    }
    parse_comparison(comparison).map_err(PyValueError::new_err)
}

/// `([(database, logical name, from physical name, to physical name)], catalog databases)`.
type DiffPairsResult = (
    Vec<(String, String, Option<String>, Option<String>)>,
    Vec<String>,
);

/// The relations a deployment diff compares, sorted, and the catalogs it reads. Raises
/// `ValueError(message)` for an endpoint that cannot be resolved.
#[pyfunction]
fn deployment_diff_relations(
    from: &str,
    to: &str,
    deployments_argument: Vec<DeploymentArgument>,
    active: Vec<Binding>,
    default_database: &str,
) -> PyResult<DiffPairsResult> {
    let (pairs, databases) = relation_pairs(
        from,
        to,
        &deployments(deployments_argument),
        &active,
        default_database,
    )
    .map_err(PyValueError::new_err)?;
    Ok((
        pairs
            .into_iter()
            .map(
                |RelationPair {
                     database,
                     logical_name,
                     from_physical_name,
                     to_physical_name,
                 }| (database, logical_name, from_physical_name, to_physical_name),
            )
            .collect(),
        databases,
    ))
}

/// The query counting the rows of `database.relation`. Raises `ValueError(message)` for an
/// invalid identifier.
#[pyfunction]
fn deployment_diff_count_sql(database: &str, relation: &str) -> PyResult<String> {
    count_sql(database, relation).map_err(PyValueError::new_err)
}

/// `(bound, (columns as (name, type, default), row count) when the relation exists)`.
type DiffSideArgument = (
    bool,
    Option<(Vec<(String, String, Option<String>)>, Option<i128>)>,
);

/// The status of one relation comparison.
#[pyfunction]
fn deployment_diff_status(from: DiffSideArgument, to: DiffSideArgument) -> &'static str {
    status(diff_side(&from), diff_side(&to)).as_str()
}

fn diff_side(argument: &DiffSideArgument) -> Side<'_> {
    let (bound, found) = argument;
    (
        *bound,
        found
            .as_ref()
            .map(|(columns, row_count)| (columns.as_slice(), *row_count)),
    )
}

/// `(deployment ID, created at, status, selected root names, (logical name, physical name)
/// mappings)`.
type InventoryRecordArgument = (String, String, String, Vec<String>, Vec<(String, String)>);

/// `(deployment ID, state, created at, persisted status, root names, physical relation names,
/// missing physical relation names, active binding names, latest published at)`.
type DeploymentSummaryResult = (
    String,
    &'static str,
    Option<String>,
    Option<String>,
    Vec<String>,
    Vec<String>,
    Vec<String>,
    Vec<String>,
    Option<String>,
);

/// Every deployment's lifecycle summary, newest deployment ID first.
#[pyfunction]
fn deployment_inventory_summaries(
    records: Vec<InventoryRecordArgument>,
    physical_candidate_names: Vec<String>,
    active_bindings: Vec<(String, String)>,
    publications: Vec<(String, String)>,
    existing_names: Vec<String>,
) -> Vec<DeploymentSummaryResult> {
    let evidence = Evidence {
        records: records
            .into_iter()
            .map(
                |(deployment_id, created_at, status, selected_root_names, mappings)| Record {
                    deployment_id,
                    created_at,
                    status,
                    selected_root_names,
                    mappings,
                },
            )
            .collect(),
        physical_candidate_names,
        active_bindings,
        publications,
        existing_names,
    };
    deployment_summaries(&evidence)
        .into_iter()
        .map(|summary| {
            (
                summary.deployment_id,
                summary.state.as_str(),
                summary.created_at,
                summary.persisted_status,
                summary.root_names,
                summary.physical_relation_names,
                summary.missing_physical_relation_names,
                summary.active_binding_names,
                summary.latest_published_at,
            )
        })
        .collect()
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(audit_readiness, module)?)?;
    module.add_function(wrap_pyfunction!(select_rollback_publications, module)?)?;
    module.add_function(wrap_pyfunction!(janitor_binding_activity, module)?)?;
    module.add_function(wrap_pyfunction!(janitor_preview, module)?)?;
    module.add_function(wrap_pyfunction!(py_is_deployment_id, module)?)?;
    module.add_function(wrap_pyfunction!(py_is_deployment_physical_name, module)?)?;
    module.add_function(wrap_pyfunction!(inspected_publish_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(publish_stable_bindings, module)?)?;
    module.add_function(wrap_pyfunction!(obsolete_binding_removals, module)?)?;
    module.add_function(wrap_pyfunction!(binding_replacement_preview, module)?)?;
    module.add_function(wrap_pyfunction!(classify_reconcile_targets, module)?)?;
    module.add_function(wrap_pyfunction!(parse_deployment_diff, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_diff_relations, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_diff_count_sql, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_diff_status, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_inventory_summaries, module)?)?;
    module.add_function(wrap_pyfunction!(order_relations_for_drop, module)?)
}
