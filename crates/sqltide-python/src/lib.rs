//! The `sqltide._native` extension module: the only crate that knows about Python.

#![forbid(unsafe_code)]

mod analysis;
mod artifacts;
mod clickhouse;
mod config;
mod destruction;
mod discovery;
mod executor;
mod graph;
mod macros;
mod planner;
mod polyglot;
mod population;
mod sql_scan;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList};
use sqltide_sqltext::model_header::{HeaderValue, parse_model_header as parse_header};

/// Parse MODEL header text into a dict, raising `ValueError` with the syntax error message.
#[pyfunction]
fn parse_model_header<'py>(py: Python<'py>, header: &str) -> PyResult<Bound<'py, PyAny>> {
    let entries = parse_header(header).map_err(PyValueError::new_err)?;
    map_object(py, &entries)
}

fn map_object<'py>(
    py: Python<'py>,
    entries: &[(String, HeaderValue)],
) -> PyResult<Bound<'py, PyAny>> {
    let mapping = PyDict::new(py);
    for (key, value) in entries {
        mapping.set_item(key, value_object(py, value)?)?;
    }
    Ok(mapping.into_any())
}

fn value_object<'py>(py: Python<'py>, value: &HeaderValue) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        HeaderValue::Null => py.None().into_bound(py),
        HeaderValue::Bool(flag) => flag.into_pyobject(py)?.to_owned().into_any(),
        HeaderValue::Integer(digits) => py
            .import("builtins")?
            .getattr("int")?
            .call1((digits.as_str(),))?,
        HeaderValue::Float(number) => PyFloat::new(py, *number).into_any(),
        HeaderValue::Text(text) | HeaderValue::Word(text) => text.into_pyobject(py)?.into_any(),
        HeaderValue::List(items) | HeaderValue::Set(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(value_object(py, item)?)?;
            }
            list.into_any()
        }
        HeaderValue::Map(entries) => map_object(py, entries)?,
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(parse_model_header, module)?)?;
    analysis::register(module)?;
    artifacts::register(module)?;
    clickhouse::register(module)?;
    graph::register(module)?;
    macros::register(module)?;
    discovery::register(module)?;
    config::register(module)?;
    destruction::register(module)?;
    executor::register(module)?;
    planner::register(module)?;
    polyglot::register(module)?;
    population::register(module)?;
    sql_scan::register(module)
}
