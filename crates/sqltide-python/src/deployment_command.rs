//! Deployment inventory and diff: gather warehouse evidence, derive lifecycle summaries and
//! compare the live relations of two deployment endpoints.

use pyo3::prelude::*;
use pyo3::types::{PyInt, PyTuple};

use crate::dev_server::support::attr;
use crate::executor::{
    DeploymentArgument, InventoryRecordArgument, MappingArgument, deployment_diff_count_sql,
    deployment_diff_relations, deployment_diff_status, deployment_inventory_summaries,
    parse_deployment_diff,
};
use crate::py_kwargs;

const MODELS: &str = "sqltide.executor.deployment.models";
const TYPES: &str = "sqltide.executor.deployment.types";
const EXCEPTIONS: &str = "sqltide.executor.deployment.exceptions";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";

type Any<'py> = Bound<'py, PyAny>;
type Column = (String, String, Option<String>);

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

/// `DeploymentDiffError(message)`.
fn diff_error(py: Python<'_>, message: impl Into<String>) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(py, EXCEPTIONS, "DeploymentDiffError")?.call1((message.into(),))?,
    ))
}

/// `except ValueError as error: raise DeploymentDiffError(str(error)) from None`.
fn diff_error_from_none<T>(py: Python<'_>, result: PyResult<T>) -> PyResult<T> {
    result.or_else(|handled| {
        if !handled.is_instance_of::<pyo3::exceptions::PyValueError>(py) {
            return Err(handled);
        }
        let raised: PyErr = diff_error(py, handled.value(py).str()?.to_string())?;
        let value: Bound<'_, PyAny> = raised.value(py).clone().into_any();
        value.setattr("__context__", handled.value(py))?;
        value.setattr("__suppress_context__", true)?;
        Err(raised)
    })
}

fn native_deployment(deployment: &Any<'_>) -> PyResult<DeploymentArgument> {
    let py: Python<'_> = deployment.py();
    let incomplete: Any<'_> = attr(
        py,
        "sqltide.adapter.constants",
        "VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE",
    )?;
    let table: Any<'_> = attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_TABLE")?;
    let view: Any<'_> = attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_VIEW")?;
    let mut mappings: Vec<MappingArgument> = Vec::new();
    for mapping in items(deployment, "prepared_object_mappings")? {
        let key: Any<'_> = mapping.getattr("logical_key")?;
        let database: Any<'_> = key.getattr("database")?;
        let object_type: Any<'_> = key.getattr("object_type")?;
        mappings.push((
            if database.is_truthy()? {
                Some(database.extract()?)
            } else {
                None
            },
            object_type.eq(&table)? || object_type.eq(&view)?,
            key.getattr("name")?.extract()?,
            mapping.getattr("physical_name")?.extract()?,
            mapping.getattr("logical_model_name")?.extract()?,
        ));
    }
    Ok((
        deployment.getattr("deployment_id")?.extract()?,
        deployment.getattr("created_at")?.extract()?,
        deployment.getattr("status")?.eq(incomplete)?,
        mappings,
    ))
}

fn diff_columns<'py>(relation: &Any<'py>) -> PyResult<(Vec<Any<'py>>, Vec<Column>)> {
    let py: Python<'py> = relation.py();
    if relation.is_none() {
        return Ok((Vec::new(), Vec::new()));
    }
    let column_class: Any<'py> = attr(py, MODELS, "DeploymentDiffColumn")?;
    let mut columns: Vec<Any<'py>> = Vec::new();
    let mut native: Vec<Column> = Vec::new();
    for column in items(relation, "columns")? {
        let name: Any<'py> = column.getattr("name")?;
        let kind: Any<'py> = column.getattr("type")?;
        let default: Any<'py> = column.getattr("default_expression")?;
        let built: Any<'py> = column_class.call(
            (),
            Some(&py_kwargs!(py; name = &name, type = &kind, default_expression = &default)?),
        )?;
        native.push((
            built.getattr("name")?.extract()?,
            built.getattr("type")?.extract()?,
            built.getattr("default_expression")?.extract()?,
        ));
        columns.push(built);
    }
    Ok((columns, native))
}

fn row_count<'py>(relation: &Any<'py>, database: &str, client: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    if relation.is_none() {
        return Ok(py.None().into_bound(py));
    }
    let name: String = relation.getattr("name")?.extract()?;
    let sql: String = diff_error_from_none(py, deployment_diff_count_sql(database, &name))?;
    let rows: Any<'py> = client.call_method1("query", (sql,))?.getattr("rows")?;
    if rows.len()? != 1 || rows.get_item(0)?.len()? != 1 {
        return Err(diff_error(
            py,
            format!("Deployment diff count for '{name}' returned an unexpected shape"),
        )?);
    }
    let value: Any<'py> = rows.get_item(0)?.get_item(0)?;
    if !value.is_instance_of::<PyInt>() {
        return Err(diff_error(
            py,
            format!("Deployment diff count for '{name}' was not an integer"),
        )?);
    }
    Ok(value)
}

