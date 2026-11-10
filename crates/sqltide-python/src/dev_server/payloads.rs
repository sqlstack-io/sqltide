//! UI payloads built from compiled analyses and warehouse reads.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::support::attr;
use crate::planner_support::{deployment_id_from_physical_name, logical_name_from_physical_name};
use crate::py_kwargs;

fn list_of<'py>(py: Python<'py>, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyList::new(py, items)?.into_any())
}

fn as_list<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyList::new(value.py(), value.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any())
}

fn sorted<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attr(value.py(), "builtins", "sorted")?.call1((value,))
}

fn optional<'py>(
    value: Bound<'py, PyAny>,
    map: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        Ok(value)
    } else {
        map(&value)
    }
}

// Sensors.

fn tick_payload<'py>(tick: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = tick.py();
    Ok(py_kwargs!(py;
        tickId = tick.getattr("tick_id")?,
        sensorName = tick.getattr("sensor_name")?,
        definitionFingerprint = tick.getattr("definition_fingerprint")?,
        kind = tick.getattr("kind")?,
        eventId = tick.getattr("event_id")?,
        eventKind = tick.getattr("event_kind")?,
        attempt = tick.getattr("attempt")?,
        status = tick.getattr("status")?,
        startedAt = tick.getattr("started_at")?,
        completedAt = tick.getattr("completed_at")?,
        errorMessage = tick.getattr("error_message")?,
        skipReason = tick.getattr("skip_reason")?,
        cursor = tick.getattr("cursor")?,
    )?
    .into_any())
}

fn ticks_payload<'py>(py: Python<'py>, ticks: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let items: Vec<Bound<'py, PyAny>> = ticks
        .try_iter()?
        .map(|tick| tick_payload(&tick?))
        .collect::<PyResult<_>>()?;
    list_of(py, items)
}

fn sensor_payload<'py>(
    sensor: &Bound<'py, PyAny>,
    overrides: &Bound<'py, PyAny>,
    repository: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = sensor.py();
    let declaration = sensor.getattr("declaration")?;
    let latest = if repository.is_none() {
        None
    } else {
        let ticks = repository.call_method(
            "list_ticks",
            (),
            Some(&py_kwargs!(py; sensor_name = sensor.getattr("name")?, limit = 1)?),
        )?;
        let count: usize = ticks.len()?;
        if count == 0 {
            None
        } else {
            Some(ticks.get_item(count - 1)?)
        }
    };
    let effective = attr(
        py,
        "sqltide.sensors._helpers.dispatch",
        "effective_sensor_status",
    )?
    .call(
        (),
        Some(&py_kwargs!(py; sensor = sensor, overrides = overrides)?),
    )?;
    let override_value = overrides.call_method1("get", (sensor.getattr("name")?,))?;
    let declared_in_code =
        attr(py, "sqltide.sensors.types", "SensorOverrideStatus")?.getattr("DECLARED_IN_CODE")?;
    let override_payload: Option<String> =
        if override_value.is_none() || override_value.is(&declared_in_code) {
            None
        } else {
            Some(override_value.str()?.to_string())
        };
    let retry = sensor.getattr("retry_policy")?;
    let payload: Bound<'py, PyDict> = py_kwargs!(py;
        name = sensor.getattr("name")?,
        kind = sensor.getattr("kind")?.str()?,
        description = sensor.getattr("description")?,
        file = sensor.getattr("relative_path")?.call_method0("as_posix")?,
        fingerprint = sensor.getattr("identity_fingerprint")?,
        defaultStatus = sensor.getattr("default_status")?.str()?,
        effectiveStatus = effective.str()?,
        override = override_payload,
        retryPolicy = py_kwargs!(py;
            maxAttempts = retry.getattr("max_attempts")?,
            backoffSeconds = retry.getattr("backoff_seconds")?,
        )?,
        timeoutSeconds = sensor.getattr("timeout_seconds")?,
        lastTick = match latest {
            Some(tick) => tick_payload(&tick)?,
            None => py.None().into_bound(py),
        },
    )?;
    let models: &str = "sqltide.sensors.models";
    if declaration.is_instance(&attr(py, models, "EventSensorDeclaration")?)? {
        payload.set_item(
            "eventType",
            declaration.getattr("event_type")?.getattr("__name__")?,
        )?;
        payload.set_item(
            "targets",
            optional(declaration.getattr("targets")?, sorted)?,
        )?;
        payload.set_item(
            "triggers",
            optional(declaration.getattr("triggers")?, sorted)?,
        )?;
    }
    if declaration.is_instance(&attr(py, models, "PollingSensorDeclaration")?)? {
        payload.set_item(
            "minimumIntervalSeconds",
            declaration.getattr("minimum_interval_seconds")?,
        )?;
    }
    Ok(payload.into_any())
}

