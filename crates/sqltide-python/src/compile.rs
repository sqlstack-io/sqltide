//! Python bindings for compile policy: model relation names and retention TTL rendering.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_compile::relations::{self, ModelClaim, ModelNaming, ModelRelationKind, SourceClaim};
use sqltide_compile::retention::{self, KafkaRetention, ModelRetention};

fn kind(is_table: bool) -> ModelRelationKind {
    if is_table {
        ModelRelationKind::Table
    } else {
        ModelRelationKind::View
    }
}

/// The model's validated relation name. An invalid name raises `ValueError(message)`.
#[pyfunction]
fn resolve_model_relation_name(
    logical_name: &str,
    is_table: bool,
    exact_name: Option<&str>,
    pipeline_prefix: Option<&str>,
    project_prefix: Option<&str>,
) -> PyResult<String> {
    relations::resolve_model_relation_name(&ModelNaming {
        logical_name,
        kind: kind(is_table),
        exact_name,
        pipeline_prefix,
        project_prefix,
    })
    .map_err(PyValueError::new_err)
}

/// Reject physical relation name collisions across `sources` as `(name, adopted table name)`
/// and `models` as `(name, relation name, is table)`; a collision raises `ValueError(message)`.
#[pyfunction]
fn validate_project_relation_names(
    sources: Vec<(String, Option<String>)>,
    models: Vec<(String, String, bool)>,
) -> PyResult<()> {
    let sources: Vec<SourceClaim<'_>> = sources
        .iter()
        .map(|(name, table_name)| match table_name {
            Some(table_name) => SourceClaim::Adopted { name, table_name },
            None => SourceClaim::Managed { name },
        })
        .collect();
    let models: Vec<ModelClaim<'_>> = models
        .iter()
        .map(|(name, relation_name, is_table)| ModelClaim {
            name,
            relation_name,
            kind: kind(*is_table),
        })
        .collect();
    relations::validate_project_relation_names(&sources, &models).map_err(PyValueError::new_err)
}

/// The managed Kafka table TTL expression.
#[pyfunction]
fn render_kafka_retention_ttl(
    duration_seconds: i128,
    fallback_landed: bool,
    cap_at_landed: bool,
) -> String {
    retention::render_kafka_retention(&KafkaRetention {
        duration_seconds,
        fallback_landed,
        cap_at_landed,
    })
}

/// The model TTL expression, or `None` when skippable columns are missing; an unusable policy
/// raises `ValueError(message)`.
#[pyfunction]
fn render_model_retention_ttl(
    duration_seconds: i128,
    timestamp_column: &str,
    cap_at_column: Option<&str>,
    skip_when_missing: bool,
    available_columns: HashMap<String, String>,
    model_name: &str,
) -> PyResult<Option<String>> {
    retention::render_model_retention(
        &ModelRetention {
            duration_seconds,
            timestamp_column,
            cap_at_column,
            skip_when_missing,
        },
        &available_columns,
        model_name,
    )
    .map_err(PyValueError::new_err)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(resolve_model_relation_name, module)?)?;
    module.add_function(wrap_pyfunction!(validate_project_relation_names, module)?)?;
    module.add_function(wrap_pyfunction!(render_kafka_retention_ttl, module)?)?;
    module.add_function(wrap_pyfunction!(render_model_retention_ttl, module)?)
}
