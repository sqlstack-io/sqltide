//! Observability schema initialization and manifest publication: adapter-rendered SQL wrapped as
//! finalization statements and run through the Python observation workflow executor.

use pyo3::prelude::*;

use crate::direct_build::Value;

/// Run adapter-rendered observation SQL as numbered finalization mutations.
fn execute_rendered<'py>(connection: &Value<'py>, rendered: Value<'py>) -> PyResult<()> {
    crate::run_events::execute_observations(
        connection,
        &crate::run_events::observation_statements(&rendered)?,
    )
}

/// Require the current observability schema before warehouse planning.
#[pyfunction]
#[pyo3(signature = (*, connection, database))]
fn initialize_observability(connection: &Value<'_>, database: &Value<'_>) -> PyResult<()> {
    if !database.is_truthy()? {
        return Ok(());
    }
    let rendered = connection.call_method1("render_migrate_metadata_state", (database,))?;
    execute_rendered(connection, rendered)
}

/// Append one manifest through the warehouse mutation gateway.
#[pyfunction]
#[pyo3(signature = (*, connection, database, manifest))]
fn publish_manifest(
    connection: &Value<'_>,
    database: &Value<'_>,
    manifest: &Value<'_>,
) -> PyResult<()> {
    if !database.is_truthy()? {
        return Ok(());
    }
    let arguments = pyo3::types::PyDict::new(connection.py());
    arguments.set_item("database", database)?;
    arguments.set_item("manifest", manifest)?;
    let rendered = connection.call_method("render_manifest_publication", (), Some(&arguments))?;
    execute_rendered(connection, rendered)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(initialize_observability, module)?)?;
    module.add_function(wrap_pyfunction!(publish_manifest, module)?)
}
