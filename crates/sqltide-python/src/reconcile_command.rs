//! Reconcile: classify live targets against the desired state, record baselines for the
//! structurally compatible ones, and persist them under the target mutation lock.

use std::collections::BTreeSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyFrozenSet, PyTuple};

use crate::dev_server::support::attr;
use crate::executor::{ReconcileObjectArgument, classify_reconcile_targets};
use crate::handling::within;
use crate::planner_support::{build_adapter_metadata_state, build_normalized_fingerprint};
use crate::py_kwargs;
use crate::workflow_statements::numbered;

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const RECONCILE_MODELS: &str = "sqltide.executor.reconcile.models";
const MUTATION_LOCK: &str = "sqltide.executor.workflow.main.target_mutation_lock";

type Any<'py> = Bound<'py, PyAny>;
type Key = (Option<String>, String, String);

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

fn is_a(value: &Any<'_>, module: &str, name: &str) -> PyResult<bool> {
    value.is_instance(&attr(value.py(), module, name)?)
}

fn native_key(key: &Any<'_>) -> PyResult<Key> {
    Ok((
        key.getattr("database")?.extract()?,
        key.getattr("object_type")?.extract()?,
        key.getattr("name")?.extract()?,
    ))
}

fn native_object(object: &Any<'_>) -> PyResult<ReconcileObjectArgument> {
    let py: Python<'_> = object.py();
    let spec: Any<'_> = object.getattr("spec")?;
    let key: Key = native_key(&object.getattr("key")?)?;
    let kind = |name: &str| -> PyResult<String> { attr(py, COMPILE_CONSTANTS, name)?.extract() };
    if is_a(&spec, COMPILE_MODELS, "TableSpec")? {
        let storage: Any<'_> = spec.getattr("storage")?;
        let columns: Vec<(String, String, Option<String>)> = items(&spec, "columns")?
            .iter()
            .map(|column| {
                Ok((
                    column.getattr("name")?.extract()?,
                    column.getattr("type")?.extract()?,
                    column.getattr("default")?.extract()?,
                ))
            })
            .collect::<PyResult<_>>()?;
        return Ok((
            kind("DESIRED_OBJECT_TYPE_TABLE")?,
            key,
            Some((
                storage.getattr("engine")?.extract()?,
                storage.getattr("order_by")?.extract()?,
                storage.getattr("partition_by")?.extract()?,
                columns,
            )),
            None,
        ));
    }
    if is_a(&spec, COMPILE_MODELS, "MaterializedViewSpec")? {
        return Ok((
            kind("DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW")?,
            key,
            None,
            Some((
                spec.getattr("source_table_name")?.extract()?,
                spec.getattr("target_table_name")?.extract()?,
            )),
        ));
    }
    if is_a(&spec, COMPILE_MODELS, "ViewSpec")? {
        return Ok((kind("DESIRED_OBJECT_TYPE_VIEW")?, key, None, None));
    }
    Ok((kind("DESIRED_OBJECT_TYPE_KAFKA_TABLE")?, key, None, None))
}

/// `datetime.now(tz=UTC).strftime(format)`.
fn utc_now(py: Python<'_>, format: &str) -> PyResult<String> {
    attr(py, "datetime", "datetime")?
        .call_method(
            "now",
            (),
            Some(&py_kwargs!(py; tz = attr(py, "datetime", "UTC")?)?),
        )?
        .call_method1("strftime", (format,))?
        .extract()
}

/// The object-state baseline reconcile records for one desired object.
fn object_state_record<'py>(
    desired: &Any<'py>,
    reconcile_id: &str,
    recorded_at: &str,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = desired.py();
    let materialized: bool = is_a(desired, COMPILE_MODELS, "DesiredMaterializedView")?;
    let query: Any<'py> = if materialized {
        desired.getattr("comparison_spec")?.getattr("query")?
    } else if is_a(desired, COMPILE_MODELS, "DesiredView")? {
        desired.getattr("query")?
    } else {
        py.None().into_bound(py)
    };
    let fingerprinted: Any<'py> = desired.getattr(if materialized {
        "comparison_spec"
    } else {
        "spec"
    })?;
    attr(py, PLANNER_MODELS, "ObjectStateRecord")?.call(
        (),
        Some(&py_kwargs!(py;
            deployment_id = reconcile_id,
            key = desired.getattr("key")?,
            normalized_fingerprint = build_normalized_fingerprint(&fingerprinted)?,
            normalized_query = query,
            recorded_at = recorded_at,
            state_kind = attr(py, "sqltide.adapter.constants", "VIRTUAL_OBJECT_STATE_KIND_RECONCILE")?,
        )?),
    )
}

