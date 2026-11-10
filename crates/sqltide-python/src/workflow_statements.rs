//! Warehouse statements for executor workflows: rendered SQL numbered into the exact
//! `WarehouseStatement` sequence the statement runner executes.

use pyo3::prelude::*;

use crate::dev_server::support::attr;
use crate::py_kwargs;

const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";

type Any<'py> = Bound<'py, PyAny>;

/// One mutation statement in workflow `phase`, a `WorkflowPhase` member name.
pub(crate) fn mutation<'py>(
    py: Python<'py>,
    sequence: usize,
    step_id: String,
    phase: &str,
    sql: Any<'py>,
) -> PyResult<Any<'py>> {
    attr(py, WORKFLOW_MODELS, "WarehouseStatement")?.call(
        (),
        Some(&py_kwargs!(py;
            sequence = sequence,
            step_id = step_id,
            phase = attr(py, WORKFLOW_TYPES, "WorkflowPhase")?.getattr(phase)?,
            intent = attr(py, WORKFLOW_TYPES, "StatementIntent")?.getattr("MUTATION")?,
            sql = sql,
        )?),
    )
}

/// Mutations for one rendered SQL batch, numbered from `first` and named
/// `{prefix}_0001` onwards.
pub(crate) fn numbered<'py>(
    rendered: &Any<'py>,
    first: usize,
    prefix: &str,
    phase: &str,
) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = rendered.py();
    rendered
        .try_iter()?
        .enumerate()
        .map(|(offset, sql)| {
            mutation(
                py,
                first + offset,
                format!("{prefix}_{:04}", offset + 1),
                phase,
                sql?,
            )
        })
        .collect()
}
