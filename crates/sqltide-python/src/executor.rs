//! Pure executor decisions: readiness audits, rollback targets, janitor retention, and
//! destructive ordering.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::janitor::{
    Decision, Deployment, Mapping, Publication as RetainedPublication, Retention, binding_activity,
    preview,
};
use sqltide_executor::ordering::{OrderingError, reverse_topological_order};
use sqltide_executor::publications::{
    Binding, Publication, Rank, RollbackError, RollbackTarget, select_rollback,
};
use sqltide_executor::readiness::{Audit, Observation, OffsetSummary, Thresholds, audit};
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

fn deployments(arguments: Vec<DeploymentArgument>) -> Vec<Deployment> {
    arguments
        .into_iter()
        .map(
            |(deployment_id, created_at, incomplete, mappings)| Deployment {
                deployment_id,
                created_at,
                incomplete,
                mappings: mappings
                    .into_iter()
                    .map(
                        |(database, publishes, logical_name, physical_name, logical_model_name)| {
                            Mapping {
                                database,
                                publishes,
                                logical_name,
                                physical_name,
                                logical_model_name,
                            }
                        },
                    )
                    .collect(),
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

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(audit_readiness, module)?)?;
    module.add_function(wrap_pyfunction!(select_rollback_publications, module)?)?;
    module.add_function(wrap_pyfunction!(janitor_binding_activity, module)?)?;
    module.add_function(wrap_pyfunction!(janitor_preview, module)?)?;
    module.add_function(wrap_pyfunction!(py_is_deployment_id, module)?)?;
    module.add_function(wrap_pyfunction!(py_is_deployment_physical_name, module)?)?;
    module.add_function(wrap_pyfunction!(order_relations_for_drop, module)?)
}
