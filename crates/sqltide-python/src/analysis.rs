//! Python bindings for `sqltide_sqlanalysis`. Parsed trees stay in Rust; Python receives compact
//! facts and generated SQL, and turns `SqlTreeError(kind, *details)` into its exception types.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sqltide_sqlanalysis::{
    AnalysisError, CatalogAnalysis, ExpectedReference, ModelError, NamedQuery, ParsedModel,
    Projection, ProjectionTypeCache, RelationCache, RelationIdentity, RelationRewrite,
    RewrittenQuery, SqlDialect, analyze_catalog, analyze_model,
    canonical_query_with_database_template, canonicalize_sql, expression_list,
    render_string_literal, rewrite_query, select_aggregate_facts, storage_column_names,
    validate_insert_query, validate_query, validated_relation_rewrite,
};
use sqltide_sqltext::sql_scan::SourceSpan;

use crate::sql_scan::{Span, raise as raise_scan, span};

create_exception!(_native, SqlTreeError, PyValueError);

/// `(relation type, name, ref type, start, end)` for one lexically scanned reference.
type Reference = (String, String, Option<String>, usize, usize);
type NativeProjection = (usize, String, String, Span);

fn analysis_error(error: AnalysisError) -> PyErr {
    SqlTreeError::new_err(("analysis", error.0))
}

fn model_error(py: Python<'_>, error: ModelError) -> PyResult<PyErr> {
    let details: Bound<'_, PyTuple> = match error {
        ModelError::Analysis(error) => return Ok(analysis_error(error)),
        ModelError::Scan(error) => return Ok(raise_scan(error)),
        ModelError::StatementCount(count) => ("statement_count", count).into_pyobject(py)?,
        ModelError::QueryShape {
            statement_type,
            statement_sql,
            is_set_operation,
        } => (
            "query_shape",
            statement_type,
            statement_sql,
            is_set_operation,
        )
            .into_pyobject(py)?,
        ModelError::UntypedProjection {
            index,
            projection_sql,
            span: location,
        } => ("untyped_projection", index, projection_sql, span(location)).into_pyobject(py)?,
        ModelError::StarProjection {
            index,
            span: location,
        } => ("star_projection", index, span(location)).into_pyobject(py)?,
        ModelError::DuplicateAlias {
            alias,
            span: location,
        } => ("duplicate_alias", alias, span(location)).into_pyobject(py)?,
    };
    Ok(SqlTreeError::new_err(details.unbind()))
}

/// One name's target in a Python resolver mapping, read on demand rather than copied whole.
fn lookup(resolver: &Bound<'_, PyAny>, name: &str) -> Option<String> {
    resolver.get_item(name).ok()?.extract().ok()
}

fn expected_references(references: Vec<Reference>) -> Vec<ExpectedReference> {
    references
        .into_iter()
        .map(
            |(relation_type, name, ref_type, start, end)| ExpectedReference {
                relation_type,
                name,
                ref_type,
                start,
                end,
            },
        )
        .collect()
}

/// One analyzed model whose parsed tree stays native until it is resolved.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct SqlModelTree {
    model: ParsedModel,
}

#[pymethods]
impl SqlModelTree {
    #[getter]
    fn function_names(&self) -> Vec<String> {
        self.model.function_names.clone()
    }

    #[getter]
    fn has_group_by(&self) -> bool {
        self.model.has_group_by
    }

    /// `(index, name, type, span)` for each typed outer projection.
    #[getter]
    fn projections(&self) -> Vec<NativeProjection> {
        self.model
            .projections
            .iter()
            .map(|projection: &Projection| {
                let location: SourceSpan = projection.span;
                (
                    projection.index,
                    projection.name.clone(),
                    projection.type_sql.clone(),
                    span(location),
                )
            })
            .collect()
    }

    fn uses_prewhere(&self) -> bool {
        self.model.uses_prewhere()
    }
}

struct AnalyzerState {
    type_cache: ProjectionTypeCache,
    relations: RelationCache,
}

/// Invocation-scoped SQL tree analysis for one dialect, owning its relation and type caches.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct SqlTreeAnalyzer {
    dialect: SqlDialect,
    state: Mutex<AnalyzerState>,
}

impl SqlTreeAnalyzer {
    fn state(&self) -> MutexGuard<'_, AnalyzerState> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

#[pymethods]
impl SqlTreeAnalyzer {
    #[new]
    fn new(dialect: &str) -> Self {
        Self {
            dialect: SqlDialect::new(dialect),
            state: Mutex::new(AnalyzerState {
                type_cache: ProjectionTypeCache::new(),
                relations: RelationCache::default(),
            }),
        }
    }

    /// Parse one model as a SELECT and validate its typed outer projections.
    fn analyze_model(&self, py: Python<'_>, sql: &str) -> PyResult<SqlModelTree> {
        let result = py.detach(|| analyze_model(sql, &self.dialect, &mut self.state().type_cache));
        match result {
            Ok(model) => Ok(SqlModelTree { model }),
            Err(error) => Err(model_error(py, error)?),
        }
    }

    /// Resolve one analyzed model into `(canonical_sql, database_template)`.
    fn resolve_model(
        &self,
        model: &SqlModelTree,
        authored_sql: &str,
        references: Vec<Reference>,
        resolver: &Bound<'_, PyAny>,
        database_placeholder: &str,
    ) -> PyResult<(String, String)> {
        let references: Vec<ExpectedReference> = expected_references(references);
        self.state()
            .relations
            .resolve(
                &model.model,
                authored_sql,
                &references,
                &|name| lookup(resolver, name),
                &self.dialect,
                database_placeholder,
            )
            .map(|query| (query.canonical_sql, query.database_template))
            .map_err(analysis_error)
    }

