//! Workflow statements and replay requests as the native workflow planners take them.
//! Statements return as `(step ID, phase, intent, SQL, continue on error)`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::virtual_workflow::{Mode, Replay};
use sqltide_executor::workflow::Statement;

/// `(step ID, phase, intent, SQL, continue on error)`.
type StatementResult = (String, &'static str, &'static str, String, bool);

fn field<'py, T: FromPyObject<'py>>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<T> {
    object.getattr(name)?.extract()
}

fn text(object: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    object.getattr(name)?.str()?.extract()
}

pub(crate) fn statement_results(statements: Vec<Statement>) -> Vec<StatementResult> {
    statements
        .into_iter()
        .map(|statement| {
            (
                statement.step_id,
                statement.phase.as_str(),
                statement.intent.as_str(),
                statement.sql,
                statement.continue_on_error,
            )
        })
        .collect()
}

/// The parts of an `AdapterReplayRequest` the workflow's own SQL reads.
pub(crate) fn replay(object: &Bound<'_, PyAny>) -> PyResult<Replay> {
    let mode_text: String = text(object, "mode")?;
    let mode: Mode = Mode::parse(&mode_text)
        .ok_or_else(|| PyValueError::new_err(format!("Unknown replay mode '{mode_text}'")))?;
    let relations = object.getattr("relations")?;
    let columns = object.getattr("columns")?;
    let window = object.getattr("window")?;
    Ok(Replay {
        mode,
        database: field(object, "database")?,
        root: field(&relations, "root")?,
        anchor: field(&relations, "anchor")?,
        target: field(&relations, "target")?,
        partition_column: field(&columns, "partition")?,
        offset_column: field(&columns, "offset")?,
        timestamp_column: field(&columns, "timestamp")?,
        landed_at_column: field(&columns, "landed_at")?,
        cursor_column: field(&columns, "cursor")?,
        forced_lower_bound: text(&window, "lower_bound_mode")? == "forced_time",
        forced_start_time: field(&window, "forced_start_time")?,
    })
}
