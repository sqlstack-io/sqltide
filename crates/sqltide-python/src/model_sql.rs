//! Python bindings that load one authored SQL model file into its `TransformStep` or `ViewStep`,
//! and extract its logical references as `ParsedRef`s.

use std::collections::BTreeSet;

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sqltide_sqltext::python_text::python_strip;

use crate::config::{check_model_header_keys, model_header_block, parse_model_spec};
use crate::model_analysis::domain;
use crate::sql_scan::{Reference, scan_references};

const DISCOVERY_EXCEPTIONS: &str = "sqltide.compiler.discovery.exceptions";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const COMPILE_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const SQL_ANALYSIS_MODELS: &str = "sqltide.compiler.sql_analysis.models";
const DIAGNOSTICS_MODELS: &str = "sqltide.diagnostics.models";
const DIAGNOSTICS_TYPES: &str = "sqltide.diagnostics.types";
const MACRO_EXPANSION: &str = "sqltide.compiler.macros.main._expand_macro_calls";
const SOURCE_RELATION: &str = "source";
const REF_RELATION: &str = "ref";
const VIEW_KIND: &str = "view";
const DISCOVERY_DIAGNOSTIC_CODE: &str = "SQT-DISCOVERY-001";

pub(crate) fn call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(callable.py());
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

pub(crate) fn object<'py, T: IntoPyObjectExt<'py>>(
    py: Python<'py>,
    value: T,
) -> PyResult<Bound<'py, PyAny>> {
    value.into_bound_py_any(py)
}

/// `PipelineDiscoveryError(message)`.
pub(crate) fn discovery_error(py: Python<'_>, message: String) -> PyErr {
    match py
        .import(DISCOVERY_EXCEPTIONS)
        .and_then(|module| module.getattr("PipelineDiscoveryError"))
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(failure) => failure,
    }
}

/// `error` raised over `context`: `raise error from cause` when `cause` is set, otherwise
/// `raise error from None`.
pub(crate) fn chained(
    error: &Bound<'_, PyAny>,
    context: &Bound<'_, PyAny>,
    cause: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    error.setattr("__context__", context)?;
    match cause {
        Some(cause) => error.setattr("__cause__", cause),
        None => error.setattr("__cause__", error.py().None()),
    }
}

/// The absolute source location of one span relative to a query starting at `line:column`.
fn source_location<'py>(
    py: Python<'py>,
    source_path: &Bound<'py, PyAny>,
    line: i64,
    column: i64,
    span: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let relative_line: i64 = span.getattr("line")?.extract()?;
    let relative_column: i64 = span.getattr("column")?.extract()?;
    let relative_end_line: i64 = span.getattr("end_line")?.extract()?;
    let relative_end_column: i64 = span.getattr("end_column")?.extract()?;
    let absolute = |relative_line: i64, relative_column: i64| -> (i64, i64) {
        (
            line + relative_line - 1,
            if relative_line == 1 {
                column + relative_column - 1
            } else {
                relative_column
            },
        )
    };
    let (start_line, start_column) = absolute(relative_line, relative_column);
    let (end_line, end_column) = absolute(relative_end_line, relative_end_column);
    call(
        &py.import(DIAGNOSTICS_MODELS)?.getattr("SourceLocation")?,
        &[
            ("path", source_path.clone()),
            ("line", object(py, start_line)?),
            ("column", object(py, start_column)?),
            ("end_line", object(py, end_line)?),
            ("end_column", object(py, end_column)?),
        ],
    )
}

