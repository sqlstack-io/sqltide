//! Deployment resolution over the adapter connection: which staged deployment an audit or a
//! publish targets, the readiness audit of its roots, the stable bindings a publish writes and
//! their live effects, and the publication a rollback restores. Warehouse reads go through the
//! Python adapter connection; the decisions are the native promotion, publication and
//! readiness planners'.

use std::collections::{BTreeSet, HashSet};

use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};
use sqltide_planner::naming::is_deployment_physical_name;

use crate::executor::{
    MappingArgument, audit_readiness, binding_replacement_preview, inspected_publish_candidates,
    obsolete_binding_removals, publish_stable_bindings, select_rollback_publications,
};
use crate::model_sql::chained;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const READINESS_MODELS: &str = "sqltide.executor.readiness.models";
const READINESS_TYPES: &str = "sqltide.executor.readiness.types";
const READINESS_EXCEPTIONS: &str = "sqltide.executor.readiness.exceptions";
const PROMOTION_MODELS: &str = "sqltide.executor.promotion.models";
const PROMOTION_TYPES: &str = "sqltide.executor.promotion.types";
const PROMOTION_EXCEPTIONS: &str = "sqltide.executor.promotion.exceptions";
const DEPLOYMENT_SEPARATOR: &str = "__";

type Value<'py> = Bound<'py, PyAny>;

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    keywords: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in keywords {
        arguments.set_item(keyword, value)?;
    }
    class(py, module, name)?.call((), Some(&arguments))
}

fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn text<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

fn boolean(py: Python<'_>, value: bool) -> PyResult<Value<'_>> {
    Ok(value.into_pyobject(py)?.to_owned().into_any())
}

/// `value or default`.
fn or<'py>(value: Value<'py>, default: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(if value.is_truthy()? {
        value
    } else {
        default.clone()
    })
}

/// `value or None`.
fn or_none<'py>(value: Value<'py>) -> PyResult<Value<'py>> {
    Ok(if value.is_truthy()? {
        value
    } else {
        value.py().None().into_bound(value.py())
    })
}

fn error(py: Python<'_>, module: &str, name: &str, message: String) -> PyErr {
    match class(py, module, name).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn audit_error(py: Python<'_>, message: String) -> PyErr {
    error(
        py,
        READINESS_EXCEPTIONS,
        "DeploymentAuditExecutionError",
        message,
    )
}

fn publish_error(py: Python<'_>, message: String) -> PyErr {
    error(py, PROMOTION_EXCEPTIONS, "PublishExecutionError", message)
}

/// `raise PublishExecutionError(message) from None` while handling `handled`.
fn publish_error_from_none(py: Python<'_>, message: String, handled: &PyErr) -> PyResult<PyErr> {
    let translated: Value<'_> =
        class(py, PROMOTION_EXCEPTIONS, "PublishExecutionError")?.call1((message,))?;
    chained(&translated, handled.value(py), None)?;
    Ok(PyErr::from_value(translated))
}

fn incomplete_status(py: Python<'_>) -> PyResult<Value<'_>> {
    class(
        py,
        ADAPTER_CONSTANTS,
        "VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE",
    )
}

fn object_type<'py>(py: Python<'py>, name: &str) -> PyResult<Value<'py>> {
    class(py, COMPILE_CONSTANTS, name)
}

/// `object_type in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}`.
fn publishes(object_type_value: &Value<'_>) -> PyResult<bool> {
    let py: Python<'_> = object_type_value.py();
    let published: Bound<'_, PySet> = PySet::new(
        py,
        [
            object_type(py, "DESIRED_OBJECT_TYPE_TABLE")?,
            object_type(py, "DESIRED_OBJECT_TYPE_VIEW")?,
        ],
    )?;
    published.contains(object_type_value)
}

/// The deployment identifier after the last separator, raising `IndexError` like
/// `physical_name.split("__")[-1]` would for a name without one.
fn deployment_id_of(physical_name: &str) -> PyResult<String> {
    physical_name
        .rsplit_once(DEPLOYMENT_SEPARATOR)
        .map(|(_, deployment_id)| deployment_id.to_owned())
        .ok_or_else(|| PyIndexError::new_err("list index out of range"))
}

/// `(database, logical name, physical name)` of each binding.
fn binding_triples(bindings: &Value<'_>) -> PyResult<Vec<(String, String, String)>> {
    items(bindings)?
        .iter()
        .map(|binding| {
            Ok((
                binding.getattr("database")?.extract()?,
                binding.getattr("logical_name")?.extract()?,
                binding.getattr("physical_name")?.extract()?,
            ))
        })
        .collect()
}

fn audit_candidates<'py>(py: Python<'py>, deployment_ids: Vec<String>) -> PyResult<Value<'py>> {
    tuple(
        py,
        deployment_ids
            .into_iter()
            .map(|deployment_id| {
                build(
                    py,
                    READINESS_MODELS,
                    "AuditDeploymentCandidate",
                    &[("deployment_id", text(py, &deployment_id))],
                )
            })
            .collect::<PyResult<_>>()?,
    )
}