    /// Validate a relation-only rewrite and return the canonical relation of each referenced target.
    fn rewrite_relations(
        &self,
        sql: &str,
        references: Vec<Reference>,
        resolver: &Bound<'_, PyAny>,
    ) -> PyResult<HashMap<String, String>> {
        let references: Vec<ExpectedReference> = expected_references(references);
        validated_relation_rewrite(
            sql,
            &references,
            &|name| lookup(resolver, name),
            &mut self.state().relations,
            &self.dialect,
        )
        .map(|relations| relations.into_iter().collect())
        .map_err(analysis_error)
    }

    /// Generate `(canonical_sql, database_template)` for one non-model query.
    fn canonicalize_query(
        &self,
        py: Python<'_>,
        sql: &str,
        database_placeholder: &str,
    ) -> PyResult<(String, String)> {
        py.detach(|| {
            canonical_query_with_database_template(sql, &self.dialect, database_placeholder)
        })
        .map(|query| (query.canonical_sql, query.database_template))
        .map_err(analysis_error)
    }

    /// The column names one storage expression references.
    fn storage_column_names(&self, py: Python<'_>, expression: &str) -> PyResult<Vec<String>> {
        py.detach(|| storage_column_names(expression, &self.dialect))
            .map_err(analysis_error)
    }
}

type NativeRewrite = (String, String, Option<Vec<Option<String>>>, bool);
type NativeIdentity = (Option<String>, String);
type NativeCatalog = (
    String,
    Option<String>,
    Vec<NativeIdentity>,
    Option<NativeIdentity>,
    Option<NativeIdentity>,
    Option<String>,
    Option<String>,
    Vec<(String, String)>,
);

fn identity(relation: RelationIdentity) -> NativeIdentity {
    (relation.database, relation.name)
}

/// Rewrite relations, conjoin a predicate and prepend CTEs in one SELECT, returning
/// `(query, function_names, has_group_by)`.
#[pyfunction]
#[pyo3(signature = (sql, dialect_name, rewrites, predicate, ctes))]
fn sql_rewrite_query(
    py: Python<'_>,
    sql: &str,
    dialect_name: &str,
    rewrites: Vec<NativeRewrite>,
    predicate: Option<String>,
    ctes: Vec<(String, String)>,
) -> PyResult<(String, Vec<String>, bool)> {
    let rewrites: Vec<RelationRewrite> = rewrites
        .into_iter()
        .map(
            |(source_name, target_relation, source_databases, preserve_source_database)| {
                RelationRewrite {
                    source_name,
                    target_relation,
                    source_databases,
                    preserve_source_database,
                }
            },
        )
        .collect();
    let ctes: Vec<NamedQuery> = ctes
        .into_iter()
        .map(|(name, query)| NamedQuery { name, query })
        .collect();
    py.detach(|| {
        rewrite_query(
            sql,
            &SqlDialect::new(dialect_name),
            &rewrites,
            predicate.as_deref(),
            &ctes,
        )
    })
    .map(|rewritten: RewrittenQuery| {
        (
            rewritten.query,
            rewritten.function_names,
            rewritten.has_group_by,
        )
    })
    .map_err(analysis_error)
}

/// `(function_names, has_group_by)` of one SELECT.
#[pyfunction]
fn sql_select_aggregate_facts(sql: &str, dialect_name: &str) -> PyResult<(Vec<String>, bool)> {
    select_aggregate_facts(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

/// Canonical query and catalog facts of one statement, as a flat tuple.
#[pyfunction]
fn sql_analyze_catalog(py: Python<'_>, sql: &str, dialect_name: &str) -> PyResult<NativeCatalog> {
    let analysis: CatalogAnalysis = py
        .detach(|| analyze_catalog(sql, &SqlDialect::new(dialect_name)))
        .map_err(analysis_error)?;
    Ok((
        analysis.canonical_sql,
        analysis.query_sql,
        analysis
            .source_relations
            .into_iter()
            .map(identity)
            .collect(),
        analysis.direct_source.map(identity),
        analysis.target_relation.map(identity),
        analysis.engine,
        analysis.ttl,
        analysis.settings,
    ))
}

#[pyfunction]
fn sql_expression_list(sql: &str, dialect_name: &str) -> PyResult<Vec<String>> {
    expression_list(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
fn sql_validate_query(sql: &str, dialect_name: &str) -> PyResult<()> {
    validate_query(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
fn sql_validate_insert_query(sql: &str, dialect_name: &str) -> PyResult<()> {
    validate_insert_query(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
fn sql_canonicalize(sql: &str, dialect_name: &str) -> PyResult<String> {
    canonicalize_sql(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
fn sql_render_string_literal(value: &str, dialect_name: &str) -> PyResult<String> {
    render_string_literal(value, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("SqlTreeError", module.py().get_type::<SqlTreeError>())?;
    module.add_class::<SqlModelTree>()?;
    module.add_class::<SqlTreeAnalyzer>()?;
    module.add_function(wrap_pyfunction!(sql_rewrite_query, module)?)?;
    module.add_function(wrap_pyfunction!(sql_select_aggregate_facts, module)?)?;
    module.add_function(wrap_pyfunction!(sql_analyze_catalog, module)?)?;
    module.add_function(wrap_pyfunction!(sql_expression_list, module)?)?;
    module.add_function(wrap_pyfunction!(sql_validate_query, module)?)?;
    module.add_function(wrap_pyfunction!(sql_validate_insert_query, module)?)?;
    module.add_function(wrap_pyfunction!(sql_canonicalize, module)?)?;
    module.add_function(wrap_pyfunction!(sql_render_string_literal, module)?)?;
    Ok(())
}
