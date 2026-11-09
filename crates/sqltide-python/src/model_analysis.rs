//! Python bindings that assemble one strict model SQL analysis and its storage expressions, and
//! raise native scan and tree failures as SQL analysis exceptions.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::analysis::{SqlModelTree, SqlTreeError};
use crate::sql_scan::{SqlScanError, extract_references};

const MODELS: &str = "sqltide.compiler.sql_analysis.models";
const TYPES: &str = "sqltide.compiler.sql_analysis.types";
const EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const MISSING_WITH_KIND: &str = "missing_with";
const MISSING_WITH_SUFFIX: &str = " must declare CTEs in a WITH clause";
const AGGREGATING_ENGINES: [&str; 2] = ["aggregatingmergetree", "summingmergetree"];

fn call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(callable.py());
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

fn span<'py>(py: Python<'py>, native: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    py.import(MODELS)?
        .getattr("SqlSourceSpan")?
        .call1(native.cast::<PyTuple>()?.clone())
}

/// The SQL analysis exception for one native scan or tree failure, raised `from None` while
/// handling it; any other failure is returned unchanged.
pub(crate) fn sql_analysis_error(py: Python<'_>, failure: PyErr) -> PyResult<PyErr> {
    let native: Bound<'_, PyAny> = failure.value(py).clone().into_any();
    let exceptions: Bound<'_, PyModule> = py.import(EXCEPTIONS)?;
    let class = |name: &str| exceptions.getattr(name);
    let args: Bound<'_, PyTuple> = native.getattr("args")?.cast_into::<PyTuple>()?;
    let error: Bound<'_, PyAny> = if failure.is_instance_of::<SqlScanError>(py) {
        let kind: String = args.get_item(0)?.extract()?;
        let message: String = args.get_item(1)?.extract()?;
        let native_span: Bound<'_, PyAny> = args.get_item(2)?;
        let location: Bound<'_, PyAny> = if native_span.is_none() {
            py.None().into_bound(py)
        } else {
            span(py, &native_span)?
        };
        if kind == MISSING_WITH_KIND && !location.is_none() {
            let context: &str = message
                .strip_suffix(MISSING_WITH_SUFFIX)
                .unwrap_or(&message);
            call(
                &class("SqlMissingWithClauseError")?,
                &[
                    ("context", context.into_pyobject(py)?.into_any()),
                    ("span", location),
                ],
            )?
        } else {
            let keywords: Bound<'_, PyDict> = PyDict::new(py);
            keywords.set_item("span", location)?;
            class("SqlAnalysisError")?.call((message,), Some(&keywords))?
        }
    } else if failure.is_instance_of::<SqlTreeError>(py) {
        let kind: String = args.get_item(0)?.extract()?;
        let detail = |index: usize| args.get_item(index);
        match kind.as_str() {
            "statement_count" => class("SqlStatementCountError")?.call1((detail(1)?,))?,
            "query_shape" => call(
                &class("SqlQueryShapeError")?,
                &[
                    ("statement_type", detail(1)?),
                    ("statement_sql", detail(2)?),
                    ("is_set_operation", detail(3)?),
                ],
            )?,
            "untyped_projection" => call(
                &class("SqlUntypedProjectionError")?,
                &[
                    ("column_index", detail(1)?),
                    ("projection_sql", detail(2)?),
                    ("span", span(py, &detail(3)?)?),
                ],
            )?,
            "star_projection" => call(
                &class("SqlStarProjectionError")?,
                &[
                    ("column_index", detail(1)?),
                    ("span", span(py, &detail(2)?)?),
                ],
            )?,
            "duplicate_alias" => call(
                &class("SqlDuplicateAliasError")?,
                &[("alias", detail(1)?), ("span", span(py, &detail(2)?)?)],
            )?,
            _ => class("SqlAnalysisError")?.call1((detail(1)?,))?,
        }
    } else {
        return Ok(failure);
    };
    error.setattr("__context__", &native)?;
    error.setattr("__cause__", py.None())?;
    Ok(PyErr::from_value(error))
}

