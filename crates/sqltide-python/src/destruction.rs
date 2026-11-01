//! Python bindings for destruction planning. Planning errors raise `ValueError((code, detail))`.

use std::collections::{BTreeSet, HashMap};

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::destruction::{
    Binding, CatalogRelation, Deployment, DestructionError, Evidence, Inventory, Key, Manifest,
    ManifestResource, Mapping, Operation, OwnedRelation, Ownership, PlanIdentity, Project,
    PublishEvent, RelationKind, Request, Snapshot, available_pipeline_names, build_challenges,
    build_evidence, inactive_pipelines, plan_fingerprint, plan_scope, resolve_selection,
};

/// `(keys as (name, is source, model pipeline, [(resource name, kind)]), upstream, downstream,
/// [(pipeline, declared source)], sources, models)`.
type ProjectArgument = (
    Vec<(String, bool, Option<String>, Vec<(String, String)>)>,
    Vec<Vec<usize>>,
    Vec<Vec<usize>>,
    Vec<(String, Option<usize>)>,
    Vec<usize>,
    Vec<usize>,
);
/// `(operation, database, pipeline names, included dependent pipeline names, include orphans)`.
type RequestArgument = (String, String, Vec<String>, Vec<String>, bool);
/// `(name, kind, logical names, pipeline names, ownership)`.
type OwnedTuple = (String, String, Vec<String>, Vec<String>, Vec<String>);
/// `(database, name, kind, exists, total bytes, active parts, catalog fingerprint, logical names,
/// pipeline names, ownership, dependency relation names)`.
type EvidenceTuple = (
    String,
    String,
    String,
    bool,
    Option<i128>,
    Option<i128>,
    Option<String>,
    Vec<String>,
    Vec<String>,
    Vec<String>,
    Vec<String>,
);

fn field<'py, T: FromPyObject<'py>>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<T> {
    object.getattr(name)?.extract()
}

fn items<'py>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<Vec<Bound<'py, PyAny>>> {
    object.getattr(name)?.try_iter()?.collect()
}

fn invalid(message: String) -> PyErr {
    PyValueError::new_err(message)
}

fn relation_kind(text: &str) -> PyResult<RelationKind> {
    RelationKind::parse(text).ok_or_else(|| invalid(format!("Unknown relation kind '{text}'")))
}

fn ownership(text: &str) -> PyResult<Ownership> {
    Ok(match text {
        "current_manifest" => Ownership::CurrentManifest,
        "historical_manifest" => Ownership::HistoricalManifest,
        "published_stable_binding" => Ownership::PublishedStableBinding,
        "virtual_physical_mapping" => Ownership::VirtualPhysicalMapping,
        other => return Err(invalid(format!("Unknown destruction ownership '{other}'"))),
    })
}

fn parse_operation(text: &str) -> PyResult<Operation> {
    Ok(match text {
        "destroy_pipelines" => Operation::DestroyPipelines,
        "reset_target" => Operation::ResetTarget,
        other => return Err(invalid(format!("Unknown destruction operation '{other}'"))),
    })
}

fn project(argument: ProjectArgument) -> PyResult<Project> {
    let (keys, upstream, downstream, pipelines, sources, models) = argument;
    let count = keys.len();
    let in_range = |index: &usize| *index < count;
    if upstream.len() != count
        || downstream.len() != count
        || !upstream.iter().chain(&downstream).flatten().all(in_range)
        || !pipelines
            .iter()
            .filter_map(|(_, source)| source.as_ref())
            .all(in_range)
        || !sources.iter().chain(&models).all(in_range)
    {
        return Err(invalid(
            "Destruction project key index out of range".to_owned(),
        ));
    }
    Ok(Project {
        keys: keys
            .into_iter()
            .map(|(name, source, pipeline, resources)| {
                Ok(Key {
                    name,
                    source,
                    pipeline,
                    resources: resources
                        .into_iter()
                        .map(|(name, kind)| Ok((name, relation_kind(&kind)?)))
                        .collect::<PyResult<_>>()?,
                })
            })
            .collect::<PyResult<_>>()?,
        upstream,
        downstream,
        pipelines,
        sources,
        models,
    })
}

fn request(argument: RequestArgument) -> PyResult<Request> {
    let (text, database, pipeline_names, included_dependent_pipeline_names, include_orphans) =
        argument;
    Ok(Request {
        operation: parse_operation(&text)?,
        database,
        pipeline_names,
        included_dependent_pipeline_names,
        include_orphans,
    })
}

