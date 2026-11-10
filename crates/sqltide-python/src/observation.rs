//! Observability schema initialization and manifest publication: adapter-rendered SQL wrapped as
//! finalization statements and run through the Python observation workflow executor.

use pyo3::prelude::*;

use crate::direct_build::{Value, build, call, class, text, tuple};

const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const OBSERVATION_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_observation_workflow";

/// Run adapter-rendered observation SQL as numbered finalization mutations.
fn execute_rendered<'py>(connection: &Value<'py>, rendered: Value<'py>) -> PyResult<()> {
    let py: Python<'py> = connection.py();
    let finalization: Value<'py> =
        class(py, WORKFLOW_TYPES, "WorkflowPhase")?.getattr("FINALIZATION")?;
    let mutation: Value<'py> = class(py, WORKFLOW_TYPES, "StatementIntent")?.getattr("MUTATION")?;
    let mut statements: Vec<Value<'py>> = Vec::new();
    for (index, sql) in rendered.try_iter()?.enumerate() {
        let sequence: usize = index + 1;
        statements.push(build(
            py,
            WORKFLOW_MODELS,
            "WarehouseStatement",
            &[
                ("sequence", sequence.into_pyobject(py)?.into_any()),
                (
                    "step_id",
                    text(py, &format!("record_terminal_observation_{sequence}")),
                ),
                ("phase", finalization.clone()),
                ("intent", mutation.clone()),
                ("sql", sql?),
            ],
        )?);
    }
    call(
        py,
        OBSERVATION_WORKFLOW,
        "execute_observation_workflow",
        &[
            ("statements", tuple(py, statements)?),
            ("connection", connection.clone()),
        ],
    )?;
    Ok(())
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
