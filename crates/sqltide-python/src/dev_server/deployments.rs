//! Deployment lifecycle mutations the dev server runs as recorded, observable invocations, and
//! the deployment comparison payload.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use super::audits::{fstring, literal, logical_project_identity, persist, terminal_invocation};
use super::support::{attr, is_instance};
use crate::handling::while_handling;
use crate::py_kwargs;

const PROMOTE_COMMAND: &str = "deployment promote";
const CLEANUP_COMMAND: &str = "janitor";
const VIRTUAL_ENVIRONMENT_MODE: &str = "virtual_environment";

type Any<'py> = Bound<'py, PyAny>;

fn call<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    kwargs: Bound<'py, PyDict>,
) -> PyResult<Any<'py>> {
    attr(py, module, name)?.call((), Some(&kwargs))
}

/// A fresh invocation identity, its wall-clock start and its monotonic start.
fn start(py: Python<'_>) -> PyResult<Bound<'_, PyTuple>> {
    let invocation_id: Any<'_> = attr(py, "uuid", "uuid4")?.call0()?.str()?.into_any();
    let utc: Any<'_> = attr(py, "datetime", "UTC")?;
    let wall: Any<'_> = attr(py, "datetime", "datetime")?
        .call_method1("now", (utc,))?
        .call_method1("strftime", ("%Y-%m-%d %H:%M:%S.%f",))?;
    let started_at: Any<'_> = wall.get_item(pyo3::types::PySlice::new(py, 0, -3, 1))?;
    let monotonic: Any<'_> = attr(py, "time", "monotonic_ns")?.call0()?;
    PyTuple::new(py, [invocation_id, started_at, monotonic])
}

/// One terminal deployment-lifecycle invocation with no node results.
#[allow(clippy::too_many_arguments)]
fn record_operation<'py>(
    connection: &Any<'py>,
    metadata_database: &Any<'py>,
    started: &Bound<'py, PyTuple>,
    project_dir: &Any<'py>,
    database: &Any<'py>,
    command: &str,
    deployment_id: Any<'py>,
    outcome: &str,
    materialized_outcome: Any<'py>,
    error_message: Any<'py>,
    summary: Bound<'py, PyDict>,
    selected_node_count: usize,
) -> PyResult<()> {
    let py: Python<'py> = connection.py();
    let exit_code: i64 = i64::from(outcome != "succeeded");
    let invocation: Any<'py> = terminal_invocation(
        started.as_any(),
        py_kwargs!(py;
            project_dir = project_dir,
            target_identity = database,
            command = command,
            mode = VIRTUAL_ENVIRONMENT_MODE,
            outcome = outcome,
            exit_code = exit_code,
            materialized_outcome = materialized_outcome,
            deployment_id = deployment_id,
            workflow_id = py.None(),
            selected_node_count = selected_node_count,
            error_message = error_message,
            summary = summary,
        )?,
    )?;
    persist(
        connection,
        metadata_database,
        &invocation,
        &PyTuple::empty(py),
    )?;
    Ok(())
}

/// Model identities a promotion executes, when persisted deployment mappings make them
/// authoritative.
#[pyfunction]
#[pyo3(signature = (*, connection, metadata_database, deployment_id))]
pub(crate) fn promotion_executed_logical_ids<'py>(
    connection: &Any<'py>,
    metadata_database: &Any<'py>,
    deployment_id: &Any<'py>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = connection.py();
    let inventory: Any<'py> = match connection
        .call_method1("load_deployment_inventory", (metadata_database,))
    {
        Ok(inventory) => inventory,
        Err(error) if is_instance(py, &error, "sqltide.adapter.exceptions", "AdapterError")? => {
            return Ok(PyTuple::empty(py));
        }
        Err(error) => return Err(error),
    };
    let mut deployment: Option<Any<'py>> = None;
    for item in inventory.getattr("deployments")?.try_iter()? {
        let item: Any<'py> = item?;
        if item.getattr("deployment_id")?.eq(deployment_id)? {
            deployment = Some(item);
            break;
        }
    }
    let Some(deployment) = deployment else {
        return Ok(PyTuple::empty(py));
    };
    let constants: &str = "sqltide.compiler.compile.constants";
    let object_types: Bound<'py, PySet> = PySet::new(
        py,
        [
            attr(py, constants, "DESIRED_OBJECT_TYPE_TABLE")?,
            attr(py, constants, "DESIRED_OBJECT_TYPE_VIEW")?,
        ],
    )?;
    let names: Bound<'py, PySet> = PySet::empty(py)?;
    for mapping in deployment.getattr("prepared_object_mappings")?.try_iter()? {
        let mapping: Any<'py> = mapping?;
        if object_types.contains(mapping.getattr("logical_key")?.getattr("object_type")?)? {
            names.add(mapping.getattr("logical_model_name")?)?;
        }
    }
    let mut identities: Vec<Any<'py>> = Vec::new();
    for name in attr(py, "builtins", "sorted")?
        .call1((names,))?
        .try_iter()?
    {
        identities.push(fstring(py, &[literal(py, "model:"), name?])?);
    }
    PyTuple::new(py, identities)
}