/// One side of a comparison: `(bound, (columns, row count) when the relation exists)`.
type Side = (bool, Option<(Vec<Column>, Option<i128>)>);

fn side(
    physical: &Option<String>,
    relation: &Any<'_>,
    columns: Vec<Column>,
    rows: &Any<'_>,
) -> PyResult<Side> {
    Ok((
        physical.is_some(),
        if relation.is_none() {
            None
        } else {
            Some((columns, rows.extract()?))
        },
    ))
}

fn compare_relation<'py>(
    database: &str,
    logical_name: &str,
    from_physical: Option<String>,
    to_physical: Option<String>,
    catalog: &Any<'py>,
    client: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let lookup = |name: &Option<String>| -> PyResult<Any<'py>> {
        match name {
            None => Ok(py.None().into_bound(py)),
            Some(name) => catalog.call_method1("relation", (name,)),
        }
    };
    let from_catalog: Any<'py> = lookup(&from_physical)?;
    let same: bool = from_physical.is_some() && from_physical == to_physical;
    let to_catalog: Any<'py> = if same {
        from_catalog.clone()
    } else {
        lookup(&to_physical)?
    };
    let (from_columns, from_native) = diff_columns(&from_catalog)?;
    let (to_columns, to_native) = diff_columns(&to_catalog)?;
    let from_rows: Any<'py> = row_count(&from_catalog, database, client)?;
    let to_rows: Any<'py> = if same {
        from_rows.clone()
    } else {
        row_count(&to_catalog, database, client)?
    };
    let status: &str = deployment_diff_status(
        side(&from_physical, &from_catalog, from_native, &from_rows)?,
        side(&to_physical, &to_catalog, to_native, &to_rows)?,
    );
    attr(py, MODELS, "DeploymentDiffRelation")?.call(
        (),
        Some(&py_kwargs!(py;
            database = database,
            logical_name = logical_name,
            status = attr(py, TYPES, "DeploymentDiffStatus")?.call1((status,))?,
            from_physical_name = from_physical,
            to_physical_name = to_physical,
            from_columns = PyTuple::new(py, from_columns)?,
            to_columns = PyTuple::new(py, to_columns)?,
            from_row_count = from_rows,
            to_row_count = to_rows,
        )?),
    )
}