/// Staged deployments selectable by default resolution: persisted deployments `keep` accepts
/// and inspected staged relations, newer than the single active deployment when there is one.
fn deployment_candidates<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
    publish: bool,
) -> PyResult<Value<'py>> {
    let inspected: Value<'py> =
        client.call_method1("inspect_managed_table_state", (default_database,))?;
    let inventory: Value<'py> =
        client.call_method1("load_deployment_inventory", (metadata_database,))?;
    let existing: Value<'py> = client
        .call_method1("load_catalog", (default_database,))?
        .call_method0("relation_names")?;
    let incomplete: Value<'py> = incomplete_status(py)?;
    let deployments: Vec<Value<'py>> = items(&inventory.getattr("deployments")?)?;
    let mut all: BTreeSet<String> = BTreeSet::new();
    let mut recorded: HashSet<String> = HashSet::new();
    for deployment in &deployments {
        recorded.insert(deployment.getattr("deployment_id")?.extract()?);
    }
    for deployment in &deployments {
        let mappings: Vec<Value<'py>> = items(&deployment.getattr("prepared_object_mappings")?)?;
        let mut existing_mappings: usize = 0;
        for mapping in &mappings {
            if existing.contains(mapping.getattr("physical_name")?)? {
                existing_mappings += 1;
            }
        }
        let kept: bool = if publish {
            deployment.getattr("status")?.ne(&incomplete)?
                && !mappings.is_empty()
                && existing_mappings == mappings.len()
        } else {
            existing_mappings > 0
        };
        if kept {
            all.insert(deployment.getattr("deployment_id")?.extract()?);
        }
    }
    for candidate in items(&inspected.getattr("physical_candidates")?)? {
        let physical_name: String = candidate.getattr("physical_name")?.extract()?;
        if !is_deployment_physical_name(&physical_name) {
            continue;
        }
        let deployment_id: String = deployment_id_of(&physical_name)?;
        if !publish || !recorded.contains(&deployment_id) {
            all.insert(deployment_id);
        }
    }
    let mut active: HashSet<String> = HashSet::new();
    for binding in items(&inspected.getattr("active_bindings")?)? {
        let physical_name: String = binding.getattr("physical_name")?.extract()?;
        if is_deployment_physical_name(&physical_name) {
            active.insert(deployment_id_of(&physical_name)?);
        }
    }
    let all: Vec<String> = all.into_iter().collect();
    if active.len() != 1 {
        return audit_candidates(py, all);
    }
    let active_deployment_id: &String = active.iter().next().unwrap_or_else(|| unreachable!());
    audit_candidates(
        py,
        all.into_iter()
            .filter(|deployment_id| deployment_id > active_deployment_id)
            .collect(),
    )
}

/// Candidate staged deployments visible to audit default resolution.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database))]
fn build_audit_deployment_candidates<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
) -> PyResult<Value<'py>> {
    deployment_candidates(py, client, metadata_database, default_database, false)
}

/// Candidate staged deployments visible to publish default resolution.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database))]
fn build_publish_deployment_candidates<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
) -> PyResult<Value<'py>> {
    deployment_candidates(py, client, metadata_database, default_database, true)
}

/// The one deployment `candidates` leaves, or the `kind` resolution failure.
fn resolve_candidate<'py>(
    py: Python<'py>,
    candidates: &Value<'py>,
    kind: &str,
    raise: fn(Python<'_>, String) -> PyErr,
) -> PyResult<Value<'py>> {
    let candidates: Vec<Value<'py>> = items(candidates)?;
    match candidates.as_slice() {
        [candidate] => candidate.getattr("deployment_id"),
        [] => Err(raise(
            py,
            format!("No staged deployment candidates are available for {kind}"),
        )),
        _ => {
            let ids: Vec<String> = candidates
                .iter()
                .map(|candidate| candidate.getattr("deployment_id")?.str()?.extract())
                .collect::<PyResult<_>>()?;
            let kind_title: String = format!("{}{}", kind[..1].to_uppercase(), &kind[1..]);
            Err(raise(
                py,
                format!(
                    "{kind_title} deployment resolution is ambiguous; choose one of: {}",
                    ids.join(", ")
                ),
            ))
        }
    }
}

/// The deployment ID to publish, requiring an explicit choice when ambiguous.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database, deployment_id))]
pub(crate) fn resolve_publish_deployment_id<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
    deployment_id: Value<'py>,
) -> PyResult<Value<'py>> {
    if !deployment_id.is_none() {
        return Ok(deployment_id);
    }
    let candidates: Value<'py> =
        deployment_candidates(py, client, metadata_database, default_database, true)?;
    resolve_candidate(py, &candidates, "publish", publish_error)
}