/// List compiled sensors with effective statuses and their latest tick.
#[pyfunction]
#[pyo3(signature = (*, analysis, repository, health))]
pub(crate) fn build_sensors_payload<'py>(
    py: Python<'py>,
    analysis: Bound<'py, PyAny>,
    repository: Bound<'py, PyAny>,
    health: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let compiled = analysis.getattr("sensors")?;
    let sensors: Vec<Bound<'py, PyAny>> = if compiled.is_none() {
        Vec::new()
    } else {
        compiled
            .getattr("registry")?
            .call_method0("ordered")?
            .try_iter()?
            .collect::<PyResult<_>>()?
    };
    let mut overrides: Bound<'py, PyAny> = PyDict::new(py).into_any();
    let mut dead_letter_count: usize = 0;
    if !repository.is_none() && !sensors.is_empty() {
        repository.call_method0("ensure_ready")?;
        overrides = repository.call_method0("override_statuses")?;
        dead_letter_count = repository.call_method0("list_dead_letters")?.len()?;
    }
    let payloads: Vec<Bound<'py, PyAny>> = sensors
        .iter()
        .map(|sensor| sensor_payload(sensor, &overrides, &repository))
        .collect::<PyResult<_>>()?;
    Ok(py_kwargs!(py;
        sensors = list_of(py, payloads)?,
        deadLetterCount = dead_letter_count,
        health = health,
    )?
    .into_any())
}

/// List recent ticks for one sensor, newest first, optionally time-windowed.
#[pyfunction]
#[pyo3(signature = (*, repository, sensor_name, limit, after = None, before = None))]
pub(crate) fn build_sensor_ticks_payload<'py>(
    py: Python<'py>,
    repository: Bound<'py, PyAny>,
    sensor_name: Bound<'py, PyAny>,
    limit: Bound<'py, PyAny>,
    after: Option<Bound<'py, PyAny>>,
    before: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    if repository.is_none() {
        return Ok(py_kwargs!(py; sensorName = sensor_name, ticks = PyList::empty(py))?.into_any());
    }
    repository.call_method0("ensure_ready")?;
    let ticks = repository.call_method(
        "list_ticks",
        (),
        Some(&py_kwargs!(py; sensor_name = &sensor_name, limit = limit, after = after, before = before)?),
    )?;
    Ok(py_kwargs!(py; sensorName = sensor_name, ticks = ticks_payload(py, &ticks)?)?.into_any())
}

/// List unresolved dead-lettered events across all sensors.
#[pyfunction]
#[pyo3(signature = (*, repository))]
pub(crate) fn build_dead_letters_payload<'py>(
    py: Python<'py>,
    repository: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if repository.is_none() {
        return Ok(py_kwargs!(py; deadLetters = PyList::empty(py))?.into_any());
    }
    repository.call_method0("ensure_ready")?;
    let ticks = repository.call_method0("list_dead_letters")?;
    Ok(py_kwargs!(py; deadLetters = ticks_payload(py, &ticks)?)?.into_any())
}

// Deployments.

#[derive(Clone, Copy, Default)]
struct Storage {
    rows: i128,
    bytes: i128,
}

