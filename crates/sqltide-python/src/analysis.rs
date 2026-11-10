//! Python bindings for `sqltide_sqlanalysis`. Parsed trees stay in Rust; Python receives compact
//! facts and generated SQL. A failure raises its SQL analysis exception over the native
//! `SqlTreeError(kind, *details)`, except where noted.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};

use pyo3::create_exception;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_sqlanalysis::{
    AnalysisError, CatalogAnalysis, ExpectedReference, ModelError, NamedQuery, ParsedModel,
    Projection, ProjectionTypeCache, RelationCache, RelationIdentity, RelationRewrite,
    RewrittenQuery, SqlDialect, analyze_catalog, analyze_model,
    canonical_query_with_database_template, canonicalize_sql, expression_list,
    quarantine_ineligibility, quarantine_split, render_string_literal, rewrite_query,
    select_row_level, storage_column_names, validate_query, validated_relation_rewrite,
};
use sqltide_sqltext::sql_scan::SourceSpan;

use crate::model_analysis::{analyze_model_sql, domain, storage_expressions};
use crate::sql_scan::{Span, raise as raise_scan, scan_failure, scan_references, span};

const MODELS: &str = "sqltide.compiler.sql_analysis.models";
const EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";

create_exception!(_native, SqlTreeError, PyValueError);

type NativeProjection = (usize, String, String, Span);

/// The native `SqlTreeError` for one analysis failure, raised as is.
fn raw_analysis_error(error: AnalysisError) -> PyErr {
    SqlTreeError::new_err(("analysis", error.0))
}

/// The SQL analysis exception for one analysis failure.
pub(crate) fn analysis_error(error: AnalysisError) -> PyErr {
    let native: PyErr = raw_analysis_error(error);
    Python::attach(|py| domain(py, native))
}