/// Each storage expression with the output columns it references; an unknown column raises
/// `SqlStorageReferenceError`.
fn storage_expressions<'py>(
    py: Python<'py>,
    analyzer: &Bound<'py, PyAny>,
    order_by: &Bound<'py, PyAny>,
    partition_by: &Bound<'py, PyAny>,
    ttl: &Bound<'py, PyAny>,
    output_columns: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let kinds: Bound<'py, PyAny> = py.import(TYPES)?.getattr("SqlStorageExpressionKind")?;
    let mut expressions: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = Vec::new();
    for expression in order_by.try_iter()? {
        expressions.push((kinds.getattr("ORDER_BY")?, expression?));
    }
    if !partition_by.is_none() {
        expressions.push((kinds.getattr("PARTITION_BY")?, partition_by.clone()));
    }
    if !ttl.is_none() {
        expressions.push((kinds.getattr("TTL")?, ttl.clone()));
    }
    let mut available_names: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut available: HashSet<String> = HashSet::new();
    for column in output_columns.try_iter()? {
        let name: Bound<'py, PyAny> = column?.getattr("name")?;
        available.insert(name.extract()?);
        available_names.push(name);
    }
    let available_names: Bound<'py, PyTuple> = PyTuple::new(py, available_names)?;
    let expression_class: Bound<'py, PyAny> = py.import(MODELS)?.getattr("SqlStorageExpression")?;
    let mut analyzed: Vec<Bound<'py, PyAny>> = Vec::new();
    for (kind, expression) in expressions {
        let names: Vec<String> = match analyzer
            .call_method1("storage_column_names", (&expression,))
            .and_then(|names| names.extract())
        {
            Ok(names) => names,
            Err(failure) => return Err(sql_analysis_error(py, failure)?),
        };
        let unknown: Vec<&String> = names
            .iter()
            .filter(|name| !available.contains(*name))
            .collect();
        if !unknown.is_empty() {
            let error: Bound<'py, PyAny> = call(
                &py.import(EXCEPTIONS)?.getattr("SqlStorageReferenceError")?,
                &[
                    ("kind", kind),
                    ("expression", expression),
                    (
                        "unknown_column_names",
                        PyTuple::new(py, unknown)?.into_any(),
                    ),
                    ("available_column_names", available_names.clone().into_any()),
                ],
            )?;
            return Err(PyErr::from_value(error));
        }
        analyzed.push(call(
            &expression_class,
            &[
                ("kind", kind),
                ("sql", expression),
                (
                    "referenced_column_names",
                    PyTuple::new(py, names)?.into_any(),
                ),
            ],
        )?);
    }
    Ok(PyTuple::new(py, analyzed)?.into_any())
}

/// Parse every authored storage expression once and validate its references.
#[pyfunction]
#[pyo3(signature = (*, order_by, partition_by, ttl, output_columns, analyzer))]
fn analyze_storage_expressions<'py>(
    py: Python<'py>,
    order_by: &Bound<'py, PyAny>,
    partition_by: &Bound<'py, PyAny>,
    ttl: &Bound<'py, PyAny>,
    output_columns: &Bound<'py, PyAny>,
    analyzer: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    storage_expressions(py, analyzer, order_by, partition_by, ttl, output_columns)
}

/// `(analysis, tree)` with every compiler-critical fact of one model.
#[pyfunction]
#[pyo3(signature = (*, sql, engine, order_by, partition_by, ttl, analyzer))]
fn analyze_model_sql<'py>(
    py: Python<'py>,
    sql: &Bound<'py, PyAny>,
    engine: &str,
    order_by: &Bound<'py, PyAny>,
    partition_by: &Bound<'py, PyAny>,
    ttl: &Bound<'py, PyAny>,
    analyzer: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let tree: Bound<'py, PyAny> = match analyzer.call_method1("analyze_model", (sql,)) {
        Ok(tree) => tree,
        Err(failure) => return Err(sql_analysis_error(py, failure)?),
    };
    let models: Bound<'py, PyModule> = py.import(MODELS)?;
    let output_class: Bound<'py, PyAny> = models.getattr("SqlOutputColumn")?;
    let projection_class: Bound<'py, PyAny> = models.getattr("SqlProjection")?;
    let mut projections: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut outputs: Vec<Bound<'py, PyAny>> = Vec::new();
    let native: PyRef<'py, SqlModelTree> = tree.cast::<SqlModelTree>()?.borrow();
    for (index, name, type_sql, location) in native.projections() {
        let output: Bound<'py, PyAny> = call(
            &output_class,
            &[
                ("name", name.into_pyobject(py)?.into_any()),
                ("type", type_sql.into_pyobject(py)?.into_any()),
            ],
        )?;
        projections.push(call(
            &projection_class,
            &[
                ("index", index.into_pyobject(py)?.into_any()),
                ("output", output.clone()),
                ("span", span(py, &location.into_pyobject(py)?.into_any())?),
            ],
        )?);
        outputs.push(output);
    }
    let references: Bound<'py, PyAny> = references(py, &sql.extract::<String>()?)?;
    let storage: Bound<'py, PyAny> = storage_expressions(
        py,
        analyzer,
        order_by,
        partition_by,
        ttl,
        &PyTuple::new(py, outputs)?.into_any(),
    )?;
    let aggregate_facts: Bound<'py, PyAny> =
        aggregate_facts(py, native.function_names(), native.has_group_by(), engine)?;
    drop(native);
    let analysis: Bound<'py, PyAny> = call(
        &models.getattr("SqlModelAnalysis")?,
        &[
            ("authored_sql", sql.clone()),
            (
                "shape",
                py.import(TYPES)?
                    .getattr("SqlQueryShape")?
                    .getattr("SELECT")?,
            ),
            ("projections", PyTuple::new(py, projections)?.into_any()),
            ("references", references),
            ("storage_expressions", storage),
            ("aggregate_facts", aggregate_facts),
        ],
    )?;
    Ok((analysis, tree))
}