/// An `AdapterManifestSnapshot`; the caller has already rejected unavailable history.
fn snapshot(object: &Bound<'_, PyAny>) -> PyResult<Snapshot> {
    let status: String = field(object, "status")?;
    Ok(Snapshot {
        absent: status == "absent",
        manifests: items(object, "manifests")?
            .iter()
            .map(|manifest| {
                Ok(Manifest {
                    published_at: field(manifest, "published_at")?,
                    resources: items(manifest, "resources")?
                        .iter()
                        .map(|resource| {
                            Ok(ManifestResource {
                                resource_name: field(resource, "resource_name")?,
                                pipeline_name: field(resource, "pipeline_name")?,
                                logical_name: field(resource, "logical_name")?,
                                logical_type: field(resource, "logical_type")?,
                                resource_role: field(resource, "resource_role")?,
                                resource_database: field(resource, "resource_database")?,
                                resource_kind: field(resource, "resource_kind")?,
                            })
                        })
                        .collect::<PyResult<_>>()?,
                })
            })
            .collect::<PyResult<_>>()?,
    })
}

fn optional_snapshot(object: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Snapshot>> {
    object.map(snapshot).transpose()
}

/// The relations of a `CatalogSnapshot`.
fn catalog(object: &Bound<'_, PyAny>) -> PyResult<Vec<CatalogRelation>> {
    items(object, "relations")?
        .iter()
        .map(|relation| {
            let mut dependency_names: Vec<String> = items(relation, "source_relation_names")?
                .iter()
                .map(PyAnyMethods::extract)
                .collect::<PyResult<_>>()?;
            for name in ["target_relation_name", "stable_binding_name"] {
                dependency_names.extend(field::<Option<String>>(relation, name)?);
            }
            Ok(CatalogRelation {
                name: field(relation, "name")?,
                engine: field(relation, "engine")?,
                ownership_generation: field(relation, "ownership_generation")?,
                dependency_names,
            })
        })
        .collect()
}

/// An `AdapterDeploymentInventory`.
fn inventory(object: &Bound<'_, PyAny>) -> PyResult<Inventory> {
    Ok(Inventory {
        deployments: items(object, "deployments")?
            .iter()
            .map(|deployment| {
                Ok(Deployment {
                    deployment_id: field(deployment, "deployment_id")?,
                    mappings: items(deployment, "prepared_object_mappings")?
                        .iter()
                        .map(|mapping| {
                            let key = mapping.getattr("logical_key")?;
                            Ok(Mapping {
                                database: field(&key, "database")?,
                                object_type: field(&key, "object_type")?,
                                name: field(&key, "name")?,
                                physical_name: field(mapping, "physical_name")?,
                                logical_model_name: field(mapping, "logical_model_name")?,
                            })
                        })
                        .collect::<PyResult<_>>()?,
                })
            })
            .collect::<PyResult<_>>()?,
        publish_events: items(object, "publish_events")?
            .iter()
            .map(|event| {
                Ok(PublishEvent {
                    deployment_id: field(event, "deployment_id")?,
                    published_at: field(event, "published_at")?,
                    bindings: items(event, "bindings")?
                        .iter()
                        .map(|binding| {
                            Ok(Binding {
                                database: field(binding, "database")?,
                                logical_name: field(binding, "logical_name")?,
                                physical_name: field(binding, "physical_name")?,
                            })
                        })
                        .collect::<PyResult<_>>()?,
                })
            })
            .collect::<PyResult<_>>()?,
    })
}

fn destruction_error(py: Python<'_>, error: DestructionError) -> PyErr {
    let detail: PyResult<Py<PyAny>> = match error {
        DestructionError::Selection(message) => ("selection", message).into_py_any(py),
        DestructionError::Resource(message) => ("resource", message).into_py_any(py),
        DestructionError::UnknownPipelines(names) => ("unknown_pipelines", names).into_py_any(py),
        DestructionError::Dependency(names) => ("dependency", names).into_py_any(py),
        DestructionError::UnsupportedHistoricalKind { kind, name } => {
            ("unsupported_historical_kind", (kind, name)).into_py_any(py)
        }
        DestructionError::InvalidObjectType(object_type) => {
            ("invalid_object_type", object_type).into_py_any(py)
        }
    };
    match detail {
        Ok(detail) => PyValueError::new_err((detail,)),
        Err(error) => error,
    }
}

fn owned_tuple(relation: OwnedRelation) -> OwnedTuple {
    (
        relation.name,
        relation.kind.as_str().to_owned(),
        relation.logical_names.into_iter().collect(),
        relation.pipeline_names.into_iter().collect(),
        relation
            .ownership
            .into_iter()
            .map(|value| value.as_str().to_owned())
            .collect(),
    )
}

fn owned_relation(tuple: OwnedTuple) -> PyResult<OwnedRelation> {
    let (name, kind, logical_names, pipeline_names, ownership_values) = tuple;
    Ok(OwnedRelation {
        name,
        kind: relation_kind(&kind)?,
        logical_names: logical_names.into_iter().collect(),
        pipeline_names: pipeline_names.into_iter().collect(),
        ownership: ownership_values
            .iter()
            .map(|value| ownership(value))
            .collect::<PyResult<BTreeSet<_>>>()?,
    })
}

fn evidence_tuple(evidence: Evidence) -> EvidenceTuple {
    (
        evidence.database,
        evidence.name,
        evidence.kind.as_str().to_owned(),
        evidence.exists,
        evidence.total_bytes,
        evidence.active_parts,
        evidence.catalog_fingerprint,
        evidence.logical_names,
        evidence.pipeline_names,
        evidence
            .ownership
            .into_iter()
            .map(|value| value.as_str().to_owned())
            .collect(),
        evidence.dependency_relation_names,
    )
}

fn evidence(tuple: EvidenceTuple) -> PyResult<Evidence> {
    let (
        database,
        name,
        kind,
        exists,
        total_bytes,
        active_parts,
        catalog_fingerprint,
        logical_names,
        pipeline_names,
        ownership_values,
        dependency_relation_names,
    ) = tuple;
    Ok(Evidence {
        database,
        name,
        kind: relation_kind(&kind)?,
        exists,
        total_bytes,
        active_parts,
        catalog_fingerprint,
        logical_names,
        pipeline_names,
        ownership: ownership_values
            .iter()
            .map(|value| ownership(value))
            .collect::<PyResult<_>>()?,
        dependency_relation_names,
    })
}

/// The current pipelines plus those only manifest history still owns.
#[pyfunction]
#[pyo3(signature = (current, snapshot))]
fn destruction_available_pipelines(
    current: Vec<String>,
    snapshot: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<String>> {
    Ok(available_pipeline_names(
        &current,
        optional_snapshot(snapshot)?.as_ref(),
    ))
}

/// `(requested, included dependents, affected)` pipeline names.
#[pyfunction]
fn resolve_destruction_selection(
    py: Python<'_>,
    project_argument: ProjectArgument,
    request_argument: RequestArgument,
    available: Vec<String>,
) -> PyResult<(Vec<String>, Vec<String>, Vec<String>)> {
    resolve_selection(
        &project(project_argument)?,
        &request(request_argument)?,
        &available,
    )
    .map_err(|error| destruction_error(py, error))
}

/// `(affected model names, affected source names, owned relations)`.
#[pyfunction]
#[pyo3(signature = (project_argument, request_argument, affected, current, snapshot, catalog_snapshot, deployment_inventory))]
#[allow(clippy::too_many_arguments)]
fn plan_destruction_scope(
    py: Python<'_>,
    project_argument: ProjectArgument,
    request_argument: RequestArgument,
    affected: Vec<String>,
    current: Vec<String>,
    snapshot: Option<&Bound<'_, PyAny>>,
    catalog_snapshot: &Bound<'_, PyAny>,
    deployment_inventory: &Bound<'_, PyAny>,
) -> PyResult<(Vec<String>, Vec<String>, Vec<OwnedTuple>)> {
    let scope = plan_scope(
        &project(project_argument)?,
        &request(request_argument)?,
        &affected,
        &current,
        optional_snapshot(snapshot)?.as_ref(),
        &catalog(catalog_snapshot)?,
        &inventory(deployment_inventory)?,
    )
    .map_err(|error| destruction_error(py, error))?;
    Ok((
        scope.affected_model_names,
        scope.affected_source_names,
        scope.owned.into_iter().map(owned_tuple).collect(),
    ))
}

/// Warehouse evidence for owned relations, given `{name: (total bytes, active parts)}`.
#[pyfunction]
fn build_destruction_evidence(
    py: Python<'_>,
    database: &str,
    owned: Vec<OwnedTuple>,
    catalog_snapshot: &Bound<'_, PyAny>,
    stats: HashMap<String, (i128, i128)>,
    limit: Option<i128>,
) -> PyResult<Vec<EvidenceTuple>> {
    let owned: Vec<OwnedRelation> = owned
        .into_iter()
        .map(owned_relation)
        .collect::<PyResult<_>>()?;
    build_evidence(database, &owned, &catalog(catalog_snapshot)?, &stats, limit)
        .map(|evidence| evidence.into_iter().map(evidence_tuple).collect())
        .map_err(|error| destruction_error(py, error))
}

/// Ordered challenge values for the affected pipelines.
#[pyfunction]
#[pyo3(name = "build_destruction_challenges")]
fn py_build_destruction_challenges(
    py: Python<'_>,
    pipeline_names: Vec<String>,
    production_reset: bool,
) -> PyResult<Vec<String>> {
    build_challenges(&pipeline_names, production_reset)
        .map_err(|error| destruction_error(py, error))
}

/// The canonical fingerprint of a destruction plan's identity.
#[pyfunction]
#[pyo3(signature = (
    *,
    operation,
    target,
    database,
    metadata_database,
    requested_pipeline_names,
    included_dependent_pipeline_names,
    affected_pipeline_names,
    affected_model_names,
    affected_source_names,
    relations,
    challenges,
    manifest_fingerprint,
    relation_drop_size_limit,
    relation_drop_size_server_limit,
    relation_drop_size_override,
    relation_drop_size_policy_observed,
    include_orphans,
))]
#[allow(clippy::too_many_arguments)]
fn destruction_plan_fingerprint(
    operation: &str,
    target: &str,
    database: &str,
    metadata_database: &str,
    requested_pipeline_names: Vec<String>,
    included_dependent_pipeline_names: Vec<String>,
    affected_pipeline_names: Vec<String>,
    affected_model_names: Vec<String>,
    affected_source_names: Vec<String>,
    relations: Vec<EvidenceTuple>,
    challenges: Vec<String>,
    manifest_fingerprint: &str,
    relation_drop_size_limit: Option<i128>,
    relation_drop_size_server_limit: Option<i128>,
    relation_drop_size_override: Option<i128>,
    relation_drop_size_policy_observed: bool,
    include_orphans: bool,
) -> PyResult<String> {
    let relations: Vec<Evidence> = relations
        .into_iter()
        .map(evidence)
        .collect::<PyResult<_>>()?;
    Ok(plan_fingerprint(&PlanIdentity {
        operation: parse_operation(operation)?,
        target,
        database,
        metadata_database,
        requested_pipeline_names: &requested_pipeline_names,
        included_dependent_pipeline_names: &included_dependent_pipeline_names,
        affected_pipeline_names: &affected_pipeline_names,
        affected_model_names: &affected_model_names,
        affected_source_names: &affected_source_names,
        relations: &relations,
        challenges: &challenges,
        manifest_fingerprint,
        relation_drop_size_limit,
        relation_drop_size_server_limit,
        relation_drop_size_override,
        relation_drop_size_policy_observed,
        include_orphans,
    }))
}

/// `(name, model count, resource count, last published at)` per inactive pipeline.
#[pyfunction]
fn list_destruction_inactive_pipelines(
    current: Vec<String>,
    snapshot_object: &Bound<'_, PyAny>,
) -> PyResult<Vec<(String, usize, usize, String)>> {
    Ok(inactive_pipelines(&current, &snapshot(snapshot_object)?)
        .into_iter()
        .map(|pipeline| {
            (
                pipeline.name,
                pipeline.model_count,
                pipeline.resource_count,
                pipeline.last_published_at,
            )
        })
        .collect())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(destruction_available_pipelines, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_destruction_selection, module)?)?;
    module.add_function(wrap_pyfunction!(plan_destruction_scope, module)?)?;
    module.add_function(wrap_pyfunction!(build_destruction_evidence, module)?)?;
    module.add_function(wrap_pyfunction!(py_build_destruction_challenges, module)?)?;
    module.add_function(wrap_pyfunction!(destruction_plan_fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(
        list_destruction_inactive_pipelines,
        module
    )?)
}