/// The logical references of `sql`. A SQL analysis failure raises `PipelineCompileError` with its
/// span and, when `source_path` is known, its absolute location, `from None`.
fn references(
    py: Python<'_>,
    sql: &str,
    source_path: Option<&Bound<'_, PyAny>>,
    line: i64,
    column: i64,
) -> PyResult<Vec<Reference>> {
    let failure: PyErr = match scan_references(sql) {
        Ok(references) => return Ok(references),
        Err(failure) => domain(py, failure),
    };
    let analysis_error: Bound<'_, PyAny> = py
        .import(SQL_ANALYSIS_EXCEPTIONS)?
        .getattr("SqlAnalysisError")?;
    if !failure.is_instance(py, &analysis_error) {
        return Err(failure);
    }
    let error: Bound<'_, PyAny> = failure.value(py).clone().into_any();
    let span: Bound<'_, PyAny> = error.getattr("span")?;
    let location: Bound<'_, PyAny> = match source_path {
        Some(source_path) if !span.is_none() => {
            source_location(py, source_path, line, column, &span)?
        }
        _ => py.None().into_bound(py),
    };
    let compile_error: Bound<'_, PyAny> = py
        .import(COMPILE_EXCEPTIONS)?
        .getattr("PipelineCompileError")?;
    let keywords: Bound<'_, PyDict> = PyDict::new(py);
    keywords.set_item("span", span)?;
    keywords.set_item("location", location)?;
    let raised: Bound<'_, PyAny> = compile_error.call((error.str()?,), Some(&keywords))?;
    chained(&raised, &error, None)?;
    Err(PyErr::from_value(raised))
}

