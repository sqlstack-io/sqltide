//! Workflow recovery after a failed or interrupted build: run each recovery statement in
//! order, record recovery failures as notes on the original failure, and keep managed sources
//! paused when a model view could not be restored.

use pyo3::exceptions::PyKeyboardInterrupt;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::dev_server::support::attr;
use crate::py_kwargs;

const CONSTANTS: &str = "sqltide.executor.workflow.constants";

type Any<'py> = Bound<'py, PyAny>;

fn prefix(py: Python<'_>, name: &str) -> PyResult<String> {
    attr(py, CONSTANTS, name)?.extract()
}

/// Run recovery statements in order and return the ones attempted, renumbered from
/// `start_sequence`.
#[pyfunction]
#[pyo3(signature = (
    *, statements, connection, emitter, cause, start_sequence, completed_step_ids, retry_command
))]
#[allow(clippy::too_many_arguments)]
fn execute_recovery_statements<'py>(
    statements: &Any<'py>,
    connection: &Any<'py>,
    emitter: &Any<'py>,
    cause: &Any<'py>,
    start_sequence: usize,
    completed_step_ids: Option<Any<'py>>,
    retry_command: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = connection.py();
    let source_prefix: String = prefix(py, "RECOVER_SOURCE_STEP_PREFIX")?;
    let view_prefix: String = prefix(py, "RECOVER_MODEL_VIEW_STEP_PREFIX")?;
    let dead_letters_prefix: String = prefix(py, "RECOVER_DEAD_LETTERS_STEP_PREFIX")?;
    let replace: Any<'py> = attr(py, "dataclasses", "replace")?;
    let run: Any<'py> = attr(
        py,
        "sqltide.executor.workflow.main._execute_warehouse_workflow",
        "execute_warehouse_workflow",
    )?;
    let adapter_error: Any<'py> = attr(py, "sqltide.adapter.exceptions", "AdapterError")?;
    let workflow_error: Any<'py> = attr(
        py,
        "sqltide.executor.workflow.exceptions",
        "WorkflowExecutionError",
    )?;
    let mut recovered: Vec<Any<'py>> = Vec::new();
    let mut view_restore_failed: bool = false;
    for recovery_statement in statements.try_iter()? {
        let recovery_statement: Any<'py> = recovery_statement?;
        let step_id: String = recovery_statement.getattr("step_id")?.extract()?;
        if view_restore_failed && step_id.starts_with(&source_prefix) {
            cause.call_method1(
                "add_note",
                (format!(
                    "Managed source recovery '{step_id}' was skipped: the source stays paused \
                     because a model view could not be restored, so no rows are lost. Run \
                     `{retry_command}` once the cause is fixed to resume it"
                ),),
            )?;
            continue;
        }
        if let Some(view) = step_id.strip_prefix(&dead_letters_prefix) {
            let may_be_live: bool = match &completed_step_ids {
                None => true,
                Some(completed) => completed.contains(format!("realize_{view}"))?,
            };
            if may_be_live {
                continue;
            }
        }
        let statement: Any<'py> = replace.call(
            (&recovery_statement,),
            Some(&py_kwargs!(py; sequence = start_sequence + recovered.len())?),
        )?;
        let attempt: PyResult<Any<'py>> = run.call(
            (),
            Some(&py_kwargs!(py;
                statements = PyTuple::new(py, [&statement])?,
                connection = connection,
                emitter = emitter,
            )?),
        );
        if let Err(error) = attempt {
            let raised: Any<'py> = error.value(py).clone().into_any();
            let is_workflow: bool = raised.is_instance(&workflow_error)?;
            if !(is_workflow
                || raised.is_instance(&adapter_error)?
                || error.is_instance_of::<PyKeyboardInterrupt>(py))
            {
                return Err(error);
            }
            let recovery_cause: Any<'py> = if is_workflow {
                raised.getattr("cause")?
            } else {
                raised
            };
            let statement_step: String = statement.getattr("step_id")?.extract()?;
            cause.call_method1(
                "add_note",
                (format!(
                    "Managed source recovery '{statement_step}' also failed: {}",
                    recovery_cause.str()?
                ),),
            )?;
            view_restore_failed = view_restore_failed || statement_step.starts_with(&view_prefix);
        }
        recovered.push(statement);
    }
    PyTuple::new(py, recovered)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_recovery_statements, module)?)
}
