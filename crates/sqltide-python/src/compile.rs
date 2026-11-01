//! Python bindings for compile policy: model relation names, retention TTL rendering, and
//! SQL audit policies, quality identities, and manifest contents.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_compile::audits::{self, AuditDefaults, AuditPolicyInput, PipelineAuditScope};
use sqltide_compile::manifest::{
    ManifestLogicalResource, ManifestResourceKind, ManifestTarget, manifest_contents,
};
use sqltide_compile::quality::{self, AuditIdentityInput, QualityFingerprints, TestIdentityInput};
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

/// `(file path, label, has name, is standalone, referenced model names, severity, severity is
/// explicit, cadence override, warmup override, scheduled override)`.
type AuditArgument = (
    String,
    String,
    bool,
    bool,
    Vec<String>,
    String,
    bool,
    Option<i128>,
    Option<i128>,
    Option<bool>,
);
/// `(severity, cadence seconds, warmup seconds, scheduled)`.
type PolicyResult = (String, Option<i128>, i128, bool);
/// `(severity, cadence seconds, warmup seconds)`.
type DefaultsArgument = (Option<String>, Option<i128>, Option<i128>);

fn audit_defaults((severity, cadence_seconds, warmup_seconds): DefaultsArgument) -> AuditDefaults {
    AuditDefaults {
        severity,
        cadence_seconds,
        warmup_seconds,
    }
}

/// Each audit's `(severity, cadence seconds, warmup seconds, scheduled)` after validating its
/// targets against `pipelines` as `(defaults, model names)`; an invalid audit raises
/// `ValueError(message)`.
#[pyfunction]
#[pyo3(name = "resolve_audit_policies")]
fn resolve_audit_policies_py(
    audits: Vec<AuditArgument>,
    pipelines: Vec<(DefaultsArgument, Vec<String>)>,
    project: DefaultsArgument,
) -> PyResult<Vec<PolicyResult>> {
    let audits: Vec<AuditPolicyInput> = audits
        .into_iter()
        .map(
            |(
                file_path,
                label,
                has_name,
                is_standalone,
                referenced_model_names,
                severity,
                severity_is_explicit,
                cadence_seconds_override,
                warmup_seconds_override,
                scheduled_override,
            )| AuditPolicyInput {
                file_path,
                label,
                has_name,
                is_standalone,
                referenced_model_names,
                severity,
                severity_is_explicit,
                cadence_seconds_override,
                warmup_seconds_override,
                scheduled_override,
            },
        )
        .collect();
    let pipelines: Vec<PipelineAuditScope> = pipelines
        .into_iter()
        .map(|(defaults, model_names)| PipelineAuditScope {
            defaults: audit_defaults(defaults),
            model_names,
        })
        .collect();
    audits::resolve_audit_policies(&audits, &pipelines, &audit_defaults(project))
        .map(|policies| {
            policies
                .into_iter()
                .map(|policy| {
                    (
                        policy.severity,
                        policy.cadence_seconds,
                        policy.warmup_seconds,
                        policy.scheduled,
                    )
                })
                .collect()
        })
        .map_err(PyValueError::new_err)
}

/// `(binding key, definition fingerprint, execution fingerprint)` of one SQL test, from its
/// canonicalized SQL.
#[pyfunction]
fn quality_test_identity(
    node_name: &str,
    mode: &str,
    assertion_names: Vec<String>,
    expected_targets: Vec<String>,
    authored_ctes: Vec<(String, String)>,
    execution_sql: &str,
) -> QualityFingerprints {
    quality::test_identity(TestIdentityInput {
        node_name,
        mode,
        assertion_names: assertion_names.iter().map(String::as_str).collect(),
        expected_targets: expected_targets.iter().map(String::as_str).collect(),
        authored_ctes: authored_ctes
            .iter()
            .map(|(name, sql)| (name.as_str(), sql.as_str()))
            .collect(),
        execution_sql,
    })
}

/// `(binding key, definition fingerprint, execution fingerprint)` of one SQL audit, from its
/// canonicalized SQL.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn quality_audit_identity(
    node_name: &str,
    attached_column: Option<&str>,
    attached_model: Option<&str>,
    attachment_kind: &str,
    referenced_models: Vec<String>,
    severity: &str,
    definition_sql: &str,
    execution_sql: &str,
) -> QualityFingerprints {
    quality::audit_identity(AuditIdentityInput {
        node_name,
        attached_column,
        attached_model,
        attachment_kind,
        referenced_models: referenced_models.iter().map(String::as_str).collect(),
        severity,
        definition_sql,
        execution_sql,
    })
}

/// `(logical type, logical name, pipeline names, [(resource kind, resource name)])`.
type ManifestLogicalArgument = (String, String, Vec<String>, Vec<(String, String)>);
/// `(sorted pipelines, sorted resource identities, fingerprint)`.
type ManifestResult = (Vec<String>, Vec<[String; 7]>, String);

/// The manifest's sorted pipelines, sorted resource identities, and fingerprint.
#[pyfunction]
fn build_manifest_contents(
    project_identity: &str,
    target_name: &str,
    target_database: &str,
    is_production: bool,
    pipeline_names: Vec<String>,
    logical_resources: Vec<ManifestLogicalArgument>,
) -> PyResult<ManifestResult> {
    let logical_resources: Vec<ManifestLogicalResource> = logical_resources
        .into_iter()
        .map(|(logical_type, logical_name, pipeline_names, resources)| {
            Ok(ManifestLogicalResource {
                logical_type,
                logical_name,
                pipeline_names,
                resources: resources
                    .into_iter()
                    .map(|(kind, name)| {
                        ManifestResourceKind::parse(&kind)
                            .map(|kind| (kind, name))
                            .ok_or_else(|| {
                                PyValueError::new_err(format!(
                                    "Unknown manifest resource kind '{kind}'"
                                ))
                            })
                    })
                    .collect::<PyResult<_>>()?,
            })
        })
        .collect::<PyResult<_>>()?;
    let contents = manifest_contents(
        &ManifestTarget {
            project_identity,
            target_name,
            target_database,
            is_production,
        },
        pipeline_names,
        &logical_resources,
    );
    Ok((contents.pipelines, contents.resources, contents.fingerprint))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(resolve_model_relation_name, module)?)?;
    module.add_function(wrap_pyfunction!(validate_project_relation_names, module)?)?;
    module.add_function(wrap_pyfunction!(render_kafka_retention_ttl, module)?)?;
    module.add_function(wrap_pyfunction!(render_model_retention_ttl, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_audit_policies_py, module)?)?;
    module.add_function(wrap_pyfunction!(quality_test_identity, module)?)?;
    module.add_function(wrap_pyfunction!(quality_audit_identity, module)?)?;
    module.add_function(wrap_pyfunction!(build_manifest_contents, module)?)
}