/// The persisted deployment metadata an audit needs.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, deployment_id))]
fn load_audit_deployment<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    deployment_id: &Value<'py>,
) -> PyResult<Value<'py>> {
    let inventory: Value<'py> =
        client.call_method1("load_deployment_inventory", (metadata_database,))?;
    let mut found: Option<Value<'py>> = None;
    for deployment in items(&inventory.getattr("deployments")?)? {
        if deployment.getattr("deployment_id")?.eq(deployment_id)? && found.is_none() {
            found = Some(deployment);
        }
    }
    let Some(deployment) = found else {
        return build(
            py,
            READINESS_MODELS,
            "LoadedAuditDeployment",
            &[
                ("deployment_id", deployment_id.clone()),
                ("created_at", text(py, "")),
                ("status", text(py, "metadata_missing")),
                ("replay_lineage_mode", py.None().into_bound(py)),
                ("warning_codes", tuple(py, Vec::new())?),
                ("root_keys", tuple(py, Vec::new())?),
                ("prepared_object_mappings", tuple(py, Vec::new())?),
            ],
        );
    };
    let object_key = |key: &Value<'py>| -> PyResult<Value<'py>> {
        build(
            py,
            COMPILE_MODELS,
            "ObjectKey",
            &[
                ("database", key.getattr("database")?),
                ("object_type", key.getattr("object_type")?),
                ("name", key.getattr("name")?),
            ],
        )
    };
    let root_keys: Vec<Value<'py>> = items(&deployment.getattr("selected_root_keys")?)?
        .iter()
        .map(object_key)
        .collect::<PyResult<_>>()?;
    let mappings: Vec<Value<'py>> = items(&deployment.getattr("prepared_object_mappings")?)?
        .iter()
        .map(|mapping| {
            tuple(
                py,
                vec![
                    object_key(&mapping.getattr("logical_key")?)?,
                    mapping.getattr("physical_name")?,
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        READINESS_MODELS,
        "LoadedAuditDeployment",
        &[
            ("deployment_id", deployment_id.clone()),
            ("created_at", deployment.getattr("created_at")?),
            ("status", deployment.getattr("status")?),
            (
                "replay_lineage_mode",
                deployment.getattr("replay_lineage_mode")?,
            ),
            ("warning_codes", deployment.getattr("warning_codes")?),
            ("root_keys", tuple(py, root_keys)?),
            ("prepared_object_mappings", tuple(py, mappings)?),
        ],
    )
}

/// Copy the named attributes of `summary` into a `class` instance, or `None`.
fn summary<'py>(
    py: Python<'py>,
    summary: Value<'py>,
    name: &str,
    fields: &[&str],
) -> PyResult<Value<'py>> {
    if summary.is_none() {
        return Ok(summary);
    }
    let keywords: Vec<(&str, Value<'py>)> = fields
        .iter()
        .map(|field| Ok((*field, summary.getattr(*field)?)))
        .collect::<PyResult<_>>()?;
    build(py, READINESS_MODELS, name, &keywords)
}

const OFFSET_SUMMARY_FIELDS: [&str; 11] = [
    "active_partition_count",
    "staged_partition_count",
    "partitions_compared",
    "missing_staged_partition_count",
    "missing_freshness_partition_count",
    "lagging_partition_count",
    "max_offset_gap",
    "average_offset_gap",
    "lag_boundary_column",
    "max_lag_seconds",
    "average_lag_seconds",
];
const SCALAR_SUMMARY_FIELDS: [&str; 5] = [
    "active_min_value",
    "active_max_value",
    "staged_min_value",
    "staged_max_value",
    "lag_seconds",
];

fn root_audit_result<'py>(
    py: Python<'py>,
    root_key: &Value<'py>,
    request: &Value<'py>,
    observation: &Value<'py>,
    thresholds: &Value<'py>,
) -> PyResult<Value<'py>> {
    let boundary_mode: Value<'py> = observation.getattr("replay_boundary_mode")?;
    let lineage: Value<'py> = if boundary_mode.is_none() {
        boundary_mode
    } else {
        class(py, DISCOVERY_TYPES, "ReplayLineageMode")?.call1((boundary_mode,))?
    };
    let offsets: Value<'py> = summary(
        py,
        observation.getattr("offset_summary")?,
        "OffsetCatchupSummary",
        &OFFSET_SUMMARY_FIELDS,
    )?;
    let scalar: Value<'py> = summary(
        py,
        observation.getattr("scalar_summary")?,
        "ScalarCatchupSummary",
        &SCALAR_SUMMARY_FIELDS,
    )?;
    let active_exists: Value<'py> = request.getattr("active_exists")?;
    let offset_lineage: bool =
        lineage.eq(class(py, DISCOVERY_TYPES, "ReplayLineageMode")?.getattr("OFFSETS")?)?;
    let offset_argument = if offsets.is_none() {
        None
    } else {
        Some((
            offsets.getattr("active_partition_count")?.extract()?,
            offsets.getattr("partitions_compared")?.extract()?,
            offsets
                .getattr("missing_staged_partition_count")?
                .extract()?,
            offsets
                .getattr("missing_freshness_partition_count")?
                .extract()?,
            !offsets.getattr("lag_boundary_column")?.is_none(),
            offsets.getattr("max_lag_seconds")?.extract()?,
        ))
    };
    let scalar_lag: Option<f64> = if scalar.is_none() {
        None
    } else {
        scalar.getattr("lag_seconds")?.extract()?
    };
    let (assessment, row_delta, row_ratio, warnings) = audit_readiness(
        (
            root_key.getattr("name")?.extract()?,
            observation.getattr("staged_exists")?.extract()?,
            active_exists.extract()?,
            observation.getattr("active_row_count")?.extract()?,
            observation.getattr("staged_row_count")?.extract()?,
            offset_lineage,
            offset_argument,
            scalar_lag,
            observation.getattr("replay_source_name")?.extract()?,
            observation.getattr("replay_source_row_count")?.extract()?,
        ),
        (
            thresholds.getattr("maximum_lag_seconds")?.extract()?,
            thresholds.getattr("minimum_staged_row_ratio")?.extract()?,
        ),
    );
    let state_kind: Value<'py> = class(py, PLANNER_TYPES, "RootDeploymentStateKind")?;
    let state: Value<'py> = state_kind.getattr(if active_exists.is_truthy()? {
        "ACTIVE_VIEW_PRESENT"
    } else {
        "GREENFIELD"
    })?;
    build(
        py,
        READINESS_MODELS,
        "RootAuditResult",
        &[
            ("root_key", root_key.clone()),
            (
                "staged_physical_name",
                request.getattr("staged_relation_name")?,
            ),
            ("state", state),
            (
                "replay_source_name",
                observation.getattr("replay_source_name")?,
            ),
            (
                "replay_source_row_count",
                observation.getattr("replay_source_row_count")?,
            ),
            ("staged_exists", observation.getattr("staged_exists")?),
            ("active_exists", active_exists),
            ("active_row_count", observation.getattr("active_row_count")?),
            ("staged_row_count", observation.getattr("staged_row_count")?),
            ("row_delta", row_delta.into_pyobject(py)?.into_any()),
            ("row_ratio", row_ratio.into_pyobject(py)?.into_any()),
            (
                "assessment",
                class(py, READINESS_TYPES, "AuditAssessment")?.call1((assessment,))?,
            ),
            ("replay_lineage_mode", lineage),
            ("offset_catchup_summary", offsets),
            ("scalar_catchup_summary", scalar),
            ("warnings", PyTuple::new(py, warnings)?.into_any()),
        ],
    )
}