/// `[ParsedRef]` for the logical `__source(...)` and `__ref(...)` references of `sql`.
#[pyfunction]
#[pyo3(signature = (*, sql, source_path=None, source_line=1, source_column=1))]
fn extract_refs<'py>(
    py: Python<'py>,
    sql: &str,
    source_path: Option<Bound<'py, PyAny>>,
    source_line: i64,
    source_column: i64,
) -> PyResult<Bound<'py, PyList>> {
    let scanned: Vec<Reference> =
        references(py, sql, source_path.as_ref(), source_line, source_column)?;
    let parsed_ref: Bound<'py, PyAny> = py.import(COMPILE_MODELS)?.getattr("ParsedRef")?;
    let ref_types: Bound<'py, PyAny> = py.import(DISCOVERY_TYPES)?.getattr("RefType")?;
    let span_class: Bound<'py, PyAny> = py.import(SQL_ANALYSIS_MODELS)?.getattr("SqlSourceSpan")?;
    let refs: Vec<Bound<'py, PyAny>> = scanned
        .into_iter()
        .map(|(name, relation_type, ref_type, location)| {
            call(
                &parsed_ref,
                &[
                    ("name", object(py, name)?),
                    ("relation_type", object(py, relation_type)?),
                    (
                        "ref_type",
                        match ref_type {
                            Some(ref_type) => ref_types.call1((ref_type,))?,
                            None => py.None().into_bound(py),
                        },
                    ),
                    ("span", span_class.call1(location)?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    PyList::new(py, refs)
}

fn names(references: &[Reference], keep: impl Fn(&Reference) -> bool) -> BTreeSet<&str> {
    references
        .iter()
        .filter(|reference| keep(reference))
        .map(|reference| reference.0.as_str())
        .collect()
}

fn only(names: &BTreeSet<&str>) -> String {
    names
        .iter()
        .next()
        .map(|name| (*name).to_owned())
        .unwrap_or_default()
}

/// The driving input of a table model: its single `__source(...)`, or its single untyped
/// `__ref(...)`.
#[pyfunction]
#[pyo3(signature = (*, query, file_path, source_line, source_column))]
fn infer_transform_source(
    py: Python<'_>,
    query: &str,
    file_path: &Bound<'_, PyAny>,
    source_line: i64,
    source_column: i64,
) -> PyResult<String> {
    let path: String = file_path.str()?.extract()?;
    let refs: Vec<Reference> = references(py, query, Some(file_path), source_line, source_column)?;
    if refs.is_empty() {
        return Err(discovery_error(
            py,
            format!(
                "SQL model '{path}' must reference exactly one driving input using \
                 __source(...) or __ref(...)"
            ),
        ));
    }
    let unique: BTreeSet<&str> = names(&refs, |_| true);
    let sources: BTreeSet<&str> = names(&refs, |reference| reference.1 == SOURCE_RELATION);
    let driving: BTreeSet<&str> = names(&refs, |reference| reference.2.is_none());
    if !sources.is_empty() {
        if sources.len() != 1 {
            return Err(discovery_error(
                py,
                format!(
                    "SQL model '{path}' must contain exactly one unique __source(...) when \
                     source refs are present"
                ),
            ));
        }
        let untyped_refs: BTreeSet<&str> = names(&refs, |reference| {
            reference.1 == REF_RELATION && reference.2.is_none()
        });
        if !untyped_refs.is_empty() {
            return Err(discovery_error(
                py,
                format!("SQL model '{path}' must not mix __source(...) with untyped __ref(...)"),
            ));
        }
        return Ok(only(&sources));
    }
    if unique.len() == 1 && refs.iter().all(|reference| reference.2.is_some()) {
        return Err(discovery_error(
            py,
            format!(
                "SQL model '{path}' must not declare ref_type for its driving input '{}'",
                only(&unique)
            ),
        ));
    }
    if driving.len() != 1 {
        return Err(discovery_error(
            py,
            format!(
                "SQL model '{path}' must contain exactly one unique untyped driving input when \
                 multiple relations are present"
            ),
        ));
    }
    Ok(only(&driving))
}

fn validate_view_refs(
    py: Python<'_>,
    query: &str,
    file_path: &Bound<'_, PyAny>,
    source_line: i64,
    source_column: i64,
) -> PyResult<()> {
    let refs: Vec<Reference> = references(py, query, Some(file_path), source_line, source_column)?;
    let typed: BTreeSet<&str> = names(&refs, |reference| reference.2.is_some());
    if typed.is_empty() {
        return Ok(());
    }
    Err(discovery_error(
        py,
        format!(
            "View MODEL(...) in '{}' cannot declare ref_type annotations for: {}",
            file_path.str()?,
            typed.into_iter().collect::<Vec<&str>>().join(", ")
        ),
    ))
}

/// The parsed `MODEL(...)` header of one file, its macro-expanded query, and the query's
/// 1-based line and column.
fn parse_model_sql<'py>(
    py: Python<'py>,
    contents: &str,
    file_path: &Bound<'py, PyAny>,
    macro_registry: Option<&Bound<'py, PyAny>>,
    macro_context: Option<&Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyAny>, String, usize, usize)> {
    let path: String = file_path.str()?.extract()?;
    let Some((header, raw_query, line, column)) = model_header_block(contents) else {
        return Err(discovery_error(
            py,
            format!(
                "SQL model '{path}' must start with a MODEL(...) header as the first \
                 non-whitespace content"
            ),
        ));
    };
    let header: Bound<'py, PyAny> = match crate::parse_model_header(py, &header) {
        Ok(header) => header,
        Err(failure) => {
            let syntax_error: Bound<'py, PyAny> = py
                .import(DISCOVERY_EXCEPTIONS)?
                .getattr("ModelHeaderSyntaxError")?
                .call1((failure.value(py).str()?,))?;
            chained(&syntax_error, failure.value(py).as_any(), None)?;
            let error: Bound<'py, PyAny> = py
                .import(DISCOVERY_EXCEPTIONS)?
                .getattr("PipelineDiscoveryError")?
                .call1((format!(
                    "MODEL(...) in '{path}' contains invalid SQLBuild header syntax: {}",
                    syntax_error.str()?
                ),))?;
            chained(&error, &syntax_error, Some(&syntax_error))?;
            return Err(PyErr::from_value(error));
        }
    };
    check_model_header_keys(py, &header, &path)?;
    let characters: Vec<char> = raw_query.chars().collect();
    let stripped: String = python_strip(&characters).iter().collect();
    let query: String = match (macro_registry, macro_context) {
        (Some(registry), Some(context)) => call(
            &py.import(MACRO_EXPANSION)?.getattr("expand_macro_calls")?,
            &[
                ("sql", object(py, stripped)?),
                ("file_path", file_path.clone()),
                ("registry", registry.clone()),
                ("context", context.clone()),
                ("source_line", object(py, line)?),
                ("source_column", object(py, column)?),
            ],
        )?
        .extract()?,
        _ => stripped,
    };
    if query.is_empty() {
        return Err(discovery_error(
            py,
            format!("SQL model '{path}' must contain a SELECT query after MODEL(...)"),
        ));
    }
    Ok((header, query, line, column))
}

/// The parsed `MODEL(...)` header of one SQL model file and its macro-expanded query.
#[pyfunction(name = "parse_model_sql")]
#[pyo3(signature = (*, contents, file_path, macro_registry=None, macro_context=None))]
fn parse_model_sql_py<'py>(
    py: Python<'py>,
    contents: &str,
    file_path: &Bound<'py, PyAny>,
    macro_registry: Option<Bound<'py, PyAny>>,
    macro_context: Option<Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyAny>, String)> {
    let (header, query, _, _) = parse_model_sql(
        py,
        contents,
        file_path,
        macro_registry.as_ref(),
        macro_context.as_ref(),
    )?;
    Ok((header, query))
}

fn load_transform<'py>(
    py: Python<'py>,
    file_path: &Bound<'py, PyAny>,
    contents: &str,
    macro_registry: Option<&Bound<'py, PyAny>>,
    macro_context: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let (header, query, line, column) =
        parse_model_sql(py, contents, file_path, macro_registry, macro_context)?;
    let (query_line, query_column): (i64, i64) = (line.try_into()?, column.try_into()?);
    let mut check_query = |kind: &str| -> PyResult<Option<String>> {
        if kind == VIEW_KIND {
            validate_view_refs(py, &query, file_path, query_line, query_column)?;
            return Ok(None);
        }
        infer_transform_source(py, &query, file_path, query_line, query_column).map(Some)
    };
    let (kind, fields) = parse_model_spec(
        py,
        &header,
        &file_path.str()?.extract::<String>()?,
        &mut check_query,
    )?;
    fields.set_item("name", file_path.getattr("stem")?)?;
    fields.set_item("query", query)?;
    fields.set_item("source_file_path", file_path)?;
    fields.set_item("source_line", line)?;
    fields.set_item("source_column", column)?;
    let class: &str = if kind == VIEW_KIND {
        "ViewStep"
    } else {
        "TransformStep"
    };
    py.import(DISCOVERY_MODELS)?
        .getattr(class)?
        .call((), Some(&fields))
}

