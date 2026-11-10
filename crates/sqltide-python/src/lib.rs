//! The `sqltide._native` extension module: the only crate that knows about Python.

#![forbid(unsafe_code)]

mod access;
mod actual_state;
mod adopted_sources;
mod analysis;
mod artifacts;
mod audit_discovery;
mod clickhouse;
mod clickhouse_catalog;
mod clickhouse_lifecycle;
mod compilation;
mod compile;
mod compile_artifacts;
mod config;
mod destruction;
mod direct_plan;
mod discovery;
mod executor;
mod graph;
mod macros;
mod model_analysis;
mod model_sql;
mod pipeline_load;
mod plan_render;
mod planner;
mod planner_support;
mod polyglot;
mod population;
mod project_analysis;
mod project_discovery;
mod realization;
mod row_error_policy;
mod rules;
mod sources;
mod sql_scan;
mod test_discovery;
mod testing;
mod timestamps;
mod toml;
mod workflow;

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
    access::register(module)?;
    actual_state::register(module)?;
    adopted_sources::register(module)?;
    analysis::register(module)?;
    artifacts::register(module)?;
    audit_discovery::register(module)?;
    clickhouse::register(module)?;
    clickhouse_catalog::register(module)?;
    clickhouse_lifecycle::register(module)?;
    compilation::register(module)?;
    compile::register(module)?;
    compile_artifacts::register(module)?;
    graph::register(module)?;
    macros::register(module)?;
    model_analysis::register(module)?;
    model_sql::register(module)?;
    pipeline_load::register(module)?;
    discovery::register(module)?;
    config::register(module)?;
    destruction::register(module)?;
    direct_plan::register(module)?;
    executor::register(module)?;
    plan_render::register(module)?;
    planner::register(module)?;
    planner_support::register(module)?;
    polyglot::register(module)?;
    project_analysis::register(module)?;
    project_discovery::register(module)?;
    realization::register(module)?;
    population::register(module)?;
    row_error_policy::register(module)?;
    rules::register(module)?;
    sources::register(module)?;
    sql_scan::register(module)?;
    test_discovery::register(module)?;
    testing::register(module)?;
    timestamps::register(module)?;
    toml::register(module)?;
    workflow::register(module)
}
