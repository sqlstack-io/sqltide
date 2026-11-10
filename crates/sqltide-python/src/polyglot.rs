//! Polyglot query analysis for authored projection-name inference.

use polyglot_sql::{AnalyzeQueryOptions, Dialect};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use serde_json::{Map, Value};

const EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";

create_exception!(_native, PolyglotError, PyValueError);

fn failure(error: impl ToString) -> PyErr {
    PolyglotError::new_err(error.to_string())
}

fn dialect(name: &str) -> PyResult<Dialect> {
    Dialect::get_by_name(name)
        .ok_or_else(|| PyValueError::new_err(format!("Unknown dialect: {name}")))
}

/// The dict entries of one list field of a compact query-analysis payload.
fn entries<'a>(payload: &'a Value, key: &str) -> Vec<&'a Map<String, Value>> {
    payload
        .get(key)
        .and_then(Value::as_array)
        .map(|values| values.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default()
}

/// The output column names one query projects; an unresolvable shape is an error message.
fn projection_names(facts: &Value) -> Result<Vec<String>, &'static str> {
    let projections: Vec<&Map<String, Value>> = entries(facts, "projections");
    if projections.is_empty() {
        return Err("Query must project at least one column");
    }
    let mut names: Vec<String> = Vec::new();
    for projection in projections {
        if projection.get("isStar") == Some(&Value::Bool(true)) {
            return Err(
                "Query uses a SELECT * whose columns cannot be resolved from the \
                        authored CTEs; alias every projection explicitly",
            );
        }
        match projection.get("name").and_then(Value::as_str) {
            Some(name) if !name.is_empty() => names.push(name.to_owned()),
            _ => return Err("Query must alias every projected column"),
        }
    }
    for set_operation in entries(facts, "setOperations") {
        let set_operation: Value = Value::Object(set_operation.clone());
        for branch in entries(&set_operation, "branches") {
            let branch: Value = Value::Object(branch.clone());
            if entries(&branch, "projections").len() != names.len() {
                return Err(
                    "Query must project the same column count in every set-operation branch",
                );
            }
        }
    }
    Ok(names)
}

/// `SqlAnalysisError(message)`, raised `from None` over `context` when one is given.
fn analysis_failure(py: Python<'_>, message: String, context: Option<PyErr>) -> PyResult<PyErr> {
    let failure: Bound<'_, PyAny> = py
        .import(EXCEPTIONS)?
        .getattr("SqlAnalysisError")?
        .call1((message,))?;
    if let Some(context) = context {
        failure.setattr("__context__", context.value(py))?;
        failure.setattr("__cause__", py.None())?;
    }
    Ok(PyErr::from_value(failure))
}

/// The output column names produced by one authored query.
#[pyfunction]
fn sql_infer_projection_names<'py>(
    py: Python<'py>,
    sql: &str,
    dialect_name: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let analyzed: PyResult<Value> = dialect(dialect_name).and_then(|dialect| {
        let options = AnalyzeQueryOptions {
            dialect: dialect.dialect_type(),
            ..AnalyzeQueryOptions::default()
        };
        let analysis = py
            .detach(|| polyglot_sql::analyze_query(sql, options))
            .map_err(failure)?;
        serde_json::to_value(&analysis).map_err(failure)
    });
    let facts: Value = match analyzed {
        Ok(facts) => facts,
        Err(error) => {
            let message: String = format!(
                "SQL could not be analyzed with Polyglot: {}",
                error.value(py).str()?
            );
            return Err(analysis_failure(py, message, Some(error))?);
        }
    };
    match projection_names(&facts) {
        Ok(names) => PyTuple::new(py, names),
        Err(message) => Err(analysis_failure(py, message.to_owned(), None)?),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PolyglotError", module.py().get_type::<PolyglotError>())?;
    module.add_function(wrap_pyfunction!(sql_infer_projection_names, module)?)?;
    Ok(())
}