/// Query and engine aggregation facts without generic aliases.
fn aggregate_facts<'py>(
    py: Python<'py>,
    names: Vec<String>,
    has_group_by: bool,
    engine: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let models: Bound<'py, PyModule> = py.import(MODELS)?;
    let engine_name: &str = engine.split('(').next().unwrap_or_default().trim();
    let lowered: String = engine_name.to_lowercase();
    let normalized: &str = lowered.strip_prefix("replicated").unwrap_or(&lowered);
    let mut function_names: Vec<String> = Vec::new();
    for name in names {
        if !function_names.contains(&name) {
            function_names.push(name);
        }
    }
    call(
        &models.getattr("SqlAggregateFacts")?,
        &[
            (
                "has_group_by",
                pyo3::types::PyBool::new(py, has_group_by)
                    .to_owned()
                    .into_any(),
            ),
            (
                "function_names",
                PyTuple::new(py, function_names)?.into_any(),
            ),
            ("engine_name", engine_name.into_pyobject(py)?.into_any()),
            (
                "engine_has_aggregate_semantics",
                pyo3::types::PyBool::new(py, AGGREGATING_ENGINES.contains(&normalized))
                    .to_owned()
                    .into_any(),
            ),
        ],
    )
}

/// Query and engine aggregation facts from a parsed tree's aggregate function names.
#[pyfunction]
#[pyo3(signature = (*, function_names, has_group_by, engine))]
fn build_aggregate_facts<'py>(
    py: Python<'py>,
    function_names: Vec<String>,
    has_group_by: bool,
    engine: &str,
) -> PyResult<Bound<'py, PyAny>> {
    aggregate_facts(py, function_names, has_group_by, engine)
}

/// Logical relation references found outside comments and quoted text.
fn references<'py>(py: Python<'py>, sql: &str) -> PyResult<Bound<'py, PyAny>> {
    let scanned = match extract_references(sql) {
        Ok(scanned) => scanned,
        Err(failure) => return Err(sql_analysis_error(py, failure)?),
    };
    let reference_class: Bound<'py, PyAny> = py.import(MODELS)?.getattr("SqlReference")?;
    let ref_types: Bound<'py, PyAny> = py.import(TYPES)?.getattr("RefType")?;
    let mut references: Vec<Bound<'py, PyAny>> = Vec::new();
    for (name, relation_type, ref_type, location) in scanned {
        let ref_type: Bound<'py, PyAny> = match ref_type {
            Some(ref_type) => ref_types.call1((ref_type,))?,
            None => py.None().into_bound(py),
        };
        references.push(call(
            &reference_class,
            &[
                ("name", name.into_pyobject(py)?.into_any()),
                ("relation_type", relation_type.into_pyobject(py)?.into_any()),
                ("ref_type", ref_type),
                ("span", span(py, &location.into_pyobject(py)?.into_any())?),
            ],
        )?);
    }
    Ok(PyTuple::new(py, references)?.into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(analyze_model_sql, module)?)?;
    module.add_function(wrap_pyfunction!(build_aggregate_facts, module)?)?;
    module.add_function(wrap_pyfunction!(analyze_storage_expressions, module)?)
}