/// Promote one deployment, recording it as a run the UI can follow.
#[pyfunction]
#[pyo3(signature = (*, connection, database, metadata_database, deployment_id, project_dir))]
pub(crate) fn run_dev_deployment_promotion<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    metadata_database: &Any<'py>,
    deployment_id: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let started: Bound<'py, PyTuple> = start(py)?;
    let sink: Any<'py> = attr(
        py,
        "sqltide.executor.observability.classes.run_event_sink",
        "RunEventSink",
    )?
    .call(
        (),
        Some(&py_kwargs!(py;
            connection = connection,
            database = metadata_database,
            invocation_id = started.get_item(0)?,
            project_identity = logical_project_identity(project_dir)?,
        )?),
    )?;
    let executed_logical_ids: Any<'py> = attr(py, "sqltide._native", "promotion_executed_logical_ids")?.call(
        (),
        Some(&py_kwargs!(py; connection = connection, metadata_database = metadata_database, deployment_id = deployment_id)?),
    )?;
    let selected_node_count: usize = executed_logical_ids.len()?;
    sink.call_method(
        "run_started",
        (),
        Some(&py_kwargs!(py;
            command = PROMOTE_COMMAND,
            display_command = fstring(py, &[literal(py, "sqt deployment promote "), deployment_id.clone()])?,
            mode = VIRTUAL_ENVIRONMENT_MODE,
            total_statements = 0,
            selected_node_count = selected_node_count,
            executed_logical_ids = &executed_logical_ids,
        )?),
    )?;
    let published: PyResult<Any<'py>> = (|| {
        let request: Any<'py> = attr(py, "sqltide.executor.promotion.models", "PublishRequest")?.call(
            (),
            Some(&py_kwargs!(py; deployment_id = deployment_id, metadata_database = metadata_database, default_database = database)?),
        )?;
        call(
            py,
            "sqltide.executor.promotion.main.execute_deployment_promotion",
            "execute_publish",
            py_kwargs!(py; request = request, client = connection, emitter = &sink)?,
        )
    })();
    let result: Any<'py> = match published {
        Ok(result) => result,
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let recorded: PyResult<()> = (|| {
                let message: Bound<'py, PyString> = error.value(py).str()?;
                sink.call_method(
                    "run_completed",
                    (),
                    Some(&py_kwargs!(py; outcome = "failed", exit_code = 1, error_message = &message)?),
                )?;
                record_operation(
                    connection,
                    metadata_database,
                    &started,
                    project_dir,
                    database,
                    PROMOTE_COMMAND,
                    deployment_id.clone(),
                    "failed",
                    py.None().into_bound(py),
                    error.value(py).str()?.into_any(),
                    PyDict::new(py),
                    selected_node_count,
                )
            })();
            return Err(match recorded {
                Ok(()) => error,
                Err(raised) => while_handling(py, raised, &error),
            });
        }
        Err(error) => return Err(error),
    };
    sink.call_method(
        "run_completed",
        (),
        Some(&py_kwargs!(py; outcome = "succeeded", exit_code = 0, error_message = py.None())?),
    )?;
    let summary: Bound<'py, PyDict> = PyDict::new(py);
    summary.set_item("publishedViews", result.getattr("published_views")?.len()?)?;
    record_operation(
        connection,
        metadata_database,
        &started,
        project_dir,
        database,
        PROMOTE_COMMAND,
        result.getattr("deployment_id")?,
        "succeeded",
        PyString::new(py, "applied").into_any(),
        py.None().into_bound(py),
        summary,
        selected_node_count,
    )?;
    let views: Bound<'py, PyList> = PyList::empty(py);
    for view in result.getattr("published_views")?.try_iter()? {
        let view: Any<'py> = view?;
        let entry: Bound<'py, PyDict> = PyDict::new(py);
        entry.set_item("logicalName", view.getattr("view_name")?)?;
        entry.set_item("physicalName", view.getattr("target_table_name")?)?;
        views.append(entry)?;
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("invocationId", started.get_item(0)?)?;
    payload.set_item("deploymentId", result.getattr("deployment_id")?)?;
    payload.set_item("publishedViews", views)?;
    payload.set_item(
        "graphAtomicPublish",
        result.getattr("graph_atomic_publish")?,
    )?;
    Ok(payload)
}

