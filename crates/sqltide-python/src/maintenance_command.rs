//! Active-view maintenance: doctor inspects stable view health per managed table, and repair
//! rebinds one stable view to a chosen deployment table under the target mutation lock.

use std::collections::BTreeSet;

use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::dev_server::support::attr;
use crate::handling::within;
use crate::py_kwargs;
use crate::workflow_statements::mutation;

const NATIVE: &str = "sqltide._native";

type Any<'py> = Bound<'py, PyAny>;

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

/// Active-view health for every managed deployment table in the default database.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn execute_doctor<'py>(request: &Any<'py>, client: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let database: Any<'py> = request.getattr("default_database")?;
    let catalog: Any<'py> = client.call_method1("load_catalog", (&database,))?;
    let state: Any<'py> = attr(
        py,
        NATIVE,
        "build_inspected_managed_table_state_from_catalog",
    )?
    .call((), Some(&py_kwargs!(py; catalog = catalog)?))?;
    let bindings: Vec<Any<'py>> = items(&state, "active_bindings")?;
    let candidates: Vec<Any<'py>> = items(&state, "physical_candidates")?;
    let mut names: BTreeSet<String> = BTreeSet::new();
    for item in bindings.iter().chain(&candidates) {
        names.insert(item.getattr("logical_name")?.extract()?);
    }
    let key_class: Any<'py> = attr(py, "sqltide.compiler.compile.models", "ObjectKey")?;
    let table: Any<'py> = attr(
        py,
        "sqltide.compiler.compile.constants",
        "DESIRED_OBJECT_TYPE_TABLE",
    )?;
    let inspect: Any<'py> = attr(py, NATIVE, "inspect_root_deployment_state")?;
    let deployment_id: Any<'py> = attr(py, NATIVE, "deployment_id_from_physical_name")?;
    let status_class: Any<'py> = attr(py, "sqltide.executor.doctor.models", "ActiveViewStatus")?;
    let mut views: Vec<Any<'py>> = Vec::new();
    for name in names {
        let inspection: Any<'py> = inspect.call(
            (),
            Some(&py_kwargs!(py;
                inspected_state = &state,
                root_key = key_class.call(
                    (),
                    Some(&py_kwargs!(py; database = &database, object_type = &table, name = &name)?),
                )?,
            )?),
        )?;
        let mut deployment_ids: Vec<String> = Vec::new();
        for candidate in &candidates {
            if candidate.getattr("database")?.eq(&database)?
                && candidate.getattr("logical_name")?.eq(&name)?
            {
                deployment_ids.push(
                    deployment_id
                        .call1((candidate.getattr("physical_name")?,))?
                        .extract()?,
                );
            }
        }
        deployment_ids.sort();
        views.push(status_class.call(
            (),
            Some(&py_kwargs!(py;
                table_name = &name,
                state_kind = inspection.getattr("state_kind")?,
                active_deployment_id = inspection.getattr("active_deployment_id")?,
                candidate_deployment_ids = PyTuple::new(py, deployment_ids)?,
            )?),
        )?);
    }
    attr(py, "sqltide.executor.doctor.models", "DoctorResult")?.call(
        (),
        Some(&py_kwargs!(py; active_views = PyTuple::new(py, views)?)?),
    )
}

/// Explicitly rebind a stable active view to a chosen deployment table.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn execute_repair_active_view<'py>(request: &Any<'py>, client: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let table_name: Any<'py> = request.getattr("table_name")?;
    let target: Any<'py> = attr(py, NATIVE, "build_deployment_physical_name")?.call(
        (),
        Some(&py_kwargs!(py;
            logical_name = &table_name,
            deployment_id = request.getattr("deployment_id")?,
        )?),
    )?;
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
    let database: Any<'py> = request.getattr("default_database")?;
    let lock: Any<'py> = attr(
        py,
        "sqltide.executor.workflow.main.target_mutation_lock",
        "target_mutation_lock",
    )?
    .call(
        (),
        Some(&py_kwargs!(py; connection = client, database = &database)?),
    )?;
    within(&lock, || {
        let models: &str = "sqltide.adapter.models";
        let binding: Any<'py> = attr(py, models, "AdapterStableBinding")?.call(
            (),
            Some(&py_kwargs!(py;
                database = &database,
                logical_name = &table_name,
                physical_name = &target,
            )?),
        )?;
        let replacement: Any<'py> = attr(py, models, "AdapterBindingReplacementRequest")?.call(
            (),
            Some(&py_kwargs!(py; bindings = PyTuple::new(py, [binding])?)?),
        )?;
        let rendered: Any<'py> =
            client.call_method1("render_replace_stable_bindings", (replacement,))?;
        let statement: Any<'py> = mutation(
            py,
            1,
            "replace_active_view".to_owned(),
            "FINALIZATION",
            rendered.get_item(0)?,
        )?;
        attr(
            py,
            "sqltide.executor.workflow.main._execute_warehouse_workflow",
            "execute_warehouse_workflow",
        )?
        .call(
            (),
            Some(
                &py_kwargs!(py; statements = PyTuple::new(py, [statement])?, connection = client)?,
            ),
        )?;
        attr(
            py,
            "sqltide.executor.repair.models",
            "RepairActiveViewResult",
        )?
        .call(
            (),
            Some(&py_kwargs!(py; table_name = &table_name, target_table_name = &target)?),
        )
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_doctor, module)?)?;
    module.add_function(wrap_pyfunction!(execute_repair_active_view, module)?)
}