/// `int(value)` for ints and floats; anything else, including booleans, counts as zero.
fn storage_int(value: &Bound<'_, PyAny>) -> PyResult<i128> {
    if value.is_instance_of::<pyo3::types::PyBool>() {
        return Ok(0);
    }
    if value.is_instance_of::<pyo3::types::PyInt>()
        || value.is_instance_of::<pyo3::types::PyFloat>()
    {
        return attr(value.py(), "builtins", "int")?
            .call1((value,))?
            .extract();
    }
    Ok(0)
}

fn relation_storage<'py>(
    connection: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Vec<(String, Storage)>> {
    let query: String = super::state::build_relation_stats_query(database);
    let rows = connection
        .call_method1("query", (query,))?
        .call_method0("named_rows")?;
    let mut storage: Vec<(String, Storage)> = Vec::new();
    for row in rows.try_iter()? {
        let row = row?;
        let name: String = row.get_item("name")?.str()?.to_string();
        let entry = Storage {
            rows: storage_int(&row.get_item("total_rows")?)?,
            bytes: storage_int(&row.get_item("total_bytes")?)?,
        };
        // A later row for the same name replaces the earlier one, as a dict comprehension does.
        match storage.iter_mut().find(|(existing, _)| *existing == name) {
            Some(slot) => slot.1 = entry,
            None => storage.push((name, entry)),
        }
    }
    Ok(storage)
}

fn storage_of(storage: &[(String, Storage)], name: &str) -> Storage {
    storage
        .iter()
        .find(|(existing, _)| existing == name)
        .map(|(_, entry)| *entry)
        .unwrap_or_default()
}

fn storage_total(relation_names: &[String], storage: &[(String, Storage)]) -> Storage {
    relation_names
        .iter()
        .fold(Storage::default(), |total, name| {
            let entry: Storage = storage_of(storage, name);
            Storage {
                rows: total.rows + entry.rows,
                bytes: total.bytes + entry.bytes,
            }
        })
}

fn strings(value: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    value.try_iter()?.map(|item| item?.extract()).collect()
}

/// Deployment relations that receive a stable view; landing views are excluded.
fn model_relation_names(py: Python<'_>, relation_names: &[String]) -> PyResult<Vec<String>> {
    let prefix: String = attr(
        py,
        "sqltide.compiler.compile.constants",
        "MATERIALIZED_VIEW_NAME_PREFIX",
    )?
    .extract()?;
    Ok(relation_names
        .iter()
        .filter(|name| !logical_name_from_physical_name(name).starts_with(&prefix))
        .cloned()
        .collect())
}

fn load_deployments<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
    metadata_database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    attr(py, "sqltide.executor.deployment.main.load_deployments", "load_deployments")?.call(
        (),
        Some(&py_kwargs!(py; client = connection, metadata_database = metadata_database, default_database = database)?),
    )
}

fn summary_payload<'py>(
    deployment: &Bound<'py, PyAny>,
    storage: &[(String, Storage)],
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = deployment.py();
    let relation_names: Vec<String> = strings(&deployment.getattr("physical_relation_names")?)?;
    let totals: Storage = storage_total(&relation_names, storage);
    let model_relations: Vec<String> = model_relation_names(py, &relation_names)?;
    py_kwargs!(py;
        deploymentId = deployment.getattr("deployment_id")?,
        state = deployment.getattr("state")?.str()?,
        createdAt = deployment.getattr("created_at")?,
        publishedAt = deployment.getattr("latest_published_at")?,
        persistedStatus = deployment.getattr("persisted_status")?,
        rootNames = as_list(&deployment.getattr("root_names")?)?,
        physicalRelationNames = relation_names.clone(),
        activeBindingNames = as_list(&deployment.getattr("active_binding_names")?)?,
        missingRelationNames = as_list(&deployment.getattr("missing_physical_relation_names")?)?,
        modelCount = model_relations.len(),
        relationCount = relation_names.len(),
        rows = totals.rows,
        bytes = totals.bytes,
    )
}

