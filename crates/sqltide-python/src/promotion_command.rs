//! Deployment promotion: rebind stable logical views to a staged deployment and record the
//! publication, under the target mutation lock.

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sqltide_planner::direct::sha256_hex;

use crate::deployment_resolution::{build_publish_binding_request, resolve_publish_deployment_id};
use crate::dev_server::support::attr;
use crate::handling::within;
use crate::py_kwargs;
use crate::workflow_statements::{mutation, numbered};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const PROMOTION_MODELS: &str = "sqltide.executor.promotion.models";

type Any<'py> = Bound<'py, PyAny>;

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

/// The single publish event recorded for this promotion.
fn publish_metadata_state<'py>(
    py: Python<'py>,
    deployment_id: &Any<'py>,
    published_at: &Any<'py>,
    views: &[(Any<'py>, Any<'py>)],
    request: &Any<'py>,
) -> PyResult<Any<'py>> {
    let database: Any<'py> = request.getattr("default_database")?;
    let operation: Any<'py> = request.getattr("operation")?;
    let now: u128 = attr(py, "time", "time_ns")?.call0()?.extract()?;
    let publication_id: String = format!(
        "{now:020}_{}",
        sha256_hex(&format!(
            "{}:{}:{}",
            deployment_id.str()?,
            published_at.str()?,
            operation.str()?
        ))
    );
    let binding_class: Any<'py> = attr(py, ADAPTER_MODELS, "AdapterStableBinding")?;
    let bindings: Vec<Any<'py>> = views
        .iter()
        .map(|(view_name, target)| {
            binding_class.call(
                (),
                Some(&py_kwargs!(py;
                    database = &database,
                    logical_name = view_name,
                    physical_name = target,
                )?),
            )
        })
        .collect::<PyResult<_>>()?;
    let event: Any<'py> = attr(py, ADAPTER_MODELS, "AdapterPublishEventRecord")?.call(
        (),
        Some(&py_kwargs!(py;
            deployment_id = deployment_id,
            published_at = published_at,
            logical_view_names = PyTuple::new(py, views.iter().map(|(name, _)| name))?,
            publication_id = publication_id,
            bindings = PyTuple::new(py, bindings)?,
            operation = &operation,
            previous_deployment_id = request.getattr("previous_deployment_id")?,
        )?),
    )?;
    attr(py, ADAPTER_MODELS, "AdapterMetadataState")?.call(
        (),
        Some(&py_kwargs!(py;
            object_states = PyTuple::empty(py),
            deployments = PyTuple::empty(py),
            deployment_watermarks = PyTuple::empty(py),
            publish_events = PyTuple::new(py, [event])?,
        )?),
    )
}

/// Binding switchovers, then metadata migration, then the publish event, in lifecycle order.
fn publish_workflow<'py>(
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    binding_request: &Any<'py>,
    metadata_state: &Any<'py>,
) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = client.py();
    let binding_sql: Any<'py> =
        client.call_method1("render_replace_stable_bindings", (binding_request,))?;
    let migration_sql: Any<'py> =
        client.call_method1("render_migrate_metadata_state", (metadata_database,))?;
    let persistence_sql: Any<'py> = client.call_method(
        "render_persist_metadata_state",
        (),
        Some(&py_kwargs!(py; database = metadata_database, state = metadata_state)?),
    )?;
    let mut step_ids: Vec<String> = Vec::new();
    for binding in items(binding_request, "bindings")? {
        let name: Any<'py> = binding.getattr("logical_name")?;
        step_ids.push(format!("replace_stable_binding_{}", name.str()?));
    }
    for removal in items(binding_request, "removals")? {
        let name: Any<'py> = removal.getattr("logical_name")?;
        step_ids.push(format!("remove_stable_binding_{}", name.str()?));
    }
    let mut statements: Vec<Any<'py>> = Vec::new();
    for (index, sql) in binding_sql.try_iter()?.enumerate() {
        let sql: Any<'py> = sql?;
        let step_id: String = step_ids
            .get(index)
            .cloned()
            .ok_or_else(|| PyIndexError::new_err("tuple index out of range"))?;
        statements.push(mutation(py, 1 + index, step_id, "STABILIZATION", sql)?);
    }
    let migration_start: usize = statements.len() + 1;
    let migrations: Vec<Any<'py>> = numbered(
        &migration_sql,
        migration_start,
        "migrate_metadata",
        "FINALIZATION",
    )?;
    let persistence_start: usize = migration_start + migrations.len();
    statements.extend(migrations);
    statements.extend(numbered(
        &persistence_sql,
        persistence_start,
        "persist_publish_event",
        "FINALIZATION",
    )?);
    Ok(statements)
}