/// Reconcile eligibility records and target rejections.
fn reconcile_preview<'py>(
    metadata_database: &Any<'py>,
    desired_state: &Any<'py>,
    actual_state: &Any<'py>,
    selected_model_keys: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = desired_state.py();
    let recorded_at: String = utc_now(py, "%Y-%m-%d %H:%M:%S.%f")?;
    let recorded_at: &str = &recorded_at[..recorded_at.len().saturating_sub(3)];
    let prefix: String = attr(
        py,
        "sqltide.executor.reconcile.constants",
        "RECONCILE_DEPLOYMENT_ID_PREFIX",
    )?
    .extract()?;
    let reconcile_id: String = format!("{prefix}{}", utc_now(py, "%Y%m%dT%H%M%SZ")?);
    let desired_objects: Any<'py> = desired_state.getattr("objects")?;
    let desired: Vec<ReconcileObjectArgument> = desired_objects
        .try_iter()?
        .map(|object| native_object(&object?))
        .collect::<PyResult<_>>()?;
    let actual: Vec<ReconcileObjectArgument> = items(actual_state, "objects")?
        .iter()
        .map(native_object)
        .collect::<PyResult<_>>()?;
    let selected: Vec<Key> = selected_model_keys
        .try_iter()?
        .map(|key| native_key(&key?))
        .collect::<PyResult<_>>()?;
    let outcomes = classify_reconcile_targets(py, desired, actual, selected).or_else(|error| {
        if !error.is_instance_of::<PyValueError>(py) {
            return Err(error);
        }
        let raised: Any<'py> = attr(
            py,
            "sqltide.compiler.planner.exceptions",
            "DeploymentPlanError",
        )?
        .call1((error.value(py).str()?,))?;
        raised.setattr("__context__", error.value(py))?;
        raised.setattr("__suppress_context__", true)?;
        Err(PyErr::from_value(raised))
    })?;
    let key_class: Any<'py> = attr(py, COMPILE_MODELS, "ObjectKey")?;
    let rejected_class: Any<'py> = attr(py, RECONCILE_MODELS, "ReconcileRejectedTarget")?;
    let mut eligible_records: Vec<Any<'py>> = Vec::new();
    let mut rejected: Vec<Any<'py>> = Vec::new();
    for ((database, object_type, name), target_name, eligible, reasons) in outcomes {
        let Some((table, transform)) = eligible else {
            rejected.push(rejected_class.call(
                (),
                Some(&py_kwargs!(py;
                    target_key = key_class.call(
                        (),
                        Some(&py_kwargs!(py; database = database, object_type = object_type, name = name)?),
                    )?,
                    target_name = target_name,
                    reasons = PyTuple::new(py, reasons)?,
                )?),
            )?);
            continue;
        };
        for position in std::iter::once(table).chain(transform) {
            eligible_records.push(object_state_record(
                &desired_objects.get_item(position)?,
                &reconcile_id,
                recorded_at,
            )?);
        }
    }
    attr(py, RECONCILE_MODELS, "ReconcilePreview")?.call(
        (),
        Some(&py_kwargs!(py;
            database = metadata_database,
            reconcile_id = reconcile_id,
            eligible_records = PyTuple::new(py, eligible_records)?,
            rejected_targets = PyTuple::new(py, rejected)?,
        )?),
    )
}

fn reconcile_result<'py>(preview: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = preview.py();
    attr(py, RECONCILE_MODELS, "ReconcileResult")?.call(
        (),
        Some(&py_kwargs!(py;
            database = preview.getattr("database")?,
            reconcile_id = preview.getattr("reconcile_id")?,
            reconciled_records = preview.getattr("eligible_records")?,
            rejected_targets = preview.getattr("rejected_targets")?,
        )?),
    )
}

fn mutation_lock<'py>(client: &Any<'py>, database: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    attr(py, MUTATION_LOCK, "target_mutation_lock")?.call(
        (),
        Some(&py_kwargs!(py; connection = client, database = database)?),
    )
}

/// Preview or apply reconcile for structurally compatible live targets.
#[pyfunction]
#[pyo3(signature = (
    *, client, target_database, metadata_database, desired_state, actual_state,
    selected_model_keys, apply = false
))]
fn execute_reconcile<'py>(
    client: &Any<'py>,
    target_database: &Any<'py>,
    metadata_database: &Any<'py>,
    desired_state: &Any<'py>,
    actual_state: &Any<'py>,
    selected_model_keys: &Any<'py>,
    apply: bool,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let preview: Any<'py> = reconcile_preview(
        metadata_database,
        desired_state,
        actual_state,
        selected_model_keys,
    )?;
    if !apply {
        return Ok(preview);
    }
    within(&mutation_lock(client, target_database)?, || {
        let state: Any<'py> = attr(py, PLANNER_MODELS, "MetadataState")?.call(
            (),
            Some(&py_kwargs!(py;
                object_states = preview.getattr("eligible_records")?,
                deployments = PyTuple::empty(py),
                deployment_watermarks = PyTuple::empty(py),
                publish_events = PyTuple::empty(py),
            )?),
        )?;
        let metadata_state: Any<'py> = build_adapter_metadata_state(py, &state)?;
        let database: Any<'py> = preview.getattr("database")?;
        let migration_sql: Any<'py> =
            client.call_method1("render_migrate_metadata_state", (&database,))?;
        let persistence_sql: Any<'py> = client.call_method(
            "render_persist_metadata_state",
            (),
            Some(&py_kwargs!(py; database = &database, state = &metadata_state)?),
        )?;
        let mut statements: Vec<Any<'py>> =
            numbered(&migration_sql, 1, "migrate_metadata", "FINALIZATION")?;
        let next: usize = statements.len() + 1;
        statements.extend(numbered(
            &persistence_sql,
            next,
            "persist_reconcile_state",
            "FINALIZATION",
        )?);
        attr(
            py,
            "sqltide.executor.workflow.main._execute_warehouse_workflow",
            "execute_warehouse_workflow",
        )?
        .call(
            (),
            Some(&py_kwargs!(py; statements = PyTuple::new(py, statements)?, connection = client)?),
        )?;
        reconcile_result(&preview)
    })
}