/// Return every reconstructed deployment with storage totals for the UI.
#[pyfunction]
#[pyo3(signature = (*, connection, database, metadata_database))]
pub(crate) fn build_deployments_payload<'py>(
    py: Python<'py>,
    connection: Bound<'py, PyAny>,
    database: &str,
    metadata_database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let inventory = load_deployments(py, &connection, database, metadata_database)?;
    let storage: Vec<(String, Storage)> = relation_storage(&connection, database)?;
    let deployments: Vec<Bound<'py, PyAny>> = inventory
        .getattr("deployments")?
        .try_iter()?
        .map(|deployment| Ok(summary_payload(&deployment?, &storage)?.into_any()))
        .collect::<PyResult<_>>()?;
    Ok(py_kwargs!(py; database = inventory.getattr("database")?, deployments = list_of(py, deployments)?)?.into_any())
}

fn deployment_id_or_none(physical_name: Option<&str>) -> PyResult<Option<String>> {
    match physical_name {
        Some(name) if sqltide_planner::naming::is_deployment_physical_name(name) => {
            Ok(Some(deployment_id_from_physical_name(name)?))
        }
        _ => Ok(None),
    }
}

fn promotion_preview_payload<'py>(
    py: Python<'py>,
    preview: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let Some(preview) = preview else {
        return Ok(py.None().into_bound(py));
    };
    let mut additions: Vec<Bound<'py, PyAny>> = Vec::new();
    for addition in preview.getattr("additions")?.try_iter()? {
        let addition = addition?;
        additions.push(
            py_kwargs!(py;
                database = addition.getattr("database")?,
                logicalName = addition.getattr("logical_name")?,
                physicalName = addition.getattr("physical_name")?,
            )?
            .into_any(),
        );
    }
    let mut replacements: Vec<Bound<'py, PyAny>> = Vec::new();
    for replacement in preview.getattr("replacements")?.try_iter()? {
        let replacement = replacement?;
        replacements.push(
            py_kwargs!(py;
                database = replacement.getattr("database")?,
                logicalName = replacement.getattr("logical_name")?,
                fromPhysicalName = replacement.getattr("from_physical_name")?,
                toPhysicalName = replacement.getattr("to_physical_name")?,
            )?
            .into_any(),
        );
    }
    let mut removals: Vec<Bound<'py, PyAny>> = Vec::new();
    for removal in preview.getattr("removals")?.try_iter()? {
        let removal = removal?;
        removals.push(
            py_kwargs!(py;
                database = removal.getattr("database")?,
                logicalName = removal.getattr("logical_name")?,
                physicalName = removal.getattr("physical_name")?,
            )?
            .into_any(),
        );
    }
    Ok(py_kwargs!(py;
        classification = preview.getattr("classification")?.str()?,
        additions = list_of(py, additions)?,
        replacements = list_of(py, replacements)?,
        removals = list_of(py, removals)?,
    )?
    .into_any())
}