/// Publish a staged deployment by creating or replacing its stable logical views.
#[pyfunction]
#[pyo3(signature = (*, request, client, emitter = None))]
fn execute_publish<'py>(
    request: &Any<'py>,
    client: &Any<'py>,
    emitter: Option<Any<'py>>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    if !client
        .getattr("capabilities")?
        .getattr("stable_logical_bindings")?
        .is_truthy()?
    {
        let name: Any<'py> = client.getattr("adapter_identity")?.getattr("name")?;
        return Err(PyErr::from_value(
            attr(py, "sqltide.adapter.exceptions", "AdapterCapabilityError")?.call1((format!(
                "Adapter '{}' does not support stable logical bindings",
                name.str()?
            ),))?,
        ));
    }
    let metadata_database: Any<'py> = request.getattr("metadata_database")?;
    let default_database: Any<'py> = request.getattr("default_database")?;
    let lock: Any<'py> = attr(
        py,
        "sqltide.executor.workflow.main.target_mutation_lock",
        "target_mutation_lock",
    )?
    .call(
        (),
        Some(&py_kwargs!(py; connection = client, database = &default_database)?),
    )?;
    within(&lock, || {
        let deployment_id: Any<'py> = resolve_publish_deployment_id(
            py,
            client,
            &metadata_database,
            &default_database,
            request.getattr("deployment_id")?,
        )?;
        let binding_request: Any<'py> = build_publish_binding_request(
            py,
            client,
            &metadata_database,
            &default_database,
            &deployment_id,
            None,
        )?;
        let view_class: Any<'py> = attr(py, PROMOTION_MODELS, "PublishedView")?;
        let mut views: Vec<(Any<'py>, Any<'py>)> = Vec::new();
        let mut published: Vec<Any<'py>> = Vec::new();
        for binding in items(&binding_request, "bindings")? {
            let view: Any<'py> = view_class.call(
                (),
                Some(&py_kwargs!(py;
                    view_name = binding.getattr("logical_name")?,
                    target_table_name = binding.getattr("physical_name")?,
                )?),
            )?;
            views.push((
                view.getattr("view_name")?,
                view.getattr("target_table_name")?,
            ));
            published.push(view);
        }
        let published_at: Any<'py> = client.call_method0("capture_warehouse_timestamp")?;
        let metadata_state: Any<'py> =
            publish_metadata_state(py, &deployment_id, &published_at, &views, request)?;
        let statements: Vec<Any<'py>> = publish_workflow(
            client,
            &metadata_database,
            &binding_request,
            &metadata_state,
        )?;
        attr(
            py,
            "sqltide.executor.workflow.main._execute_warehouse_workflow",
            "execute_warehouse_workflow",
        )?
        .call(
            (),
            Some(&py_kwargs!(py;
                statements = PyTuple::new(py, statements)?,
                connection = client,
                emitter = &emitter,
            )?),
        )?;
        let capabilities: Any<'py> = client.getattr("capabilities")?;
        attr(py, PROMOTION_MODELS, "PublishResult")?.call(
            (),
            Some(&py_kwargs!(py;
                deployment_id = &deployment_id,
                published_views = PyTuple::new(py, published)?,
                per_relation_atomic_replace = capabilities.getattr("per_relation_atomic_replace")?,
                graph_atomic_publish = capabilities.getattr("graph_atomic_publish")?,
                operation = request.getattr("operation")?,
                previous_deployment_id = request.getattr("previous_deployment_id")?,
            )?),
        )
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_publish, module)?)
}