/// Audit decisions for each adapter-observed staged root.
#[pyfunction]
#[pyo3(signature = (
    *, client, default_database, inspected_state, root_keys, prepared_object_mappings, thresholds
))]
fn build_root_audit_results<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    default_database: &Value<'py>,
    inspected_state: &Value<'py>,
    root_keys: &Value<'py>,
    prepared_object_mappings: &Value<'py>,
    thresholds: &Value<'py>,
) -> PyResult<Value<'py>> {
    let physical_name_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for mapping in items(prepared_object_mappings)? {
        physical_name_by_key.set_item(mapping.get_item(0)?, mapping.get_item(1)?)?;
    }
    let table: Value<'py> = object_type(py, "DESIRED_OBJECT_TYPE_TABLE")?;
    let staged_name_by_root: Bound<'py, PyDict> = PyDict::new(py);
    for root_key in items(root_keys)? {
        if root_key.getattr("object_type")?.eq(&table)?
            && physical_name_by_key.contains(&root_key)?
        {
            staged_name_by_root.set_item(
                &root_key,
                physical_name_by_key.as_any().get_item(&root_key)?,
            )?;
        }
    }
    let mut ordered: Vec<((String, String, String), Value<'py>)> = Vec::new();
    for root_key in staged_name_by_root.keys() {
        let database: Value<'py> = root_key.getattr("database")?;
        let database: String = if database.is_truthy()? {
            database.extract()?
        } else {
            String::new()
        };
        ordered.push((
            (
                database,
                root_key.getattr("object_type")?.extract()?,
                root_key.getattr("name")?.extract()?,
            ),
            root_key,
        ));
    }
    ordered.sort_by(|left, right| left.0.cmp(&right.0));
    let active_bindings: Vec<Value<'py>> = items(&inspected_state.getattr("active_bindings")?)?;
    let mut requests: Vec<Value<'py>> = Vec::new();
    for (_, root_key) in &ordered {
        let name: Value<'py> = root_key.getattr("name")?;
        let root_database: Value<'py> = root_key.getattr("database")?;
        let mut active_exists: bool = false;
        for binding in &active_bindings {
            let binding_database: Value<'py> = binding.getattr("database")?;
            if binding_database.eq(or(root_database.clone(), &binding_database)?)?
                && binding.getattr("logical_name")?.eq(&name)?
            {
                active_exists = true;
                break;
            }
        }
        requests.push(build(
            py,
            ADAPTER_MODELS,
            "AdapterReadinessRootRequest",
            &[
                ("database", or(root_database, default_database)?),
                ("logical_name", name),
                (
                    "staged_relation_name",
                    staged_name_by_root.as_any().get_item(root_key)?,
                ),
                ("active_exists", boolean(py, active_exists)?),
            ],
        )?);
    }
    let requests_tuple: Value<'py> = tuple(py, requests.clone())?;
    let observations: Vec<Value<'py>> = items(&client.call_method1(
        "compare_readiness",
        (build(
            py,
            ADAPTER_MODELS,
            "AdapterReadinessRequest",
            &[("roots", requests_tuple)],
        )?,),
    )?)?;
    let observation_by_root: Bound<'py, PyDict> = PyDict::new(py);
    for observation in &observations {
        observation_by_root.set_item(observation.getattr("root")?, observation)?;
    }
    let result_error = |message: &str| {
        error(
            py,
            ADAPTER_EXCEPTIONS,
            "AdapterResultError",
            message.to_owned(),
        )
    };
    if observation_by_root.len() != observations.len() {
        return Err(result_error(
            "Adapter readiness returned duplicate root observations",
        ));
    }
    let observed: Bound<'py, PySet> = PySet::new(py, observation_by_root.keys())?;
    let requested: Bound<'py, PySet> = PySet::new(py, &requests)?;
    if observed.as_any().ne(&requested)? {
        return Err(result_error(
            "Adapter readiness observations did not match requested roots",
        ));
    }
    let mut results: Vec<Value<'py>> = Vec::new();
    for ((_, root_key), request) in ordered.iter().zip(&requests) {
        let observation: Value<'py> = observation_by_root.as_any().get_item(request)?;
        results.push(root_audit_result(
            py,
            root_key,
            request,
            &observation,
            thresholds,
        )?);
    }
    tuple(py, results)
}