fn model_error(py: Python<'_>, error: ModelError) -> PyResult<PyErr> {
    let details: Bound<'_, PyTuple> = match error {
        ModelError::Analysis(error) => return Ok(raw_analysis_error(error)),
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

/// The expected reference of each analyzed `SqlReference`.
fn expected_references(references: &[Bound<'_, PyAny>]) -> PyResult<Vec<ExpectedReference>> {
    references
        .iter()
        .map(|reference| {
            let ref_type: Bound<'_, PyAny> = reference.getattr("ref_type")?;
            let location: Bound<'_, PyAny> = reference.getattr("span")?;
            Ok(ExpectedReference {
                relation_type: reference.getattr("relation_type")?.str()?.extract()?,
                name: reference.getattr("name")?.extract()?,
                ref_type: if ref_type.is_none() {
                    None
                } else {
                    Some(ref_type.str()?.extract()?)
                },
                start: location.getattr("start")?.extract()?,
                end: location.getattr("end")?.extract()?,
            })
        })
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
    pub(crate) fn function_names(&self) -> Vec<String> {
        self.model.function_names.clone()
    }

    #[getter]
    pub(crate) fn has_group_by(&self) -> bool {
        self.model.has_group_by
    }

    /// `(index, name, type, span)` for each typed outer projection.
    #[getter]
    pub(crate) fn projections(&self) -> Vec<NativeProjection> {
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
            Err(error) => Err(domain(py, model_error(py, error)?)),
        }
    }

    /// Resolve one analyzed model into `(canonical_sql, database_template)`.
    fn resolve_model(
        &self,
        model: &SqlModelTree,
        authored_sql: &str,
        references: Vec<Bound<'_, PyAny>>,
        resolver: &Bound<'_, PyAny>,
        database_placeholder: &str,
    ) -> PyResult<(String, String)> {
        let references: Vec<ExpectedReference> = expected_references(&references)?;
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

    /// `sql` with each logical reference replaced by its resolved target's canonical relation,
    /// preserving the authored surrounding text.
    fn rewrite_references(
        &self,
        py: Python<'_>,
        sql: &str,
        resolver: &Bound<'_, PyAny>,
    ) -> PyResult<String> {
        let references = scan_references(sql).map_err(|error| domain(py, error))?;
        for (name, _, _, location) in &references {
            if !resolver.contains(name)? {
                let keywords: Bound<'_, PyDict> = PyDict::new(py);
                keywords.set_item(
                    "span",
                    py.import(MODELS)?
                        .getattr("SqlSourceSpan")?
                        .call1(*location)?,
                )?;
                return Err(PyErr::from_value(
                    py.import(EXCEPTIONS)?
                        .getattr("SqlAnalysisError")?
                        .call((format!("Unresolved ref: {name}"),), Some(&keywords))?,
                ));
            }
        }
        let expected: Vec<ExpectedReference> = references
            .iter()
            .map(
                |(name, relation_type, ref_type, location)| ExpectedReference {
                    relation_type: (*relation_type).to_owned(),
                    name: name.clone(),
                    ref_type: ref_type.map(str::to_owned),
                    start: location.0,
                    end: location.1,
                },
            )
            .collect();
        let canonical: HashMap<String, String> = validated_relation_rewrite(
            sql,
            &expected,
            &|name| lookup(resolver, name),
            &mut self.state().relations,
            &self.dialect,
        )
        .map(|relations| relations.into_iter().collect())
        .map_err(analysis_error)?;
        let mut rewritten: Vec<char> = sql.chars().collect();
        for (name, _, _, location) in references.iter().rev() {
            let target: String = resolver.get_item(name)?.extract()?;
            let relation: &String = canonical
                .get(&target)
                .ok_or_else(|| PyKeyError::new_err(target.clone()))?;
            let _ = rewritten.splice(location.0..location.1, relation.chars());
        }
        sqltide_sqltext::sql_scan::normalized_statement_sql(&rewritten).map_err(scan_failure)
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

fn identity<'py>(py: Python<'py>, relation: RelationIdentity) -> PyResult<Bound<'py, PyAny>> {
    py.import(MODELS)?
        .getattr("SqlRelationIdentity")?
        .call1((relation.database, relation.name))
}

/// The `SqlQueryRewriteResult` of one rewritten SELECT and its aggregate facts.
pub(crate) fn rewrite_result<'py>(
    py: Python<'py>,
    query: String,
    function_names: &[String],
    has_group_by: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("query", query)?;
    keywords.set_item(
        "has_aggregate_semantics",
        has_group_by || !function_names.is_empty(),
    )?;
    py.import(MODELS)?
        .getattr("SqlQueryRewriteResult")?
        .call((), Some(&keywords))
}

/// Rewrite relations, conjoin a predicate and prepend CTEs in one parsed SELECT.
#[pyfunction]
#[pyo3(
    name = "rewrite_query",
    signature = (*, sql, dialect, relation_rewrites = Vec::new(), predicate = None, prepend_ctes = Vec::new())
)]
fn rewrite_select_query<'py>(
    py: Python<'py>,
    sql: &str,
    dialect: &str,
    relation_rewrites: Vec<Bound<'py, PyAny>>,
    predicate: Option<String>,
    prepend_ctes: Vec<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let rewrites: Vec<RelationRewrite> = relation_rewrites
        .iter()
        .map(|rewrite| {
            Ok(RelationRewrite {
                source_name: rewrite.getattr("source_name")?.extract()?,
                target_relation: rewrite.getattr("target_relation")?.extract()?,
                source_databases: rewrite.getattr("source_databases")?.extract()?,
                preserve_source_database: rewrite.getattr("preserve_source_database")?.extract()?,
            })
        })
        .collect::<PyResult<_>>()?;
    let ctes: Vec<NamedQuery> = prepend_ctes
        .iter()
        .map(|named_query| {
            Ok(NamedQuery {
                name: named_query.getattr("name")?.extract()?,
                query: named_query.getattr("query")?.extract()?,
            })
        })
        .collect::<PyResult<_>>()?;
    let rewritten: RewrittenQuery = py
        .detach(|| {
            rewrite_query(
                sql,
                &SqlDialect::new(dialect),
                &rewrites,
                predicate.as_deref(),
                &ctes,
            )
        })
        .map_err(analysis_error)?;
    rewrite_result(
        py,
        rewritten.query,
        &rewritten.function_names,
        rewritten.has_group_by,
    )
}

/// `(column, reason)` for the first column preventing a quarantine split, else `None`.
#[pyfunction]
fn sql_quarantine_ineligibility(
    sql: &str,
    dialect_name: &str,
) -> PyResult<Option<(String, String)>> {
    quarantine_ineligibility(sql, &SqlDialect::new(dialect_name))
        .map(|blocked| blocked.map(|item| (item.column, item.reason)))
        .map_err(raw_analysis_error)
}

/// `(kept-rows query, dead-letter query)` for one quarantine model.
#[pyfunction]
fn sql_quarantine_split(
    sql: &str,
    model_name: &str,
    dialect_name: &str,
) -> PyResult<(String, String)> {
    quarantine_split(sql, model_name, &SqlDialect::new(dialect_name))
        .map(|split| (split.good_query, split.dead_letter_query))
        .map_err(raw_analysis_error)
}

#[pyfunction]
fn sql_select_is_row_level(sql: &str, dialect_name: &str) -> PyResult<bool> {
    select_row_level(sql, &SqlDialect::new(dialect_name)).map_err(raw_analysis_error)
}

