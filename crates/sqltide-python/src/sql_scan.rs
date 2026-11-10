//! Python bindings for `sqltide_sqltext::sql_scan`. A scan failure raises its SQL analysis
//! exception over the native `SqlScanError(kind, message, span)`.

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_sqltext::sql_scan::{
    self, RelationRewrite, ScanError, ScanErrorKind, SourceSpan, SqlReference,
};

use sqltide_sqlanalysis::{SqlDialect, select_aggregate_facts};

use crate::analysis::{analysis_error, rewrite_result};

create_exception!(_native, SqlScanError, PyValueError);

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

/// Rewrite relations, predicate and CTEs of one placeholder-qualified template through
/// byte-preserving text edits.
#[pyfunction]
#[pyo3(signature = (
    *, template, dialect, database_placeholder, relation_rewrites = Vec::new(), predicate = None,
    prepend_ctes = Vec::new()
))]
fn rewrite_template_query<'py>(
    py: Python<'py>,
    template: &str,
    dialect: &str,
    database_placeholder: &str,
    relation_rewrites: Vec<Bound<'py, PyAny>>,
    predicate: Option<&str>,
    prepend_ctes: Vec<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let rewrites: Vec<RelationRewrite> = relation_rewrites
        .iter()
        .map(|rewrite| {
            Ok(RelationRewrite {
                source_name: rewrite.getattr("source_name")?.extract()?,
                target_relation: rewrite.getattr("target_relation")?.extract()?,
                preserve_source_database: rewrite.getattr("preserve_source_database")?.extract()?,
            })
        })
        .collect::<PyResult<_>>()?;
    let mut rewritten: String =
        sql_scan::rewrite_template_relations(template, &rewrites, database_placeholder);
    if let Some(predicate) = predicate {
        rewritten = sql_scan::append_template_predicate(&chars(&rewritten), predicate)
            .map_err(scan_failure)?;
    }
    let named_queries: Vec<(String, String)> = prepend_ctes
        .iter()
        .map(|query| {
            Ok((
                query.getattr("name")?.extract()?,
                query.getattr("query")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    rewritten =
        sql_scan::merge_template_ctes(&chars(&rewritten), &named_queries).map_err(scan_failure)?;
    let (function_names, has_group_by) =
        select_aggregate_facts(&rewritten, &SqlDialect::new(dialect)).map_err(analysis_error)?;
    rewrite_result(py, rewritten, &function_names, has_group_by)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("SqlScanError", module.py().get_type::<SqlScanError>())?;
    module.add_function(wrap_pyfunction!(rewrite_template_query, module)?)?;
    Ok(())
}