/// Audit a staged backfill deployment, resolving it when no deployment ID is given.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn execute_deployment_audit<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<Value<'py>> {
    let metadata_database: Value<'py> = request.getattr("metadata_database")?;
    let default_database: Value<'py> = request.getattr("default_database")?;
    let mut deployment_id: Value<'py> = request.getattr("deployment_id")?;
    if deployment_id.is_none() {
        let candidates: Value<'py> =
            deployment_candidates(py, client, &metadata_database, &default_database, false)?;
        deployment_id = resolve_candidate(py, &candidates, "audit", audit_error)?;
    }
    let loaded: Value<'py> = load_audit_deployment(py, client, &metadata_database, &deployment_id)?;
    let inspected: Value<'py> =
        client.call_method1("inspect_managed_table_state", (&default_database,))?;
    let mut root_keys: Value<'py> = loaded.getattr("root_keys")?;
    let mut mappings: Value<'py> = loaded.getattr("prepared_object_mappings")?;
    if !mappings.is_truthy()? {
        let table: Value<'py> = object_type(py, "DESIRED_OBJECT_TYPE_TABLE")?;
        let mut inspected_mappings: Vec<Value<'py>> = Vec::new();
        let mut inspected_keys: Vec<Value<'py>> = Vec::new();
        for candidate in items(&inspected.getattr("physical_candidates")?)? {
            if !candidate.getattr("object_type")?.eq(&table)? {
                continue;
            }
            let physical_name: Value<'py> = candidate.getattr("physical_name")?;
            if !deployment_id.eq(deployment_id_of(&physical_name.extract::<String>()?)?)? {
                continue;
            }
            let key: Value<'py> = build(
                py,
                COMPILE_MODELS,
                "ObjectKey",
                &[
                    ("database", candidate.getattr("database")?),
                    ("object_type", table.clone()),
                    ("name", candidate.getattr("logical_name")?),
                ],
            )?;
            inspected_keys.push(key.clone());
            inspected_mappings.push(tuple(py, vec![key, physical_name])?);
        }
        mappings = tuple(py, inspected_mappings)?;
        root_keys = tuple(py, inspected_keys)?;
    }
    let thresholds: Value<'py> = request.getattr("thresholds")?;
    let root_results: Value<'py> = build_root_audit_results(
        py,
        client,
        &default_database,
        &inspected,
        &root_keys,
        &mappings,
        &thresholds,
    )?;
    let assessment_class: Value<'py> = class(py, READINESS_TYPES, "AuditAssessment")?;
    let mut overall: &str = "READY";
    for level in ["NOT_READY", "CAUTION"] {
        let wanted: Value<'py> = assessment_class.getattr(level)?;
        let mut found: bool = false;
        for result in items(&root_results)? {
            if result.getattr("assessment")?.eq(&wanted)? {
                found = true;
                break;
            }
        }
        if found {
            overall = level;
            break;
        }
    }
    build(
        py,
        READINESS_MODELS,
        "DeploymentAuditResult",
        &[
            ("deployment_id", deployment_id),
            ("deployment_status", loaded.getattr("status")?),
            (
                "assessment",
                assessment_class.call1((assessment_class.getattr(overall)?,))?,
            ),
            (
                "replay_lineage_mode",
                loaded.getattr("replay_lineage_mode")?,
            ),
            ("warning_codes", loaded.getattr("warning_codes")?),
            ("root_results", root_results),
            ("thresholds", thresholds),
        ],
    )
}

/// `AdapterStableBinding`s for `relations`, or `PublishExecutionError` when the native
/// planner refuses them.
fn publish_bindings<'py>(
    py: Python<'py>,
    relations: Vec<(Option<String>, String, String, String)>,
    existing: &Value<'py>,
    default_database: &str,
    deployment_id: &str,
) -> PyResult<Value<'py>> {
    let existing: HashSet<String> = items(existing)?
        .iter()
        .map(|name| name.extract())
        .collect::<PyResult<_>>()?;
    let bindings =
        match publish_stable_bindings(relations, existing, default_database, deployment_id) {
            Ok(bindings) => bindings,
            Err(native) => {
                let message: String = native.value(py).str()?.extract()?;
                return Err(publish_error_from_none(py, message, &native)?);
            }
        };
    stable_bindings(py, bindings)
}

fn stable_bindings<'py>(
    py: Python<'py>,
    bindings: Vec<(String, String, String)>,
) -> PyResult<Value<'py>> {
    tuple(
        py,
        bindings
            .into_iter()
            .map(|(database, logical_name, physical_name)| {
                build(
                    py,
                    ADAPTER_MODELS,
                    "AdapterStableBinding",
                    &[
                        ("database", text(py, &database)),
                        ("logical_name", text(py, &logical_name)),
                        ("physical_name", text(py, &physical_name)),
                    ],
                )
            })
            .collect::<PyResult<_>>()?,
    )
}

/// `(database or None, publishes, logical name, physical name, logical model name)` mappings.
fn native_mappings(mappings: &Value<'_>) -> PyResult<Vec<MappingArgument>> {
    items(mappings)?
        .iter()
        .map(|mapping| {
            let key: Value<'_> = mapping.getattr("logical_key")?;
            Ok((
                or_none(key.getattr("database")?)?.extract()?,
                publishes(&key.getattr("object_type")?)?,
                key.getattr("name")?.extract()?,
                mapping.getattr("physical_name")?.extract()?,
                mapping.getattr("logical_model_name")?.extract()?,
            ))
        })
        .collect()
}

