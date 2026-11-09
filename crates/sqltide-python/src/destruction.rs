//! Python bindings for destruction planning. Planning errors raise `ValueError((code, detail))`.

use std::collections::{BTreeSet, HashMap};

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use serde::Serialize;
use serde::de::DeserializeOwned;
use sqltide_executor::destruction::{
    Binding, CatalogRelation, Deployment, DestructionError, Evidence, Inventory, Key, Manifest,
    ManifestResource, Mapping, Operation, OwnedRelation, Ownership, PlanIdentity, Project,
    PublishEvent, RelationKind, Request, Snapshot, available_pipeline_names, build_challenges,
    build_evidence, drop_statements, inactive_pipelines, plan_fingerprint, plan_scope,
    resolve_selection,
};
use sqltide_executor::destruction_plan_payload::{
    PayloadError, PlanPayload, RelationPayload, Timestamp, deserialize_plan, serialize_plan,
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

/// `(step ID, display name, SQL)` dropping each `(kind, database, name)` relation in order.
#[pyfunction]
fn destruction_drop_statements(
    relations: Vec<(String, String, String)>,
    size_override: Option<i128>,
) -> PyResult<Vec<(String, String, String)>> {
    let relations: Vec<(RelationKind, String, String)> = relations
        .into_iter()
        .map(|(kind, database, name)| {
            RelationKind::parse(&kind)
                .map(|kind| (kind, database, name))
                .ok_or_else(|| PyValueError::new_err(format!("Unknown relation kind '{kind}'")))
        })
        .collect::<PyResult<_>>()?;
    Ok(drop_statements(&relations, size_override)
        .into_iter()
        .map(|statement| (statement.step_id, statement.display_name, statement.sql))
        .collect())
}

const DESTRUCTION_MODELS: &str = "sqltide.executor.destruction.models";
const DESTRUCTION_TYPES: &str = "sqltide.executor.destruction.types";
const DESTRUCTION_EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";

/// A stored enum from a Python string or string enum value.
fn stored_enum<T: DeserializeOwned>(value: &Bound<'_, PyAny>) -> PyResult<T> {
    let text: String = value.extract()?;
    serde_json::from_value(serde_json::Value::String(text))
        .map_err(|error| invalid(error.to_string()))
}

/// The Python enum `class_name` holding a stored enum's string value.
fn python_enum<'py, T: Serialize>(
    types: &Bound<'py, PyModule>,
    class_name: &str,
    value: &T,
) -> PyResult<Bound<'py, PyAny>> {
    let text: serde_json::Value =
        serde_json::to_value(value).map_err(|error| invalid(error.to_string()))?;
    types.getattr(class_name)?.call1((text.as_str(),))
}

fn timestamp(object: &Bound<'_, PyAny>, name: &str) -> PyResult<Timestamp> {
    let value: Bound<'_, PyAny> = object.getattr(name)?;
    let offset: Bound<'_, PyAny> = value.call_method0("utcoffset")?;
    let utc_offset_seconds: Option<i32> = if offset.is_none() {
        None
    } else {
        Some(field::<i32>(&offset, "days")? * 86_400 + field::<i32>(&offset, "seconds")?)
    };
    Ok(Timestamp {
        year: field(&value, "year")?,
        month: field(&value, "month")?,
        day: field(&value, "day")?,
        hour: field(&value, "hour")?,
        minute: field(&value, "minute")?,
        second: field(&value, "second")?,
        microsecond: field(&value, "microsecond")?,
        utc_offset_seconds,
    })
}

/// An aware UTC `datetime`; stored plans are always read back in UTC.
fn utc_datetime<'py>(py: Python<'py>, value: Timestamp) -> PyResult<Bound<'py, PyAny>> {
    let datetime: Bound<'py, PyModule> = py.import("datetime")?;
    let arguments = (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        datetime.getattr("UTC")?,
    );
    datetime.getattr("datetime")?.call1(arguments)
}

fn relation_payload(relation: &Bound<'_, PyAny>) -> PyResult<RelationPayload> {
    Ok(RelationPayload {
        database: field(relation, "database")?,
        name: field(relation, "name")?,
        kind: stored_enum(&relation.getattr("kind")?)?,
        exists: field(relation, "exists")?,
        total_bytes: field(relation, "total_bytes")?,
        active_parts: field(relation, "active_parts")?,
        catalog_fingerprint: field(relation, "catalog_fingerprint")?,
        logical_names: field(relation, "logical_names")?,
        pipeline_names: field(relation, "pipeline_names")?,
        ownership: items(relation, "ownership")?
            .iter()
            .map(stored_enum)
            .collect::<PyResult<_>>()?,
        dependency_relation_names: field(relation, "dependency_relation_names")?,
    })
}

/// The plan as the strict, versioned JSON envelope the plan store persists.
#[pyfunction]
fn serialize_destruction_plan(plan: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(serialize_plan(&PlanPayload {
        plan_id: field(plan, "plan_id")?,
        operation: stored_enum(&plan.getattr("operation")?)?,
        target: field(plan, "target")?,
        database: field(plan, "database")?,
        metadata_database: field(plan, "metadata_database")?,
        requested_pipeline_names: field(plan, "requested_pipeline_names")?,
        included_dependent_pipeline_names: field(plan, "included_dependent_pipeline_names")?,
        affected_pipeline_names: field(plan, "affected_pipeline_names")?,
        affected_model_names: field(plan, "affected_model_names")?,
        affected_source_names: field(plan, "affected_source_names")?,
        relations: items(plan, "relations")?
            .iter()
            .map(relation_payload)
            .collect::<PyResult<_>>()?,
        challenges: field(plan, "challenges")?,
        preserves_sources: field(plan, "preserves_sources")?,
        preserves_replay_data: field(plan, "preserves_replay_data")?,
        manifest_fingerprint: field(plan, "manifest_fingerprint")?,
        plan_fingerprint: field(plan, "plan_fingerprint")?,
        created_at: timestamp(plan, "created_at")?,
        expires_at: timestamp(plan, "expires_at")?,
        relation_drop_size_limit: field(plan, "relation_drop_size_limit")?,
        relation_drop_size_server_limit: field(plan, "relation_drop_size_server_limit")?,
        relation_drop_size_override: field(plan, "relation_drop_size_override")?,
        relation_drop_size_policy_observed: field(plan, "relation_drop_size_policy_observed")?,
        include_orphans: field(plan, "include_orphans")?,
    }))
}

