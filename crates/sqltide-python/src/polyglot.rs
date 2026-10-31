//! The Polyglot entry points SQL analysis uses, returning the same serde trees and error text as
//! the `polyglot-sql` Python package built from this engine.

use polyglot_sql::{AnalyzeQueryOptions, Dialect, DialectType};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pythonize::pythonize;

create_exception!(_native, PolyglotError, PyValueError);

fn failure(error: impl ToString) -> PyErr {
    PolyglotError::new_err(error.to_string())
}

fn dialect(name: &str) -> PyResult<Dialect> {
    Dialect::get_by_name(name)
        .ok_or_else(|| PyValueError::new_err(format!("Unknown dialect: {name}")))
}

fn to_python<'py, T: serde::Serialize>(py: Python<'py>, value: &T) -> PyResult<Bound<'py, PyAny>> {
    pythonize(py, value).map_err(failure)
}

#[pyfunction]
fn polyglot_generate_data_type(sql: &str, dialect_name: &str) -> PyResult<String> {
    let dialect_type: DialectType = dialect(dialect_name)?.dialect_type();
    let data_type = polyglot_sql::parse_data_type(sql, dialect_type).map_err(failure)?;
    polyglot_sql::generate_data_type(&data_type, dialect_type).map_err(failure)
}

#[pyfunction]
fn polyglot_analyze_query<'py>(
    py: Python<'py>,
    sql: &str,
    dialect_name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let options = AnalyzeQueryOptions {
        dialect: dialect(dialect_name)?.dialect_type(),
        ..AnalyzeQueryOptions::default()
    };
    let analysis = py
        .detach(|| polyglot_sql::analyze_query(sql, options))
        .map_err(failure)?;
    to_python(py, &analysis)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PolyglotError", module.py().get_type::<PolyglotError>())?;
    module.add_function(wrap_pyfunction!(polyglot_generate_data_type, module)?)?;
    module.add_function(wrap_pyfunction!(polyglot_analyze_query, module)?)?;
    Ok(())
}