/// Compare active or retained deployment relations: schemas and row counts.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn execute_deployment_diff<'py>(request: &Any<'py>, client: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let database: String = request.getattr("database")?.extract()?;
    let metadata_database: String = request.getattr("metadata_database")?.extract()?;
    let comparison: String = request.getattr("comparison")?.extract()?;
    let (from, to) = diff_error_from_none(
        py,
        parse_deployment_diff(
            &comparison,
            vec![database.clone(), metadata_database.clone()],
        ),
    )?;
    let inventory: Any<'py> =
        client.call_method1("load_deployment_inventory", (&metadata_database,))?;
    let state: Any<'py> = client.call_method1("inspect_managed_table_state", (&database,))?;
    let deployments: Vec<DeploymentArgument> = items(&inventory, "deployments")?
        .iter()
        .map(native_deployment)
        .collect::<PyResult<_>>()?;
    let active: Vec<(String, String, String)> = items(&state, "active_bindings")?
        .iter()
        .map(|binding| {
            Ok((
                binding.getattr("database")?.extract()?,
                binding.getattr("logical_name")?.extract()?,
                binding.getattr("physical_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let (pairs, databases) = diff_error_from_none(
        py,
        deployment_diff_relations(&from, &to, deployments, active, &database),
    )?;
    let mut catalogs: Vec<(String, Any<'py>)> = Vec::new();
    for catalog_database in databases {
        let catalog: Any<'py> = client.call_method1("load_catalog", (&catalog_database,))?;
        match catalogs
            .iter_mut()
            .find(|(name, _)| *name == catalog_database)
        {
            Some(entry) => entry.1 = catalog,
            None => catalogs.push((catalog_database, catalog)),
        }
    }
    let mut relations: Vec<Any<'py>> = Vec::new();
    for (pair_database, logical_name, from_physical, to_physical) in pairs {
        let catalog: &Any<'py> = &catalogs
            .iter()
            .find(|(name, _)| *name == pair_database)
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(pair_database.clone()))?
            .1;
        relations.push(compare_relation(
            &pair_database,
            &logical_name,
            from_physical,
            to_physical,
            catalog,
            client,
        )?);
    }
    attr(py, MODELS, "DeploymentDiffResult")?.call(
        (),
        Some(&py_kwargs!(py;
            database = database,
            from_endpoint = from,
            to_endpoint = to,
            relations = PyTuple::new(py, relations)?,
        )?),
    )
}

/// Deterministic deployment inventory for one target: append-only lifecycle records combined
/// with current warehouse evidence.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, default_database))]
fn load_deployments<'py>(
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    default_database: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let persisted: Any<'py> =
        client.call_method1("load_deployment_inventory", (metadata_database,))?;
    let inspected: Any<'py> =
        client.call_method1("inspect_managed_table_state", (default_database,))?;
    let catalog: Any<'py> = client.call_method1("load_catalog", (default_database,))?;
    let mut records: Vec<InventoryRecordArgument> = Vec::new();
    for record in items(&persisted, "deployments")? {
        records.push((
            record.getattr("deployment_id")?.extract()?,
            record.getattr("created_at")?.extract()?,
            record.getattr("status")?.extract()?,
            items(&record, "selected_root_keys")?
                .iter()
                .map(|key| key.getattr("name")?.extract())
                .collect::<PyResult<_>>()?,
            items(&record, "prepared_object_mappings")?
                .iter()
                .map(|mapping| {
                    Ok((
                        mapping.getattr("logical_key")?.getattr("name")?.extract()?,
                        mapping.getattr("physical_name")?.extract()?,
                    ))
                })
                .collect::<PyResult<_>>()?,
        ));
    }
    let candidates: Vec<String> = items(&inspected, "physical_candidates")?
        .iter()
        .map(|candidate| candidate.getattr("physical_name")?.extract())
        .collect::<PyResult<_>>()?;
    let active: Vec<(String, String)> = items(&inspected, "active_bindings")?
        .iter()
        .map(|binding| {
            Ok((
                binding.getattr("logical_name")?.extract()?,
                binding.getattr("physical_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let publications: Vec<(String, String)> = items(&persisted, "publish_events")?
        .iter()
        .map(|event| {
            Ok((
                event.getattr("deployment_id")?.extract()?,
                event.getattr("published_at")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let mut existing: Vec<String> = catalog
        .call_method0("relation_names")?
        .try_iter()?
        .map(|name| name?.extract())
        .collect::<PyResult<_>>()?;
    existing.sort();
    let state_class: Any<'py> = attr(py, TYPES, "DeploymentLifecycleState")?;
    let summary_class: Any<'py> = attr(py, MODELS, "DeploymentSummary")?;
    let mut summaries: Vec<Any<'py>> = Vec::new();
    for (
        deployment_id,
        state,
        created_at,
        persisted_status,
        root_names,
        physical_relation_names,
        missing_physical_relation_names,
        active_binding_names,
        latest_published_at,
    ) in deployment_inventory_summaries(records, candidates, active, publications, existing)
    {
        summaries.push(summary_class.call(
            (),
            Some(&py_kwargs!(py;
                deployment_id = deployment_id,
                state = state_class.call1((state,))?,
                created_at = created_at,
                persisted_status = persisted_status,
                root_names = PyTuple::new(py, root_names)?,
                physical_relation_names = PyTuple::new(py, physical_relation_names)?,
                missing_physical_relation_names = PyTuple::new(py, missing_physical_relation_names)?,
                active_binding_names = PyTuple::new(py, active_binding_names)?,
                latest_published_at = latest_published_at,
            )?),
        )?);
    }
    attr(py, MODELS, "DeploymentInventory")?.call(
        (),
        Some(&py_kwargs!(py; database = default_database, deployments = PyTuple::new(py, summaries)?)?),
    )
}

/// One deployment by ID, or `DeploymentNotFoundError`.
#[pyfunction]
#[pyo3(signature = (*, deployment_id, client, metadata_database, default_database))]
fn load_deployment<'py>(
    deployment_id: &Any<'py>,
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    default_database: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let inventory: Any<'py> = load_deployments(client, metadata_database, default_database)?;
    for deployment in items(&inventory, "deployments")? {
        if deployment.getattr("deployment_id")?.eq(deployment_id)? {
            return Ok(deployment);
        }
    }
    Err(PyErr::from_value(
        attr(py, EXCEPTIONS, "DeploymentNotFoundError")?.call1((format!(
            "Deployment '{}' was not found in database '{}'",
            deployment_id.str()?,
            default_database.str()?
        ),))?,
    ))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_deployment_diff, module)?)?;
    module.add_function(wrap_pyfunction!(load_deployments, module)?)?;
    module.add_function(wrap_pyfunction!(load_deployment, module)?)
}