/// The `DestructionPlan` in a stored envelope, raising `DestructionPlanCorruptError` for any
/// other version, unknown or mistyped field, naive timestamp, or expiry not after creation.
#[pyfunction]
fn deserialize_destruction_plan<'py>(
    py: Python<'py>,
    payload_json: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let plan: PlanPayload = deserialize_plan(payload_json).map_err(|error| {
        let message: String = match error {
            PayloadError::Incompatible(reason) => {
                format!("Stored destruction plan payload is incompatible or corrupt: {reason}")
            }
            PayloadError::Corrupt(reason) => reason,
        };
        match py
            .import(DESTRUCTION_EXCEPTIONS)
            .and_then(|module| module.getattr("DestructionPlanCorruptError"))
            .and_then(|class| class.call1((message,)))
        {
            Ok(exception) => PyErr::from_value(exception),
            Err(error) => error,
        }
    })?;
    let models: Bound<'py, PyModule> = py.import(DESTRUCTION_MODELS)?;
    let types: Bound<'py, PyModule> = py.import(DESTRUCTION_TYPES)?;
    let evidence_class: Bound<'py, PyAny> = models.getattr("DestructionRelationEvidence")?;
    let relations: Vec<Bound<'py, PyAny>> = plan
        .relations
        .iter()
        .map(|relation| {
            let ownership: Vec<Bound<'py, PyAny>> = relation
                .ownership
                .iter()
                .map(|item| python_enum(&types, "DestructionOwnership", item))
                .collect::<PyResult<_>>()?;
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("database", &relation.database)?;
            arguments.set_item("name", &relation.name)?;
            arguments.set_item(
                "kind",
                python_enum(&types, "DestructionRelationKind", &relation.kind)?,
            )?;
            arguments.set_item("exists", relation.exists)?;
            arguments.set_item("total_bytes", relation.total_bytes)?;
            arguments.set_item("active_parts", relation.active_parts)?;
            arguments.set_item("catalog_fingerprint", &relation.catalog_fingerprint)?;
            arguments.set_item("logical_names", PyTuple::new(py, &relation.logical_names)?)?;
            arguments.set_item(
                "pipeline_names",
                PyTuple::new(py, &relation.pipeline_names)?,
            )?;
            arguments.set_item("ownership", PyTuple::new(py, ownership)?)?;
            arguments.set_item(
                "dependency_relation_names",
                PyTuple::new(py, &relation.dependency_relation_names)?,
            )?;
            evidence_class.call((), Some(&arguments))
        })
        .collect::<PyResult<_>>()?;
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    arguments.set_item("plan_id", &plan.plan_id)?;
    arguments.set_item(
        "operation",
        python_enum(&types, "DestructionOperation", &plan.operation)?,
    )?;
    arguments.set_item("target", &plan.target)?;
    arguments.set_item("database", &plan.database)?;
    arguments.set_item("metadata_database", &plan.metadata_database)?;
    for (name, names) in [
        ("requested_pipeline_names", &plan.requested_pipeline_names),
        (
            "included_dependent_pipeline_names",
            &plan.included_dependent_pipeline_names,
        ),
        ("affected_pipeline_names", &plan.affected_pipeline_names),
        ("affected_model_names", &plan.affected_model_names),
        ("affected_source_names", &plan.affected_source_names),
        ("challenges", &plan.challenges),
    ] {
        arguments.set_item(name, PyTuple::new(py, names)?)?;
    }
    arguments.set_item("relations", PyTuple::new(py, relations)?)?;
    arguments.set_item("preserves_sources", plan.preserves_sources)?;
    arguments.set_item("preserves_replay_data", plan.preserves_replay_data)?;
    arguments.set_item("manifest_fingerprint", &plan.manifest_fingerprint)?;
    arguments.set_item("plan_fingerprint", &plan.plan_fingerprint)?;
    arguments.set_item("created_at", utc_datetime(py, plan.created_at)?)?;
    arguments.set_item("expires_at", utc_datetime(py, plan.expires_at)?)?;
    arguments.set_item("relation_drop_size_limit", plan.relation_drop_size_limit)?;
    arguments.set_item(
        "relation_drop_size_server_limit",
        plan.relation_drop_size_server_limit,
    )?;
    arguments.set_item(
        "relation_drop_size_override",
        plan.relation_drop_size_override,
    )?;
    arguments.set_item(
        "relation_drop_size_policy_observed",
        plan.relation_drop_size_policy_observed,
    )?;
    arguments.set_item("include_orphans", plan.include_orphans)?;
    models
        .getattr("DestructionPlan")?
        .call((), Some(&arguments))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(serialize_destruction_plan, module)?)?;
    module.add_function(wrap_pyfunction!(deserialize_destruction_plan, module)?)?;
    module.add_function(wrap_pyfunction!(destruction_drop_statements, module)?)?;
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