/// The `SqlCatalogAnalysis` of one query or catalog statement.
#[pyfunction]
#[pyo3(signature = (*, sql, dialect))]
fn analyze_catalog_sql<'py>(
    py: Python<'py>,
    sql: &str,
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let analysis: CatalogAnalysis = py
        .detach(|| analyze_catalog(sql, &SqlDialect::new(dialect)))
        .map_err(analysis_error)?;
    let source_relations: Vec<Bound<'py, PyAny>> = analysis
        .source_relations
        .into_iter()
        .map(|relation| identity(py, relation))
        .collect::<PyResult<_>>()?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("canonical_sql", analysis.canonical_sql)?;
    keywords.set_item("query_sql", analysis.query_sql)?;
    keywords.set_item("source_relations", PyTuple::new(py, source_relations)?)?;
    keywords.set_item(
        "direct_source",
        analysis
            .direct_source
            .map(|relation| identity(py, relation))
            .transpose()?,
    )?;
    keywords.set_item(
        "target_relation",
        analysis
            .target_relation
            .map(|relation| identity(py, relation))
            .transpose()?,
    )?;
    keywords.set_item("engine", analysis.engine)?;
    keywords.set_item("ttl", analysis.ttl)?;
    let settings: Vec<Bound<'py, PyTuple>> = analysis
        .settings
        .into_iter()
        .map(|setting| PyTuple::new(py, [setting.0, setting.1]))
        .collect::<PyResult<_>>()?;
    keywords.set_item("settings", PyTuple::new(py, settings)?)?;
    py.import(MODELS)?
        .getattr("SqlCatalogAnalysis")?
        .call((), Some(&keywords))
}

/// The top-level expressions of one comma-separated ClickHouse expression list.
#[pyfunction]
#[pyo3(signature = (*, sql, dialect))]
fn parse_expression_list<'py>(
    py: Python<'py>,
    sql: &str,
    dialect: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let expressions: Vec<String> =
        expression_list(sql, &SqlDialect::new(dialect)).map_err(analysis_error)?;
    PyTuple::new(py, expressions)
}

#[pyfunction]
pub(crate) fn sql_validate_query(sql: &str, dialect_name: &str) -> PyResult<()> {
    validate_query(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
pub(crate) fn sql_canonicalize(sql: &str, dialect_name: &str) -> PyResult<String> {
    canonicalize_sql(sql, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

#[pyfunction]
pub(crate) fn sql_render_string_literal(value: &str, dialect_name: &str) -> PyResult<String> {
    render_string_literal(value, &SqlDialect::new(dialect_name)).map_err(analysis_error)
}

fn database_placeholder(py: Python<'_>) -> PyResult<String> {
    py.import(ADAPTER_CONSTANTS)?
        .getattr("ADAPTER_DATABASE_PLACEHOLDER")?
        .extract()
}

fn resolved_query<'py>(
    py: Python<'py>,
    (canonical_sql, database_template): (String, String),
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("canonical_sql", canonical_sql)?;
    keywords.set_item("database_template", database_template)?;
    py.import(MODELS)?
        .getattr("SqlResolvedQuery")?
        .call((), Some(&keywords))
}

/// Private parsed model state for one complete compiler invocation.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct SqlModelAnalyzer {
    dialect: String,
    analyzer: Py<SqlTreeAnalyzer>,
    trees: Py<PyDict>,
}

impl SqlModelAnalyzer {
    fn tree<'py>(
        &self,
        py: Python<'py>,
        analysis: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, SqlModelTree>> {
        match self.trees.bind(py).get_item(analysis)? {
            Some(tree) => Ok(tree.cast_into::<SqlModelTree>()?),
            None => Err(PyErr::from_value(
                py.import(EXCEPTIONS)?
                    .getattr("SqlAnalysisError")?
                    .call1(("Model analysis does not belong to this compiler invocation",))?,
            )),
        }
    }

    fn authored_sql(analysis: &Bound<'_, PyAny>) -> PyResult<String> {
        analysis.getattr("authored_sql")?.extract()
    }
}

#[pymethods]
impl SqlModelAnalyzer {
    #[new]
    #[pyo3(signature = (*, dialect))]
    fn new(py: Python<'_>, dialect: &str) -> PyResult<Self> {
        Ok(Self {
            dialect: dialect.to_owned(),
            analyzer: Py::new(py, SqlTreeAnalyzer::new(dialect))?,
            trees: PyDict::new(py).unbind(),
        })
    }

    /// Analyze one authored model and retain only private rewrite state.
    #[pyo3(signature = (*, sql, engine, order_by, partition_by, ttl))]
    fn analyze<'py>(
        &self,
        py: Python<'py>,
        sql: &Bound<'py, PyAny>,
        engine: &str,
        order_by: &Bound<'py, PyAny>,
        partition_by: &Bound<'py, PyAny>,
        ttl: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (analysis, tree) = analyze_model_sql(
            py,
            sql,
            engine,
            order_by,
            partition_by,
            ttl,
            self.analyzer.bind(py).as_any(),
        )?;
        self.trees.bind(py).set_item(&analysis, tree)?;
        Ok(analysis)
    }

    /// Resolve and qualify a previously analyzed model without reparsing it.
    #[pyo3(signature = (*, analysis, resolver))]
    fn resolve<'py>(
        &self,
        py: Python<'py>,
        analysis: &Bound<'py, PyAny>,
        resolver: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let tree: Bound<'py, SqlModelTree> = self.tree(py, analysis)?;
        let references: Vec<Bound<'py, PyAny>> = analysis
            .getattr("references")?
            .try_iter()?
            .collect::<PyResult<_>>()?;
        let resolved: (String, String) = self.analyzer.get().resolve_model(
            tree.get(),
            &Self::authored_sql(analysis)?,
            references,
            resolver,
            &database_placeholder(py)?,
        )?;
        resolved_query(py, resolved)
    }

    /// Whether an analyzed query contains a PREWHERE clause.
    #[pyo3(signature = (*, analysis))]
    fn uses_prewhere(&self, py: Python<'_>, analysis: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(self.tree(py, analysis)?.get().uses_prewhere())
    }

    /// Canonical and adapter-template forms of a generated query.
    #[pyo3(signature = (*, sql))]
    fn canonicalize_query<'py>(&self, py: Python<'py>, sql: &str) -> PyResult<Bound<'py, PyAny>> {
        let canonical: (String, String) =
            self.analyzer
                .get()
                .canonicalize_query(py, sql, &database_placeholder(py)?)?;
        resolved_query(py, canonical)
    }

    /// Standalone storage clauses analyzed through this invocation's dialect.
    #[pyo3(signature = (*, order_by, partition_by, ttl, output_columns))]
    fn analyze_storage<'py>(
        &self,
        py: Python<'py>,
        order_by: &Bound<'py, PyAny>,
        partition_by: &Bound<'py, PyAny>,
        ttl: &Bound<'py, PyAny>,
        output_columns: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        storage_expressions(
            py,
            self.analyzer.bind(py).as_any(),
            order_by,
            partition_by,
            ttl,
            output_columns,
        )
    }

    /// Whether the outer SELECT produces one row per input row.
    #[pyo3(signature = (*, analysis))]
    fn is_row_level(&self, analysis: &Bound<'_, PyAny>) -> PyResult<bool> {
        sql_select_is_row_level(&Self::authored_sql(analysis)?, &self.dialect)
    }

    /// The first `(column, reason)` preventing a quarantine split.
    #[pyo3(signature = (*, analysis))]
    fn quarantine_ineligibility(
        &self,
        analysis: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(String, String)>> {
        sql_quarantine_ineligibility(&Self::authored_sql(analysis)?, &self.dialect)
    }

    /// The kept-rows and dead-letter queries generated from one resolved query.
    #[pyo3(signature = (*, sql, model_name))]
    fn quarantine_split(&self, sql: &str, model_name: &str) -> PyResult<(String, String)> {
        sql_quarantine_split(sql, model_name, &self.dialect)
    }
}

