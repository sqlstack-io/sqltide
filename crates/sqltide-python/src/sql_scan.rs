//! Python bindings for `sqltide_sqltext::sql_scan`. A scan failure raises its SQL analysis
//! exception over the native `SqlScanError(kind, message, span)`.

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sqltide_sqltext::sql_scan::{
    self, RelationRewrite, ScanError, ScanErrorKind, SourceSpan, SqlReference,
};

create_exception!(_native, SqlScanError, PyValueError);

const MODELS: &str = "sqltide.compiler.sql_analysis.models";

pub(crate) type Span = (usize, usize, usize, usize, usize, usize);
pub(crate) type Reference = (String, &'static str, Option<&'static str>, Span);

pub(crate) fn span(span: SourceSpan) -> Span {
    (
        span.start,
        span.end,
        span.line,
        span.column,
        span.end_line,
        span.end_column,
    )
}

pub(crate) fn raise(error: ScanError) -> PyErr {
    let kind: &str = match error.kind {
        ScanErrorKind::Analysis => "analysis",
        ScanErrorKind::MissingWithClause => "missing_with",
    };
    SqlScanError::new_err((kind, error.message, error.span.map(span)))
}

/// The SQL analysis exception for one scan failure.
pub(crate) fn scan_failure(error: ScanError) -> PyErr {
    let native: PyErr = raise(error);
    Python::attach(|py| crate::model_analysis::domain(py, native))
}

fn chars(sql: &str) -> Vec<char> {
    sql.chars().collect()
}

#[pyfunction]
fn skip_trivia(sql: &str, start: usize) -> PyResult<usize> {
    sql_scan::skip_trivia(&chars(sql), start).map_err(scan_failure)
}

#[pyfunction]
fn find_matching_parenthesis(sql: &str, open_index: usize, context: &str) -> PyResult<usize> {
    sql_scan::find_matching_parenthesis(&chars(sql), open_index, context).map_err(scan_failure)
}

#[pyfunction]
fn find_next_unquoted_character(
    sql: &str,
    character: char,
    start: usize,
) -> PyResult<Option<usize>> {
    sql_scan::find_next_unquoted_character(&chars(sql), character, start).map_err(scan_failure)
}

#[pyfunction]
fn try_consume_keyword(sql: &str, start: usize, keyword: &str) -> Option<usize> {
    sql_scan::try_consume_keyword(&chars(sql), start, keyword)
}

/// `(name, relation type, ref type, span)` of each logical reference, or the raw scan failure.
pub(crate) fn scan_references(sql: &str) -> PyResult<Vec<Reference>> {
    let references: Vec<SqlReference> = sql_scan::extract_references(&chars(sql)).map_err(raise)?;
    Ok(references
        .into_iter()
        .map(|reference| {
            (
                reference.name,
                reference.relation_type,
                reference.ref_type,
                span(reference.span),
            )
        })
        .collect())
}

/// Line-leading extension headers and their SQL bodies as `SqlHeaderBlock`s.
#[pyfunction]
fn split_header_blocks<'py>(
    py: Python<'py>,
    sql: &str,
    keyword: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let class: Bound<'py, PyAny> = py.import(MODELS)?.getattr("SqlHeaderBlock")?;
    let blocks: Vec<Bound<'py, PyAny>> = sql_scan::split_header_blocks(&chars(sql), keyword)
        .map_err(scan_failure)?
        .into_iter()
        .map(|block| class.call1((block.start, block.body_start, block.header, block.body)))
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, blocks)
}

/// One leading WITH clause's CTEs and trailing statement as `SqlTopLevelCtes`.
#[pyfunction]
fn extract_top_level_ctes<'py>(
    py: Python<'py>,
    sql: &str,
    context: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let top_level = sql_scan::extract_top_level_ctes(&chars(sql), context).map_err(scan_failure)?;
    let models: Bound<'py, PyModule> = py.import(MODELS)?;
    let span_class: Bound<'py, PyAny> = models.getattr("SqlSourceSpan")?;
    let cte_class: Bound<'py, PyAny> = models.getattr("SqlCommonTableExpression")?;
    let ctes: Vec<Bound<'py, PyAny>> = top_level
        .ctes
        .into_iter()
        .map(|cte| cte_class.call1((cte.name, cte.query, span_class.call1(span(cte.span))?)))
        .collect::<PyResult<_>>()?;
    models.getattr("SqlTopLevelCtes")?.call1((
        PyTuple::new(py, ctes)?,
        top_level.trailing_sql,
        span_class.call1(span(top_level.trailing_span))?,
    ))
}

#[pyfunction]
fn find_next_macro_start(sql: &str, start: usize) -> PyResult<Option<usize>> {
    sql_scan::find_next_macro_start(&chars(sql), start).map_err(scan_failure)
}

/// `template` with each `SqlRelationRewrite`'s placeholder-qualified relation replaced.
#[pyfunction]
fn rewrite_template_relations(
    template: &str,
    rewrites: Vec<Bound<'_, PyAny>>,
    database_placeholder: &str,
) -> PyResult<String> {
    let rewrites: Vec<RelationRewrite> = rewrites
        .iter()
        .map(|rewrite| {
            Ok(RelationRewrite {
                source_name: rewrite.getattr("source_name")?.extract()?,
                target_relation: rewrite.getattr("target_relation")?.extract()?,
                preserve_source_database: rewrite.getattr("preserve_source_database")?.extract()?,
            })
        })
        .collect::<PyResult<_>>()?;
    Ok(sql_scan::rewrite_template_relations(
        template,
        &rewrites,
        database_placeholder,
    ))
}

#[pyfunction]
fn append_template_predicate(template: &str, predicate: &str) -> PyResult<String> {
    sql_scan::append_template_predicate(&chars(template), predicate).map_err(scan_failure)
}

#[pyfunction]
fn merge_template_ctes(template: &str, named_queries: Vec<Bound<'_, PyAny>>) -> PyResult<String> {
    let named_queries: Vec<(String, String)> = named_queries
        .iter()
        .map(|query| {
            Ok((
                query.getattr("name")?.extract()?,
                query.getattr("query")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    sql_scan::merge_template_ctes(&chars(template), &named_queries).map_err(scan_failure)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("SqlScanError", module.py().get_type::<SqlScanError>())?;
    module.add_function(wrap_pyfunction!(skip_trivia, module)?)?;
    module.add_function(wrap_pyfunction!(find_matching_parenthesis, module)?)?;
    module.add_function(wrap_pyfunction!(find_next_unquoted_character, module)?)?;
    module.add_function(wrap_pyfunction!(try_consume_keyword, module)?)?;
    module.add_function(wrap_pyfunction!(split_header_blocks, module)?)?;
    module.add_function(wrap_pyfunction!(extract_top_level_ctes, module)?)?;
    module.add_function(wrap_pyfunction!(find_next_macro_start, module)?)?;
    module.add_function(wrap_pyfunction!(rewrite_template_relations, module)?)?;
    module.add_function(wrap_pyfunction!(append_template_predicate, module)?)?;
    module.add_function(wrap_pyfunction!(merge_template_ctes, module)?)?;
    Ok(())
}