/// Validated stable bindings, and the stale bindings they retire, for publishing a staged
/// deployment.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database, deployment_id, inspected_state = None))]
pub(crate) fn build_publish_binding_request<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
    deployment_id: &Value<'py>,
    inspected_state: Option<Value<'py>>,
) -> PyResult<Value<'py>> {
    let inspected_state: Option<Value<'py>> = inspected_state.filter(|state| !state.is_none());
    let managed_state = || -> PyResult<Value<'py>> {
        match &inspected_state {
            Some(state) => Ok(state.clone()),
            None => client.call_method1("inspect_managed_table_state", (default_database,)),
        }
    };
    let default_text: String = default_database.extract()?;
    let deployment_text: String = deployment_id.extract()?;
    let inventory: Value<'py> =
        client.call_method1("load_deployment_inventory", (metadata_database,))?;
    let deployments: Vec<Value<'py>> = items(&inventory.getattr("deployments")?)?;
    let mut found: Option<&Value<'py>> = None;
    for candidate in &deployments {
        if candidate.getattr("deployment_id")?.eq(deployment_id)? {
            found = Some(candidate);
            break;
        }
    }
    let Some(deployment) = found else {
        let state: Value<'py> = managed_state()?;
        let candidates: Vec<Value<'py>> = items(&state.getattr("physical_candidates")?)?;
        let native_candidates: Vec<(String, String)> = candidates
            .iter()
            .map(|candidate| {
                Ok((
                    candidate.getattr("physical_name")?.extract()?,
                    candidate.getattr("object_type")?.extract()?,
                ))
            })
            .collect::<PyResult<_>>()?;
        let positions: Vec<usize> =
            match inspected_publish_candidates(native_candidates, &deployment_text) {
                Ok(positions) => positions,
                Err(native) => {
                    let message: String = native.value(py).str()?.extract()?;
                    return Err(publish_error_from_none(py, message, &native)?);
                }
            };
        let existing: Value<'py> = client
            .call_method1("load_catalog", (default_database,))?
            .call_method0("relation_names")?;
        let relations: Vec<(Option<String>, String, String, String)> = positions
            .iter()
            .map(|position| {
                let candidate: &Value<'py> = &candidates[*position];
                Ok((
                    candidate.getattr("database")?.extract()?,
                    candidate.getattr("object_type")?.extract()?,
                    candidate.getattr("logical_name")?.extract()?,
                    candidate.getattr("physical_name")?.extract()?,
                ))
            })
            .collect::<PyResult<_>>()?;
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterBindingReplacementRequest",
            &[(
                "bindings",
                publish_bindings(py, relations, &existing, &default_text, &deployment_text)?,
            )],
        );
    };
    let incomplete: Value<'py> = incomplete_status(py)?;
    if deployment.getattr("status")?.eq(&incomplete)? {
        return Err(publish_error(
            py,
            format!("Deployment '{deployment_text}' is incomplete and cannot be published"),
        ));
    }
    let existing: Value<'py> = client
        .call_method1("load_catalog", (default_database,))?
        .call_method0("relation_names")?;
    let mappings: Value<'py> = deployment.getattr("prepared_object_mappings")?;
    let relations: Vec<(Option<String>, String, String, String)> = items(&mappings)?
        .iter()
        .map(|mapping| {
            let key: Value<'py> = mapping.getattr("logical_key")?;
            Ok((
                or_none(key.getattr("database")?)?.extract()?,
                key.getattr("object_type")?.extract()?,
                key.getattr("name")?.extract()?,
                mapping.getattr("physical_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let bindings: Value<'py> =
        publish_bindings(py, relations, &existing, &default_text, &deployment_text)?;
    let state: Value<'py> = managed_state()?;
    let native_deployments = deployments
        .iter()
        .map(|candidate| {
            Ok((
                candidate.getattr("deployment_id")?.extract()?,
                candidate.getattr("created_at")?.extract()?,
                candidate.getattr("status")?.eq(&incomplete)?,
                native_mappings(&candidate.getattr("prepared_object_mappings")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let publications: Vec<(String, Vec<String>)> = items(&inventory.getattr("publish_events")?)?
        .iter()
        .map(|event| {
            Ok((
                event.getattr("deployment_id")?.extract()?,
                items(&event.getattr("logical_view_names")?)?
                    .iter()
                    .map(|name| name.extract())
                    .collect::<PyResult<_>>()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let removals: Vec<(String, String)> = obsolete_binding_removals(
        native_mappings(&mappings)?,
        native_deployments,
        publications,
        binding_triples(&state.getattr("active_bindings")?)?,
        &default_text,
    );
    let removals: Vec<Value<'py>> = removals
        .into_iter()
        .map(|(database, logical_name)| {
            build(
                py,
                ADAPTER_MODELS,
                "AdapterStableBindingRemoval",
                &[
                    ("database", text(py, &database)),
                    ("logical_name", text(py, &logical_name)),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterBindingReplacementRequest",
        &[("bindings", bindings), ("removals", tuple(py, removals)?)],
    )
}

/// The exact live-binding effects of an executable replacement request.
#[pyfunction]
#[pyo3(signature = (*, binding_request, active_bindings))]
fn build_binding_replacement_preview<'py>(
    py: Python<'py>,
    binding_request: &Value<'py>,
    active_bindings: &Value<'py>,
) -> PyResult<Value<'py>> {
    let bindings: Vec<Value<'py>> = items(&binding_request.getattr("bindings")?)?;
    let removals: Vec<Value<'py>> = items(&binding_request.getattr("removals")?)?;
    let native_removals: Vec<(String, String)> = removals
        .iter()
        .map(|removal| {
            Ok((
                removal.getattr("database")?.extract()?,
                removal.getattr("logical_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let (initial_publish, additions, replacements, removed, orphaned) = binding_replacement_preview(
        binding_triples(&binding_request.getattr("bindings")?)?,
        native_removals,
        binding_triples(active_bindings)?,
    );
    let classification: Value<'py> = class(py, PROMOTION_TYPES, "PromotionPreviewClassification")?
        .getattr(if initial_publish {
            "INITIAL_PUBLISH"
        } else {
            "PROMOTION"
        })?;
    let field = |value: &Value<'py>, name: &str| value.getattr(name);
    let additions: Vec<Value<'py>> = additions
        .into_iter()
        .map(|position| {
            let binding: &Value<'py> = &bindings[position];
            build(
                py,
                PROMOTION_MODELS,
                "PromotionBindingAddition",
                &[
                    ("database", field(binding, "database")?),
                    ("logical_name", field(binding, "logical_name")?),
                    ("physical_name", field(binding, "physical_name")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let replacements: Vec<Value<'py>> = replacements
        .into_iter()
        .map(|(position, from_physical_name)| {
            let binding: &Value<'py> = &bindings[position];
            build(
                py,
                PROMOTION_MODELS,
                "PromotionBindingReplacement",
                &[
                    ("database", field(binding, "database")?),
                    ("logical_name", field(binding, "logical_name")?),
                    ("from_physical_name", text(py, &from_physical_name)),
                    ("to_physical_name", field(binding, "physical_name")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let removed: Vec<Value<'py>> = removed
        .into_iter()
        .map(|(position, physical_name)| {
            let removal: &Value<'py> = &removals[position];
            build(
                py,
                PROMOTION_MODELS,
                "PromotionBindingRemoval",
                &[
                    ("database", field(removal, "database")?),
                    ("logical_name", field(removal, "logical_name")?),
                    ("physical_name", text(py, &physical_name)),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let orphaned: Vec<Value<'py>> = orphaned
        .into_iter()
        .map(|(database, physical_name)| {
            build(
                py,
                PROMOTION_MODELS,
                "PromotionOrphanedRelation",
                &[
                    ("database", text(py, &database)),
                    ("physical_name", text(py, &physical_name)),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        PROMOTION_MODELS,
        "DeploymentPromotionPreview",
        &[
            ("classification", classification),
            ("additions", tuple(py, additions)?),
            ("replacements", tuple(py, replacements)?),
            ("removals", tuple(py, removed)?),
            ("orphaned_relations", tuple(py, orphaned)?),
        ],
    )
}

/// Promotion effects from the same binding request execution uses.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database, deployment_id, inspected_state))]
fn build_deployment_promotion_preview<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    metadata_database: &Value<'py>,
    default_database: &Value<'py>,
    deployment_id: &Value<'py>,
    inspected_state: Value<'py>,
) -> PyResult<Value<'py>> {
    let active_bindings: Value<'py> = inspected_state.getattr("active_bindings")?;
    let request: Value<'py> = build_publish_binding_request(
        py,
        client,
        metadata_database,
        default_database,
        deployment_id,
        Some(inspected_state),
    )?;
    build_binding_replacement_preview(py, &request, &active_bindings)
}

/// `(published at, rank ID)` of one publication: its ISO timestamp, read as UTC when naive,
/// and its publication ID or deployment ID.
fn event_rank<'py>(py: Python<'py>, event: &Value<'py>) -> PyResult<(Value<'py>, Value<'py>)> {
    let datetime_module: Bound<'py, PyModule> = py.import("datetime")?;
    let iso: Value<'py> = event
        .getattr("published_at")?
        .call_method1("replace", (" ", "T"))?;
    let mut published_at: Value<'py> = datetime_module
        .getattr("datetime")?
        .call_method1("fromisoformat", (iso,))?;
    if published_at.getattr("tzinfo")?.is_none() {
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("tzinfo", datetime_module.getattr("UTC")?)?;
        published_at = published_at.call_method("replace", (), Some(&keywords))?;
    }
    let publication_id: Value<'py> = event.getattr("publication_id")?;
    let rank_id: Value<'py> = if publication_id.is_truthy()? {
        publication_id
    } else {
        event.getattr("deployment_id")?
    };
    Ok((published_at, rank_id))
}

/// `(microseconds since the epoch, rank ID)`, or `None` for an unparseable timestamp.
fn native_event_rank(py: Python<'_>, event: &Value<'_>) -> PyResult<Option<(i64, String)>> {
    let (published_at, rank_id) = match event_rank(py, event) {
        Ok(rank) => rank,
        Err(error) if error.is_instance_of::<PyValueError>(py) => return Ok(None),
        Err(error) => return Err(error),
    };
    let datetime_module: Bound<'_, PyModule> = py.import("datetime")?;
    let epoch: Value<'_> = datetime_module.getattr("datetime")?.call(
        (1970, 1, 1),
        Some(&{
            let keywords: Bound<'_, PyDict> = PyDict::new(py);
            keywords.set_item("tzinfo", datetime_module.getattr("UTC")?)?;
            keywords
        }),
    )?;
    let microsecond: Value<'_> = {
        let keywords: Bound<'_, PyDict> = PyDict::new(py);
        keywords.set_item("microseconds", 1)?;
        datetime_module
            .getattr("timedelta")?
            .call((), Some(&keywords))?
    };
    let micros: i64 = published_at.sub(epoch)?.floor_div(microsecond)?.extract()?;
    Ok(Some((micros, rank_id.extract()?)))
}

/// The active publication and the retained rollback target the request names.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn resolve_deployment_rollback<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<Value<'py>> {
    let default_database: Value<'py> = request.getattr("default_database")?;
    let inventory: Value<'py> = client.call_method1(
        "load_deployment_inventory",
        (request.getattr("metadata_database")?,),
    )?;
    let managed_state: Value<'py> =
        client.call_method1("inspect_managed_table_state", (&default_database,))?;
    let events: Vec<Value<'py>> = items(&inventory.getattr("publish_events")?)?;
    let deployment_id: Option<String> = if request.getattr("previous")?.is_truthy()? {
        None
    } else {
        Some(or(request.getattr("deployment_id")?, &text(py, ""))?.extract()?)
    };
    let publications = events
        .iter()
        .map(|event| {
            Ok((
                event.getattr("deployment_id")?.extract()?,
                native_event_rank(py, event)?,
                binding_triples(&event.getattr("bindings")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let active = binding_triples(&managed_state.getattr("active_bindings")?)?;
    let (current, target) = match select_rollback_publications(publications, active, deployment_id)
    {
        Ok(selected) => selected,
        Err(native) => {
            let arguments: Value<'py> = native.value(py).getattr("args")?;
            let code: String = arguments.get_item(0)?.extract()?;
            let detail: Value<'py> = arguments.get_item(1)?;
            if code == "unranked" {
                let position: usize = detail.extract()?;
                if let Err(parse) = event_rank(py, &events[position]) {
                    let raised: Value<'py> = parse.value(py).clone().into_any();
                    if raised.getattr("__context__")?.is_none() {
                        raised.setattr("__context__", native.value(py))?;
                    }
                    return Err(parse);
                }
            }
            return Err(publish_error_from_none(
                py,
                detail.str()?.extract()?,
                &native,
            )?);
        }
    };
    let current_event: &Value<'py> = &events[current];
    let target_event: &Value<'py> = &events[target];
    let target_id: Value<'py> = target_event.getattr("deployment_id")?;
    let target_text: String = target_id.str()?.extract()?;
    let deployment_by_id: Bound<'py, PyDict> = PyDict::new(py);
    for deployment in items(&inventory.getattr("deployments")?)? {
        deployment_by_id.set_item(deployment.getattr("deployment_id")?, deployment)?;
    }
    let Some(target_deployment) = deployment_by_id.get_item(&target_id)? else {
        return Err(publish_error(
            py,
            format!("Rollback target deployment '{target_text}' is missing deployment metadata"),
        ));
    };
    let mappings: Vec<Value<'py>> = items(&target_deployment.getattr("prepared_object_mappings")?)?;
    let mapping_database = |mapping: &Value<'py>| {
        or(
            mapping.getattr("logical_key")?.getattr("database")?,
            &default_database,
        )
    };
    let identity = |triples: Vec<Value<'py>>| -> PyResult<Value<'py>> {
        let sorted: Value<'py> = py
            .import("builtins")?
            .getattr("sorted")?
            .call1((PyList::new(py, triples)?,))?;
        py.import("builtins")?.getattr("tuple")?.call1((sorted,))
    };
    let mut expected: Vec<Value<'py>> = Vec::new();
    for mapping in &mappings {
        let key: Value<'py> = mapping.getattr("logical_key")?;
        if publishes(&key.getattr("object_type")?)? {
            expected.push(tuple(
                py,
                vec![
                    mapping_database(mapping)?,
                    key.getattr("name")?,
                    mapping.getattr("physical_name")?,
                ],
            )?);
        }
    }
    let recorded: Vec<Value<'py>> = items(&target_event.getattr("bindings")?)?
        .iter()
        .map(|binding| {
            tuple(
                py,
                vec![
                    binding.getattr("database")?,
                    binding.getattr("logical_name")?,
                    binding.getattr("physical_name")?,
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    if identity(recorded)?.ne(identity(expected)?)? {
        return Err(publish_error(
            py,
            format!(
                "Rollback target deployment '{target_text}' publication bindings do not match \
                 its deployment metadata"
            ),
        ));
    }
    let mut databases: BTreeSet<String> = BTreeSet::new();
    for mapping in &mappings {
        databases.insert(mapping_database(mapping)?.extract()?);
    }
    let catalogs: Bound<'py, PyDict> = PyDict::new(py);
    for database in &databases {
        catalogs.set_item(database, client.call_method1("load_catalog", (database,))?)?;
    }
    let mut missing: Vec<String> = Vec::new();
    for mapping in &mappings {
        let database: Value<'py> = mapping_database(mapping)?;
        let physical_name: Value<'py> = mapping.getattr("physical_name")?;
        let names: Value<'py> = catalogs
            .as_any()
            .get_item(&database)?
            .call_method0("relation_names")?;
        if !names.contains(&physical_name)? {
            missing.push(format!("{}.{}", database.str()?, physical_name.str()?));
        }
    }
    if !missing.is_empty() {
        missing.sort();
        return Err(publish_error(
            py,
            format!(
                "Rollback target deployment '{target_text}' is missing retained relations: {}",
                missing.join(", ")
            ),
        ));
    }
    let mut view_names: Vec<String> = Vec::new();
    for mapping in &mappings {
        let key: Value<'py> = mapping.getattr("logical_key")?;
        if publishes(&key.getattr("object_type")?)? {
            view_names.push(key.getattr("name")?.extract()?);
        }
    }
    view_names.sort();
    build(
        py,
        PROMOTION_MODELS,
        "RollbackPlan",
        &[
            (
                "current_deployment_id",
                current_event.getattr("deployment_id")?,
            ),
            ("target_deployment_id", target_id),
            (
                "logical_view_names",
                PyTuple::new(py, view_names)?.into_any(),
            ),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_audit_deployment_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(
        build_publish_deployment_candidates,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(resolve_publish_deployment_id, module)?)?;
    module.add_function(wrap_pyfunction!(load_audit_deployment, module)?)?;
    module.add_function(wrap_pyfunction!(build_root_audit_results, module)?)?;
    module.add_function(wrap_pyfunction!(execute_deployment_audit, module)?)?;
    module.add_function(wrap_pyfunction!(build_publish_binding_request, module)?)?;
    module.add_function(wrap_pyfunction!(build_binding_replacement_preview, module)?)?;
    module.add_function(wrap_pyfunction!(
        build_deployment_promotion_preview,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(resolve_deployment_rollback, module)?)
}