/// Load one authored SQL model file into its kind-specific model step. A discovery failure
/// carries a discovery diagnostic at the start of the file.
#[pyfunction]
#[pyo3(signature = (*, file_path, contents=None, macro_registry=None, macro_context=None))]
pub(crate) fn load_transform_from_sql_file<'py>(
    py: Python<'py>,
    file_path: &Bound<'py, PyAny>,
    contents: Option<String>,
    macro_registry: Option<Bound<'py, PyAny>>,
    macro_context: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let contents: String = match contents {
        Some(contents) => contents,
        None => {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("encoding", "utf-8")?;
            file_path
                .call_method("read_text", (), Some(&keywords))?
                .extract()?
        }
    };
    let failure: PyErr = match load_transform(
        py,
        file_path,
        &contents,
        macro_registry.as_ref(),
        macro_context.as_ref(),
    ) {
        Ok(step) => return Ok(step),
        Err(failure) => failure,
    };
    let discovery: Bound<'py, PyAny> = py
        .import(DISCOVERY_EXCEPTIONS)?
        .getattr("PipelineDiscoveryError")?;
    if failure.is_instance(py, &discovery) {
        let types: Bound<'py, PyModule> = py.import(DIAGNOSTICS_TYPES)?;
        let models: Bound<'py, PyModule> = py.import(DIAGNOSTICS_MODELS)?;
        let location: Bound<'py, PyAny> = call(
            &models.getattr("SourceLocation")?,
            &[
                ("path", file_path.clone()),
                ("line", object(py, 1)?),
                ("column", object(py, 1)?),
            ],
        )?;
        let diagnostic: Bound<'py, PyAny> = call(
            &models.getattr("CompilerDiagnostic")?,
            &[
                (
                    "phase",
                    types.getattr("DiagnosticPhase")?.getattr("DISCOVERY")?,
                ),
                (
                    "severity",
                    types.getattr("DiagnosticSeverity")?.getattr("ERROR")?,
                ),
                ("code", object(py, DISCOVERY_DIAGNOSTIC_CODE)?),
                ("message", failure.value(py).str()?.into_any()),
                ("resource_name", file_path.getattr("stem")?),
                ("location", location),
            ],
        )?;
        failure.value(py).setattr("diagnostic", diagnostic)?;
    }
    Err(failure)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(extract_refs, module)?)?;
    module.add_function(wrap_pyfunction!(infer_transform_source, module)?)?;
    module.add_function(wrap_pyfunction!(parse_model_sql_py, module)?)?;
    module.add_function(wrap_pyfunction!(load_transform_from_sql_file, module)?)
}