/// Apply janitor cleanup, recording it as a run the UI can follow.
#[pyfunction]
#[pyo3(signature = (*, connection, database, metadata_database, retention_days, project_dir))]
pub(crate) fn run_dev_deployment_cleanup<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    metadata_database: &Any<'py>,
    retention_days: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let started: Bound<'py, PyTuple> = start(py)?;
    let request: Any<'py> = attr(py, "sqltide.executor.janitor.models", "JanitorRequest")?.call(
        (),
        Some(&py_kwargs!(py;
            database = database,
            metadata_database = metadata_database,
            retention_days = retention_days,
            apply = true,
        )?),
    )?;
    let result: Any<'py> = call(
        py,
        "sqltide.executor.janitor.main.execute_janitor",
        "execute_janitor",
        py_kwargs!(py; request = request, client = connection)?,
    )?;
    let applied: bool = result.is_instance(&attr(
        py,
        "sqltide.executor.janitor.models",
        "JanitorApplyResult",
    )?)?;
    let removed: usize = if applied {
        result.getattr("deleted_object_names")?.len()?
    } else {
        0
    };
    let removed_deployments: usize = if applied {
        result.getattr("deleted_deployment_ids")?.len()?
    } else {
        0
    };
    let summary: Bound<'py, PyDict> = PyDict::new(py);
    summary.set_item("removedRelations", removed)?;
    summary.set_item("removedDeployments", removed_deployments)?;
    record_operation(
        connection,
        metadata_database,
        &started,
        project_dir,
        database,
        CLEANUP_COMMAND,
        py.None().into_bound(py),
        "succeeded",
        PyString::new(py, "applied").into_any(),
        py.None().into_bound(py),
        summary,
        0,
    )?;
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("invocationId", started.get_item(0)?)?;
    payload.set_item("removedRelations", removed)?;
    payload.set_item("removedDeployments", removed_deployments)?;
    Ok(payload)
}

/// Compare two deployment endpoints for the deployment detail view.
#[pyfunction]
#[pyo3(signature = (*, connection, database, metadata_database, comparison))]
pub(crate) fn build_deployment_diff_payload<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    metadata_database: &Any<'py>,
    comparison: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let request: Any<'py> = attr(py, "sqltide.executor.deployment.models", "DeploymentDiffRequest")?.call(
        (),
        Some(&py_kwargs!(py; database = database, metadata_database = metadata_database, comparison = comparison)?),
    )?;
    let result: Any<'py> = call(
        py,
        "sqltide.executor.deployment.main.execute_deployment_diff",
        "execute_deployment_diff",
        py_kwargs!(py; request = request, client = connection)?,
    )?;
    deployment_diff_payload(&result)
}

/// `str(getattr(column, "name", ""))`.
fn column_name<'py>(column: &Any<'py>) -> PyResult<Bound<'py, PyString>> {
    let py: Python<'py> = column.py();
    attr(py, "builtins", "getattr")?
        .call1((column, "name", ""))?
        .str()
}

/// Names of `left` columns absent from `right`, in `left` order.
fn column_names<'py>(left: &Any<'py>, right: &Any<'py>) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = left.py();
    let right_names: Bound<'py, PySet> = PySet::empty(py)?;
    for column in right.try_iter()? {
        right_names.add(column_name(&column?)?)?;
    }
    let names: Bound<'py, PyList> = PyList::empty(py);
    for column in left.try_iter()? {
        let column: Any<'py> = column?;
        if !right_names.contains(column_name(&column)?)? {
            names.append(column_name(&column)?)?;
        }
    }
    Ok(names)
}

/// Serialize one deployment comparison for the deployment detail view.
#[pyfunction]
#[pyo3(name = "deployment_diff_payload", signature = (*, result))]
fn deployment_diff_payload_py<'py>(result: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    deployment_diff_payload(result)
}

fn deployment_diff_payload<'py>(result: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = result.py();
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("database", result.getattr("database")?)?;
    payload.set_item("fromEndpoint", result.getattr("from_endpoint")?)?;
    payload.set_item("toEndpoint", result.getattr("to_endpoint")?)?;
    let relations: Bound<'py, PyList> = PyList::empty(py);
    for relation in result.getattr("relations")?.try_iter()? {
        let relation: Any<'py> = relation?;
        let entry: Bound<'py, PyDict> = PyDict::new(py);
        entry.set_item("logicalName", relation.getattr("logical_name")?)?;
        entry.set_item("status", relation.getattr("status")?.str()?)?;
        entry.set_item("fromPhysicalName", relation.getattr("from_physical_name")?)?;
        entry.set_item("toPhysicalName", relation.getattr("to_physical_name")?)?;
        entry.set_item("fromRowCount", relation.getattr("from_row_count")?)?;
        entry.set_item("toRowCount", relation.getattr("to_row_count")?)?;
        let from_columns: Any<'py> = relation.getattr("from_columns")?;
        let to_columns: Any<'py> = relation.getattr("to_columns")?;
        entry.set_item("addedColumns", column_names(&to_columns, &from_columns)?)?;
        entry.set_item("removedColumns", column_names(&from_columns, &to_columns)?)?;
        relations.append(entry)?;
    }
    payload.set_item("relations", relations)?;
    Ok(payload)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(promotion_executed_logical_ids, module)?)?;
    module.add_function(wrap_pyfunction!(run_dev_deployment_promotion, module)?)?;
    module.add_function(wrap_pyfunction!(run_dev_deployment_cleanup, module)?)?;
    module.add_function(wrap_pyfunction!(build_deployment_diff_payload, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_diff_payload_py, module)?)
}