/// Logical relation rewriting with one private relation cache per invocation.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct SqlReferenceRewriter {
    analyzer: SqlTreeAnalyzer,
}

#[pymethods]
impl SqlReferenceRewriter {
    #[new]
    #[pyo3(signature = (*, dialect))]
    fn new(dialect: &str) -> Self {
        Self {
            analyzer: SqlTreeAnalyzer::new(dialect),
        }
    }

    /// Replace logical relation calls while preserving authored surrounding text.
    #[pyo3(signature = (*, sql, resolver))]
    fn rewrite(&self, py: Python<'_>, sql: &str, resolver: &Bound<'_, PyAny>) -> PyResult<String> {
        self.analyzer.rewrite_references(py, sql, resolver)
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("SqlTreeError", module.py().get_type::<SqlTreeError>())?;
    module.add_class::<SqlModelTree>()?;
    module.add_class::<SqlTreeAnalyzer>()?;
    module.add_class::<SqlModelAnalyzer>()?;
    module.add_class::<SqlReferenceRewriter>()?;
    module.add_function(wrap_pyfunction!(rewrite_select_query, module)?)?;
    module.add_function(wrap_pyfunction!(sql_select_is_row_level, module)?)?;
    module.add_function(wrap_pyfunction!(sql_quarantine_ineligibility, module)?)?;
    module.add_function(wrap_pyfunction!(sql_quarantine_split, module)?)?;
    module.add_function(wrap_pyfunction!(analyze_catalog_sql, module)?)?;
    module.add_function(wrap_pyfunction!(parse_expression_list, module)?)?;
    module.add_function(wrap_pyfunction!(sql_validate_query, module)?)?;
    module.add_function(wrap_pyfunction!(sql_canonicalize, module)?)?;
    module.add_function(wrap_pyfunction!(sql_render_string_literal, module)?)?;
    Ok(())
}