/// Preview or persist structurally compatible direct model baselines.
#[pyfunction]
#[pyo3(signature = (
    *, client, target_database, metadata_database, desired_state, catalog, models,
    selected_model_keys, tool_version, apply = false
))]
#[allow(clippy::too_many_arguments)]
fn execute_direct_reconcile<'py>(
    client: &Any<'py>,
    target_database: &Any<'py>,
    metadata_database: &Any<'py>,
    desired_state: &Any<'py>,
    catalog: &Any<'py>,
    models: &Any<'py>,
    selected_model_keys: &Any<'py>,
    tool_version: &Any<'py>,
    apply: bool,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let actual_state: Any<'py> =
        attr(py, "sqltide._native", "build_direct_reconcile_actual_state")?.call(
            (),
            Some(&py_kwargs!(py; desired_state = desired_state, catalog = catalog)?),
        )?;
    let preview: Any<'py> = reconcile_preview(
        metadata_database,
        desired_state,
        &actual_state,
        selected_model_keys,
    )?;
    if !apply {
        return Ok(preview);
    }
    let rejected_keys: Vec<Any<'py>> = items(&preview, "rejected_targets")?
        .iter()
        .map(|target| target.getattr("target_key"))
        .collect::<PyResult<_>>()?;
    let rejected_keys: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, rejected_keys)?;
    let raw_prefix: String = attr(py, COMPILE_CONSTANTS, "RAW_TABLE_NAME_PREFIX")?.extract()?;
    let selecting: bool = selected_model_keys.is_truthy()?;
    let mut eligible: BTreeSet<String> = BTreeSet::new();
    for object in items(desired_state, "objects")? {
        if !is_a(&object, COMPILE_MODELS, "DesiredTable")? {
            continue;
        }
        let name: String = object.getattr("name")?.extract()?;
        let key: Any<'py> = object.getattr("key")?;
        if name.starts_with(&raw_prefix)
            || rejected_keys.contains(&key)?
            || (selecting && !selected_model_keys.contains(&key)?)
        {
            continue;
        }
        let logical: Any<'py> = object.getattr("logical_model_name")?;
        eligible.insert(if logical.is_truthy()? {
            logical.extract()?
        } else {
            name
        });
    }
    let mut eligible_models: Vec<Any<'py>> = Vec::new();
    let mut resolved: BTreeSet<String> = BTreeSet::new();
    for model in models.try_iter()? {
        let model: Any<'py> = model?;
        let name: String = model.getattr("key")?.getattr("name")?.extract()?;
        if eligible.contains(&name) {
            resolved.insert(name);
            eligible_models.push(model);
        }
    }
    let unresolved: Vec<&String> = eligible.difference(&resolved).collect();
    if !unresolved.is_empty() {
        return Err(PyErr::from_value(
            attr(
                py,
                "sqltide.executor.reconcile.exceptions",
                "ReconcileError",
            )?
            .call1((format!(
                "Direct reconcile could not resolve compiled models for eligible targets: {}",
                unresolved
                    .iter()
                    .map(|name| name.as_str())
                    .collect::<Vec<&str>>()
                    .join(", ")
            ),))?,
        ));
    }
    within(&mutation_lock(client, target_database)?, || {
        attr(
            py,
            "sqltide._native",
            "persist_reconciled_direct_fingerprints",
        )?
        .call(
            (),
            Some(&py_kwargs!(py;
                models = PyTuple::new(py, &eligible_models)?,
                target_database = target_database,
                metadata_database = metadata_database,
                workflow_id = preview.getattr("reconcile_id")?,
                tool_version = tool_version,
                connection = client,
            )?),
        )?;
        Ok(())
    })?;
    reconcile_result(&preview)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_reconcile, module)?)?;
    module.add_function(wrap_pyfunction!(execute_direct_reconcile, module)?)
}
