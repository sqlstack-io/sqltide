//! Python bindings for `sqltide_sqltext::sql_scan`. Results are plain tuples; Python wraps them in
//! its own models and turns `SqlScanError(kind, message, span)` into its exception types.

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_sqltext::sql_scan::{
    self, RelationRewrite, ScanError, ScanErrorKind, SourceSpan, SqlReference,
};

create_exception!(_native, SqlScanError, PyValueError);

pub(crate) type Span = (usize, usize, usize, usize, usize, usize);
pub(crate) type Reference = (String, &'static str, Option<&'static str>, Span);
type Cte = (String, String, Span);

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

fn chars(sql: &str) -> Vec<char> {
    sql.chars().collect()
}

#[pyfunction]
fn skip_trivia(sql: &str, start: usize) -> PyResult<usize> {
    sql_scan::skip_trivia(&chars(sql), start).map_err(raise)
}

#[pyfunction]
fn find_matching_parenthesis(sql: &str, open_index: usize, context: &str) -> PyResult<usize> {
    sql_scan::find_matching_parenthesis(&chars(sql), open_index, context).map_err(raise)
}

#[pyfunction]
fn find_next_unquoted_character(
    sql: &str,
    character: char,
    start: usize,
) -> PyResult<Option<usize>> {
    sql_scan::find_next_unquoted_character(&chars(sql), character, start).map_err(raise)
}

#[pyfunction]
fn try_consume_keyword(sql: &str, start: usize, keyword: &str) -> Option<usize> {
    sql_scan::try_consume_keyword(&chars(sql), start, keyword)
}

#[pyfunction]
fn normalized_statement_sql(sql: &str) -> PyResult<String> {
    sql_scan::normalized_statement_sql(&chars(sql)).map_err(raise)
}

#[pyfunction]
pub(crate) fn extract_references(sql: &str) -> PyResult<Vec<Reference>> {
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

#[pyfunction]
fn split_header_blocks(sql: &str, keyword: &str) -> PyResult<Vec<(usize, usize, String, String)>> {
    Ok(sql_scan::split_header_blocks(&chars(sql), keyword)
        .map_err(raise)?
        .into_iter()
        .map(|block| (block.start, block.body_start, block.header, block.body))
        .collect())
}

#[pyfunction]
fn extract_top_level_ctes(sql: &str, context: &str) -> PyResult<(Vec<Cte>, String, Span)> {
    let top_level = sql_scan::extract_top_level_ctes(&chars(sql), context).map_err(raise)?;
    let ctes = top_level
        .ctes
        .into_iter()
        .map(|cte| (cte.name, cte.query, span(cte.span)))
        .collect();
    Ok((ctes, top_level.trailing_sql, span(top_level.trailing_span)))
}

#[pyfunction]
fn outer_projection_spans(sql: &str) -> PyResult<Vec<Span>> {
    Ok(sql_scan::outer_projection_spans(&chars(sql))
        .map_err(raise)?
        .into_iter()
        .map(span)
        .collect())
}

#[pyfunction]
fn outer_star_span(sql: &str, start: usize, end: usize) -> PyResult<Option<Span>> {
    let sql: Vec<char> = chars(sql);
    let end: usize = end.min(sql.len());
    Ok(sql_scan::outer_star_span(&sql, start, end)
        .map_err(raise)?
        .map(span))
}

#[pyfunction]
fn outer_double_colon_type(sql: &str, start: usize, end: usize) -> PyResult<Option<String>> {
    let sql: Vec<char> = chars(sql);
    let end: usize = end.min(sql.len());
    sql_scan::outer_double_colon_type(&sql, start, end).map_err(raise)
}

#[pyfunction]
fn find_next_macro_start(sql: &str, start: usize) -> PyResult<Option<usize>> {
    sql_scan::find_next_macro_start(&chars(sql), start).map_err(raise)
}

#[pyfunction]
fn rewrite_template_relations(
    template: &str,
    rewrites: Vec<(String, String, bool)>,
    database_placeholder: &str,
) -> String {
    let rewrites: Vec<RelationRewrite> = rewrites
        .into_iter()
        .map(
            |(source_name, target_relation, preserve_source_database)| RelationRewrite {
                source_name,
                target_relation,
                preserve_source_database,
            },
        )
        .collect();
    sql_scan::rewrite_template_relations(template, &rewrites, database_placeholder)
}

#[pyfunction]
fn append_template_predicate(template: &str, predicate: &str) -> PyResult<String> {
    sql_scan::append_template_predicate(&chars(template), predicate).map_err(raise)
}

#[pyfunction]
fn merge_template_ctes(template: &str, named_queries: Vec<(String, String)>) -> PyResult<String> {
    sql_scan::merge_template_ctes(&chars(template), &named_queries).map_err(raise)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("SqlScanError", module.py().get_type::<SqlScanError>())?;
    module.add_function(wrap_pyfunction!(skip_trivia, module)?)?;
    module.add_function(wrap_pyfunction!(find_matching_parenthesis, module)?)?;
    module.add_function(wrap_pyfunction!(find_next_unquoted_character, module)?)?;
    module.add_function(wrap_pyfunction!(try_consume_keyword, module)?)?;
    module.add_function(wrap_pyfunction!(normalized_statement_sql, module)?)?;
    module.add_function(wrap_pyfunction!(extract_references, module)?)?;
    module.add_function(wrap_pyfunction!(split_header_blocks, module)?)?;
    module.add_function(wrap_pyfunction!(extract_top_level_ctes, module)?)?;
    module.add_function(wrap_pyfunction!(outer_projection_spans, module)?)?;
    module.add_function(wrap_pyfunction!(outer_star_span, module)?)?;
    module.add_function(wrap_pyfunction!(outer_double_colon_type, module)?)?;
    module.add_function(wrap_pyfunction!(find_next_macro_start, module)?)?;
    module.add_function(wrap_pyfunction!(rewrite_template_relations, module)?)?;
    module.add_function(wrap_pyfunction!(append_template_predicate, module)?)?;
    module.add_function(wrap_pyfunction!(merge_template_ctes, module)?)?;
    Ok(())
}
