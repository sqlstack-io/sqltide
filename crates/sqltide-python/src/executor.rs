//! Pure executor decisions: readiness audits and destructive ordering.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::ordering::{OrderingError, reverse_topological_order};
use sqltide_executor::readiness::{Audit, Observation, OffsetSummary, Thresholds, audit};

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

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(audit_readiness, module)?)?;
    module.add_function(wrap_pyfunction!(order_relations_for_drop, module)?)
}