/// Return one deployment with per-model staged versus live comparison.
#[pyfunction]
#[pyo3(signature = (*, connection, database, metadata_database, deployment_id))]
pub(crate) fn build_deployment_detail_payload<'py>(
    py: Python<'py>,
    connection: Bound<'py, PyAny>,
    database: &str,
    metadata_database: &str,
    deployment_id: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let inventory = load_deployments(py, &connection, database, metadata_database)?;
    let mut found: Option<Bound<'py, PyAny>> = None;
    for item in inventory.getattr("deployments")?.try_iter()? {
        let item = item?;
        if item.getattr("deployment_id")?.extract::<String>()? == deployment_id {
            found = Some(item);
            break;
        }
    }
    let Some(deployment) = found else {
        return Ok(py.None().into_bound(py));
    };
    let storage: Vec<(String, Storage)> = relation_storage(&connection, database)?;
    let inspected = connection.call_method1("inspect_managed_table_state", (database,))?;
    let mut live_by_logical: Vec<(String, String)> = Vec::new();
    for binding in inspected.getattr("active_bindings")?.try_iter()? {
        let binding = binding?;
        let logical: String = binding.getattr("logical_name")?.extract()?;
        let physical: String = binding.getattr("physical_name")?.extract()?;
        match live_by_logical
            .iter_mut()
            .find(|(existing, _)| *existing == logical)
        {
            Some(slot) => slot.1 = physical,
            None => live_by_logical.push((logical, physical)),
        }
    }
    let relation_names: Vec<String> = strings(&deployment.getattr("physical_relation_names")?)?;
    let mut models: Vec<Bound<'py, PyAny>> = Vec::new();
    for staged_relation in model_relation_names(py, &relation_names)? {
        let logical_name: String = logical_name_from_physical_name(&staged_relation);
        let live_relation: Option<String> = live_by_logical
            .iter()
            .find(|(logical, _)| *logical == logical_name)
            .map(|(_, physical)| physical.clone());
        let staged: Storage = storage_of(&storage, &staged_relation);
        let live_rows: Option<i128> = live_relation
            .as_deref()
            .map(|name| storage_of(&storage, name).rows);
        models.push(
            py_kwargs!(py;
                logicalName = logical_name.as_str(),
                stagedRelation = staged_relation.as_str(),
                stagedRows = staged.rows,
                stagedBytes = staged.bytes,
                liveRelation = live_relation.clone(),
                liveDeploymentId = deployment_id_or_none(live_relation.as_deref())?,
                liveRows = live_rows,
                isActive = live_relation.as_deref() == Some(staged_relation.as_str()),
                isNew = live_relation.is_none(),
            )?
            .into_any(),
        );
    }
    let lifecycle = attr(
        py,
        "sqltide.executor.deployment.types",
        "DeploymentLifecycleState",
    )?;
    let state = deployment.getattr("state")?;
    let staged_state = lifecycle.getattr("STAGED")?;
    let mut promotion_preview: Option<Bound<'py, PyAny>> = None;
    if state.eq(&staged_state)? || state.eq(lifecycle.getattr("ACTIVE")?)? {
        let candidate = attr(py, "sqltide._native", "build_deployment_promotion_preview")?.call(
            (),
            Some(&py_kwargs!(py;
                client = &connection,
                metadata_database = metadata_database,
                default_database = database,
                deployment_id = deployment_id,
                inspected_state = &inspected,
            )?),
        )?;
        let changes: bool = candidate.getattr("additions")?.is_truthy()?
            || candidate.getattr("replacements")?.is_truthy()?
            || candidate.getattr("removals")?.is_truthy()?;
        if state.eq(&staged_state)? || changes {
            promotion_preview = Some(candidate);
        }
    }
    let orphaned: Vec<String> = match &promotion_preview {
        None => Vec::new(),
        Some(preview) => preview
            .getattr("orphaned_relations")?
            .try_iter()?
            .map(|relation| relation?.getattr("physical_name")?.extract())
            .collect::<PyResult<_>>()?,
    };
    let orphan_totals: Storage = storage_total(&orphaned, &storage);
    let payload: Bound<'py, PyDict> = py_kwargs!(py; database = database)?;
    for (key, value) in summary_payload(&deployment, &storage)?.iter() {
        payload.set_item(key, value)?;
    }
    payload.set_item("models", list_of(py, models)?)?;
    payload.set_item(
        "promotionPreview",
        promotion_preview_payload(py, promotion_preview.as_ref())?,
    )?;
    payload.set_item(
        "wouldOrphan",
        py_kwargs!(py;
            relationNames = orphaned.clone(),
            relationCount = orphaned.len(),
            bytes = orphan_totals.bytes,
        )?,
    )?;
    Ok(payload.into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_sensors_payload, module)?)?;
    module.add_function(wrap_pyfunction!(build_sensor_ticks_payload, module)?)?;
    module.add_function(wrap_pyfunction!(build_dead_letters_payload, module)?)?;
    module.add_function(wrap_pyfunction!(build_deployments_payload, module)?)?;
    module.add_function(wrap_pyfunction!(build_deployment_detail_payload, module)?)?;
    module.add_function(wrap_pyfunction!(
        super::definitions::build_definitions_payload,
        module
    )?)
}
