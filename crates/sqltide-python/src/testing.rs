//! Assembly of discovered SQL tests into executable comparison statements against the compiled
//! graph, reference replacement, and the quality identities of compiled SQL tests and audits.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use sqltide_compile::sql_test_chain::{ChainError, resolve_chain};

use crate::analysis::sql_canonicalize;
use crate::compile::{
    quality_audit_identity, quality_test_identity, sql_test_chain_step_queries,
    sql_test_wrap_with_ctes,
};
use crate::model_analysis::extract_references;
use crate::model_sql::{call, chained, object};
use crate::polyglot::sql_infer_projection_names;

const TESTING_MODELS: &str = "sqltide.compiler.testing.models";
const TESTING_EXCEPTIONS: &str = "sqltide.compiler.testing.exceptions";
const TEST_DISCOVERY_MODELS: &str = "sqltide.compiler.test_discovery.models";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const QUALITY_MODELS: &str = "sqltide.compiler.quality.models";
const QUALITY_TYPES: &str = "sqltide.compiler.quality.types";
const QUALITY_EXCEPTIONS: &str = "sqltide.compiler.quality.exceptions";
const ASSEMBLED_MODEL_PREFIX: &str = "__model__";
const EXPECTED_PREFIX: &str = "__expected__";
const ASSERT_PREFIX: &str = "__assert__";
const REF_PREFIX: &str = "__ref__";
const SOURCE_PREFIX: &str = "__source__";
const ASSERTION_TARGET_PREFIX: &str = "assert ";
const MACRO_TARGET_PREFIX: &str = "macro ";
const EXPECTED_COLUMNS_LABEL: &str = "__expected__ ";
const ASSERTION_COLUMNS_LABEL: &str = "__assert__ ";
const MACRO_ACTUAL_LABEL: &str = "__macro_actual__";
const MACRO_EXPECTED_LABEL: &str = "__macro_expected__";
const MACRO_MODE: &str = "macro";

type Ctes = Vec<(String, String)>;

fn is_instance(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    error
        .value(py)
        .is_instance(&py.import(module)?.getattr(name)?)
}

/// `SqlTestAssemblyError(message)`.
fn assembly_error(py: Python<'_>, message: String) -> PyErr {
    py.import(TESTING_EXCEPTIONS)
        .and_then(|module| module.getattr("SqlTestAssemblyError"))
        .and_then(|class| class.call1((message,)))
        .map_or_else(|error| error, PyErr::from_value)
}

/// `SqlTestAssemblyError(message)` raised while handling `handled`, `from None` or `from handled`.
fn assembly_error_from(py: Python<'_>, message: String, handled: &PyErr, caused: bool) -> PyErr {
    let error: PyErr = assembly_error(py, message);
    let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
    let handled: Bound<'_, PyAny> = handled.value(py).clone().into_any();
    match chained(&value, &handled, caused.then_some(&handled)) {
        Ok(()) => error,
        Err(failure) => failure,
    }
}

fn strip_prefix<'a>(text: &'a str, prefix: &str) -> &'a str {
    text.strip_prefix(prefix).unwrap_or(text)
}

fn ctes_tuple<'py>(py: Python<'py>, ctes: &[(String, String)]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, ctes.iter().map(|(name, query)| (name, query)))
}

/// Replace logical ref markers in `sql` with the resolved relation names, raising
/// `PipelineCompileError` for SQL the rewriter cannot analyze.
#[pyfunction]
#[pyo3(signature = (*, sql, resolver, rewriter))]
pub(crate) fn replace_refs<'py>(
    py: Python<'py>,
    sql: &Bound<'py, PyAny>,
    resolver: &Bound<'py, PyAny>,
    rewriter: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("sql", sql)?;
    keywords.set_item("resolver", resolver)?;
    match rewriter.call_method("rewrite", (), Some(&keywords)) {
        Ok(rewritten) => Ok(rewritten),
        Err(error) if is_instance(py, &error, SQL_ANALYSIS_EXCEPTIONS, "SqlAnalysisError")? => {
            let failure: Bound<'py, PyAny> = error.value(py).clone().into_any();
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("span", failure.getattr("span")?)?;
            let raised: Bound<'py, PyAny> = py
                .import(COMPILE_EXCEPTIONS)?
                .getattr("PipelineCompileError")?
                .call((failure.str()?,), Some(&keywords))?;
            chained(&raised, &failure, None)?;
            Err(PyErr::from_value(raised))
        }
        Err(error) => Err(error),
    }
}

fn quality_identity<'py>(
    py: Python<'py>,
    kind: &str,
    node_name: &str,
    (binding_key, definition, execution): (String, String, String),
) -> PyResult<Bound<'py, PyAny>> {
    call(
        &py.import(QUALITY_MODELS)?.getattr("QualityNodeIdentity")?,
        &[
            (
                "node_kind",
                py.import(QUALITY_TYPES)?
                    .getattr("QualityNodeKind")?
                    .getattr(kind)?,
            ),
            ("node_name", object(py, node_name)?),
            ("binding_key", object(py, binding_key)?),
            ("definition_fingerprint", object(py, definition)?),
            ("execution_fingerprint", object(py, execution)?),
        ],
    )
}

/// The node name of a test or audit: its authored name, or its file stem.
fn node_name(node: &Bound<'_, PyAny>) -> PyResult<String> {
    let name: Bound<'_, PyAny> = node.getattr("name")?;
    if name.is_truthy()? {
        return name.extract();
    }
    node.getattr("file_path")?.getattr("stem")?.extract()
}

fn names_of(items: &Bound<'_, PyAny>, attribute: &str) -> PyResult<Vec<String>> {
    items
        .try_iter()?
        .map(|item| item?.getattr(attribute)?.extract())
        .collect()
}

/// Authored-definition and assembled-execution identity for one SQL test.
#[pyfunction]
#[pyo3(signature = (*, loaded_test, test_case, dialect))]
pub(crate) fn build_test_quality_identity<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
    test_case: &Bound<'py, PyAny>,
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let node_name: String = node_name(loaded_test)?;
    let mode: String = loaded_test.getattr("mode")?.str()?.extract()?;
    let assertion_names: Vec<String> = names_of(&test_case.getattr("assertion_cases")?, "name")?;
    let target_names: Vec<String> =
        names_of(&test_case.getattr("target_cases")?, "target_model_name")?;
    let mut authored: Ctes = Vec::new();
    for cte in loaded_test.getattr("authored_ctes")?.try_iter()? {
        let cte: Bound<'py, PyAny> = cte?;
        authored.push((
            cte.getattr("name")?.extract()?,
            sql_canonicalize(&cte.getattr("query")?.extract::<String>()?, dialect)?,
        ));
    }
    let execution: String =
        sql_canonicalize(&test_case.getattr("query")?.extract::<String>()?, dialect)?;
    let fingerprints = quality_test_identity(
        &node_name,
        &mode,
        assertion_names,
        target_names,
        authored,
        &execution,
    );
    quality_identity(py, "TEST", &node_name, fingerprints)
}

/// Target-neutral and target-resolved identity for one compiled SQL audit.
#[pyfunction]
#[pyo3(signature = (*, audit, resolved_query, dialect))]
pub(crate) fn build_audit_quality_identity<'py>(
    py: Python<'py>,
    audit: &Bound<'py, PyAny>,
    resolved_query: &str,
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let node_name: String = node_name(audit)?;
    let attached_column: Option<String> = audit.getattr("attached_column")?.extract()?;
    let attached_model: Option<String> = audit.getattr("attached_model")?.extract()?;
    let attachment_kind: String = audit.getattr("attachment_kind")?.str()?.extract()?;
    let referenced: Vec<String> = audit.getattr("referenced_model_names")?.extract()?;
    let severity: String = audit.getattr("severity")?.extract()?;
    let definition: String =
        sql_canonicalize(&audit.getattr("query")?.extract::<String>()?, dialect)?;
    let execution: String = sql_canonicalize(resolved_query, dialect)?;
    let fingerprints = quality_audit_identity(
        &node_name,
        attached_column.as_deref(),
        attached_model.as_deref(),
        &attachment_kind,
        referenced,
        &severity,
        &definition,
        &execution,
    );
    quality_identity(py, "AUDIT", &node_name, fingerprints)
}

/// A compiled identity, or `QualityIdentityError` before writing incomplete metadata.
#[pyfunction]
pub(crate) fn require_quality_identity<'py>(
    py: Python<'py>,
    identity: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !identity.is_none() {
        return Ok(identity);
    }
    Err(py
        .import(QUALITY_EXCEPTIONS)
        .and_then(|module| module.getattr("QualityIdentityError"))
        .and_then(|class| class.call1(("Quality node does not have a compiled identity",)))
        .map_or_else(|error| error, PyErr::from_value))
}

/// Every compiled model addressable by tests, by logical name, and its references.
struct Chain<'py> {
    registry: Bound<'py, PyDict>,
    refs_by_model: HashMap<String, Vec<String>>,
    source_names: HashSet<String>,
}

impl<'py> Chain<'py> {
    fn new(py: Python<'py>, compiled_pipelines: &Bound<'py, PyAny>) -> PyResult<Self> {
        let registry: Bound<'py, PyDict> = PyDict::new(py);
        let mut source_names: HashSet<String> = HashSet::new();
        for pipeline in compiled_pipelines.try_iter()? {
            let pipeline: Bound<'py, PyAny> = pipeline?;
            let source: Bound<'py, PyAny> = pipeline.getattr("source")?;
            if !source.is_none() {
                source_names.insert(source.getattr("key")?.getattr("name")?.extract()?);
            }
            for model in pipeline.getattr("models")?.try_iter()? {
                let model: Bound<'py, PyAny> = model?;
                registry.set_item(model.getattr("key")?.getattr("name")?, model)?;
            }
        }
        let mut refs_by_model: HashMap<String, Vec<String>> = HashMap::new();
        for (name, model) in registry.iter() {
            refs_by_model.insert(
                name.extract()?,
                names_of(&model.getattr("parsed_refs")?, "name")?,
            );
        }
        Ok(Self {
            registry,
            refs_by_model,
            source_names,
        })
    }
}

/// Resolves the logical relations of one model-mode test into mock CTEs or real compiled model
/// CTEs, assembled once each in dependency order.
struct Assembler<'a, 'py> {
    py: Python<'py>,
    path: String,
    payload: &'a Bound<'py, PyAny>,
    rewriter: &'a Bound<'py, PyAny>,
    chain: &'a Chain<'py>,
    mock_cte_by_name: HashMap<String, String>,
    assembled_cte_by_name: HashMap<String, String>,
    position_by_name: HashMap<String, usize>,
    assembled: Ctes,
    used_mocks: HashSet<String>,
}

impl<'a, 'py> Assembler<'a, 'py> {
    fn new(
        path: String,
        payload: &'a Bound<'py, PyAny>,
        rewriter: &'a Bound<'py, PyAny>,
        chain: &'a Chain<'py>,
    ) -> PyResult<Self> {
        let mut mock_cte_by_name: HashMap<String, String> = HashMap::new();
        for mock in payload.getattr("mocks")?.try_iter()? {
            let mock: Bound<'py, PyAny> = mock?;
            mock_cte_by_name.insert(
                mock.getattr("name")?.extract()?,
                mock.getattr("cte_name")?.extract()?,
            );
        }
        Ok(Self {
            py: payload.py(),
            path,
            payload,
            rewriter,
            chain,
            mock_cte_by_name,
            assembled_cte_by_name: HashMap::new(),
            position_by_name: HashMap::new(),
            assembled: Vec::new(),
            used_mocks: HashSet::new(),
        })
    }

    /// The CTE name that realizes one logical relation, assembling the models it needs.
    fn resolve(&mut self, logical_name: &str) -> PyResult<String> {
        let mocks: HashSet<String> = self.mock_cte_by_name.keys().cloned().collect();
        let assembled: HashSet<String> = self.assembled_cte_by_name.keys().cloned().collect();
        let resolution =
            match resolve_chain(logical_name, &self.chain.refs_by_model, &mocks, &assembled) {
                Ok(resolution) => resolution,
                Err(error) => {
                    let (reason, name, message) = match error {
                        ChainError::Cycle(name) => {
                            let message: String = format!(
                                "SQL test '{}' encountered a cyclic dependency while assembling \
                             '{name}'",
                                self.path
                            );
                            ("cycle", name, message)
                        }
                        ChainError::Unresolved(name) => {
                            let message: String = self.unresolved_message(&name)?;
                            ("unresolved", name, message)
                        }
                    };
                    let native: PyErr = PyValueError::new_err((reason, name));
                    return Err(assembly_error_from(self.py, message, &native, false));
                }
            };
        for mock in resolution.used_mocks {
            self.used_mocks.insert(self.mock_cte_by_name[&mock].clone());
        }
        for name in resolution.assembled {
            let entry: Bound<'py, PyAny> = self
                .chain
                .registry
                .get_item(&name)?
                .ok_or_else(|| PyKeyError::new_err(name.clone()))?;
            self.assemble(&name, &entry)?;
        }
        self.resolved_cte_name(logical_name)
    }

    fn resolved_cte_name(&self, logical_name: &str) -> PyResult<String> {
        self.mock_cte_by_name
            .get(logical_name)
            .or_else(|| self.assembled_cte_by_name.get(logical_name))
            .cloned()
            .ok_or_else(|| PyKeyError::new_err(logical_name.to_owned()))
    }

    fn rewrite(&self, sql: &Bound<'py, PyAny>, resolver: &Bound<'py, PyDict>) -> PyResult<String> {
        replace_refs(self.py, sql, resolver.as_any(), self.rewriter)?.extract()
    }

    fn assemble(&mut self, logical_name: &str, entry: &Bound<'py, PyAny>) -> PyResult<()> {
        let resolver: Bound<'py, PyDict> = PyDict::new(self.py);
        for name in names_of(&entry.getattr("parsed_refs")?, "name")? {
            let resolved: String = self.resolved_cte_name(&name)?;
            resolver.set_item(name, resolved)?;
        }
        let cte_name: String = format!("{ASSEMBLED_MODEL_PREFIX}{logical_name}");
        self.position_by_name
            .insert(logical_name.to_owned(), self.assembled.len());
        let query: String = self.rewrite(&entry.getattr("query")?, &resolver)?;
        self.assembled.push((cte_name.clone(), query));
        self.assembled_cte_by_name
            .insert(logical_name.to_owned(), cte_name);
        Ok(())
    }

    fn unresolved_message(&self, logical_name: &str) -> PyResult<String> {
        let prefix: &str = if self.chain.source_names.contains(logical_name) {
            SOURCE_PREFIX
        } else {
            REF_PREFIX
        };
        let targets: Vec<String> = names_of(&self.payload.getattr("expected_targets")?, "name")?
            .iter()
            .map(|name| strip_prefix(name, EXPECTED_PREFIX).to_owned())
            .collect();
        Ok(format!(
            "SQL test '{}' targets '{}', but dependency '{logical_name}' cannot be resolved. Add \
             `{prefix}{logical_name}` to mock it directly.",
            self.path,
            targets.join(", ")
        ))
    }

    fn unreachable_mock_warnings(&self) -> PyResult<Vec<String>> {
        let mut warnings: Vec<String> = Vec::new();
        for mock in self.payload.getattr("mocks")?.try_iter()? {
            let cte_name: String = mock?.getattr("cte_name")?.extract()?;
            if !self.used_mocks.contains(&cte_name) {
                warnings.push(format!(
                    "SQL test '{}' never reaches mock '{cte_name}'",
                    self.path
                ));
            }
        }
        Ok(warnings)
    }
}

/// The output column names of one authored test query, inferred with its CTEs in scope.
fn column_names<'py>(
    py: Python<'py>,
    query: &str,
    path: &str,
    label: &str,
    ctes: &[(String, String)],
    dialect: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let wrapped: String = sql_test_wrap_with_ctes(query, ctes.to_vec());
    match sql_infer_projection_names(py, &wrapped, dialect) {
        Ok(names) => Ok(names),
        Err(error) if is_instance(py, &error, SQL_ANALYSIS_EXCEPTIONS, "SqlAnalysisError")? => {
            let message: String = format!(
                "SQL test '{path}' cannot resolve {label} columns: {}",
                error.value(py).str()?
            );
            Err(assembly_error_from(py, message, &error, true))
        }
        Err(error) => Err(error),
    }
}

fn chain_step<'py>(
    py: Python<'py>,
    target: &str,
    columns: Bound<'py, PyTuple>,
    ctes: &[(String, String)],
    actual: String,
    expected: String,
) -> PyResult<Bound<'py, PyAny>> {
    call(
        &py.import(TESTING_MODELS)?.getattr("SqlTestChainStep")?,
        &[
            ("target_model_name", object(py, target)?),
            ("expected_column_names", columns.into_any()),
            ("ctes", ctes_tuple(py, ctes)?.into_any()),
            ("actual_query", object(py, actual)?),
            ("expected_query", object(py, expected)?),
        ],
    )
}

/// One comparison per authored expected target, in dependency order.
fn chain_steps<'py>(
    assembler: &mut Assembler<'_, 'py>,
    authored: &[(String, String)],
    dialect: &str,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = assembler.py;
    let mut steps: Vec<(usize, Bound<'py, PyAny>)> = Vec::new();
    for expected in assembler.payload.getattr("expected_targets")?.try_iter()? {
        let expected: Bound<'py, PyAny> = expected?;
        let expected_name: String = expected.getattr("name")?.extract()?;
        let target: &str = strip_prefix(&expected_name, EXPECTED_PREFIX);
        let Some(entry) = assembler.chain.registry.get_item(target)? else {
            return Err(assembly_error(
                py,
                format!(
                    "SQL test '{}' targets unknown model '{target}'",
                    assembler.path
                ),
            ));
        };
        let actual_cte_name: String = assembler.resolve(target)?;
        let position: usize = *assembler
            .position_by_name
            .get(target)
            .ok_or_else(|| PyKeyError::new_err(target.to_owned()))?;
        let expected_query: String = expected.getattr("query")?.extract()?;
        let columns: Bound<'py, PyTuple> = column_names(
            py,
            &expected_query,
            &assembler.path,
            &format!("{EXPECTED_COLUMNS_LABEL}{target}"),
            authored,
            dialect,
        )?;
        let mut outputs: Vec<(String, String)> = Vec::new();
        for column in entry.getattr("output_columns")?.try_iter()? {
            let column: Bound<'py, PyAny> = column?;
            outputs.push((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
            ));
        }
        let (actual_query, typed_expected) = match sql_test_chain_step_queries(
            columns.extract()?,
            outputs,
            &actual_cte_name,
            &expected_query,
        ) {
            Ok(queries) => queries,
            Err(error) if error.is_instance_of::<PyValueError>(py) => {
                let missing: Vec<String> =
                    error.value(py).getattr("args")?.get_item(0)?.extract()?;
                let message: String = format!(
                    "SQL test '{}' expects columns not produced by '{target}': {}",
                    assembler.path,
                    missing.join(", ")
                );
                return Err(assembly_error_from(py, message, &error, false));
            }
            Err(error) => return Err(error),
        };
        let ctes: Ctes = authored
            .iter()
            .chain(&assembler.assembled)
            .cloned()
            .collect();
        steps.push((
            position,
            chain_step(py, target, columns, &ctes, actual_query, typed_expected)?,
        ));
    }
    steps.sort_by_key(|(position, _)| *position);
    Ok(steps.into_iter().map(|(_, step)| step).collect())
}

/// One zero-row assertion per authored `__assert__` CTE.
fn assertion_steps<'py>(
    assembler: &mut Assembler<'_, 'py>,
    authored: &[(String, String)],
    dialect: &str,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = assembler.py;
    let step_class: Bound<'py, PyAny> =
        py.import(TESTING_MODELS)?.getattr("SqlTestAssertionStep")?;
    let mut steps: Vec<Bound<'py, PyAny>> = Vec::new();
    for assertion in assembler.payload.getattr("assertions")?.try_iter()? {
        let assertion: Bound<'py, PyAny> = assertion?;
        let full_name: String = assertion.getattr("name")?.extract()?;
        let name: &str = strip_prefix(&full_name, ASSERT_PREFIX);
        let query: Bound<'py, PyAny> = assertion.getattr("query")?;
        let resolver: Bound<'py, PyDict> = PyDict::new(py);
        for reference in extract_references(py, &query.extract::<String>()?)?.try_iter()? {
            let reference_name: String = reference?.getattr("name")?.extract()?;
            let resolved: String = assembler.resolve(&reference_name)?;
            resolver.set_item(reference_name, resolved)?;
        }
        let resolved_query: String = assembler.rewrite(&query, &resolver)?;
        let ctes: Ctes = authored
            .iter()
            .chain(&assembler.assembled)
            .cloned()
            .collect();
        let columns: Bound<'py, PyTuple> = column_names(
            py,
            &resolved_query,
            &assembler.path,
            &format!("{ASSERTION_COLUMNS_LABEL}{name}"),
            &ctes,
            dialect,
        )?;
        steps.push(call(
            &step_class,
            &[
                ("name", object(py, name)?),
                ("column_names", columns.into_any()),
                ("ctes", ctes_tuple(py, &ctes)?.into_any()),
                ("query", object(py, resolved_query)?),
            ],
        )?);
    }
    Ok(steps)
}

/// The one adapter statement that runs every chain and assertion comparison.
fn comparison_query<'py>(
    py: Python<'py>,
    renderer: &Bound<'py, PyAny>,
    targets: &[Bound<'py, PyAny>],
    assertions: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    let target_class: Bound<'py, PyAny> = models.getattr("AdapterSetDifferenceTarget")?;
    let mut requested: Vec<Bound<'py, PyAny>> = Vec::new();
    for step in targets {
        requested.push(call(
            &target_class,
            &[
                ("name", step.getattr("target_model_name")?),
                ("column_names", step.getattr("expected_column_names")?),
                ("ctes", step.getattr("ctes")?),
                ("actual_query", step.getattr("actual_query")?),
                ("expected_query", step.getattr("expected_query")?),
            ],
        )?);
    }
    for step in assertions {
        requested.push(call(
            &target_class,
            &[
                (
                    "name",
                    object(
                        py,
                        format!("{ASSERTION_TARGET_PREFIX}{}", step.getattr("name")?.str()?),
                    )?,
                ),
                ("column_names", step.getattr("column_names")?),
                ("ctes", step.getattr("ctes")?),
                ("actual_query", step.getattr("query")?),
                ("expected_query", py.None().into_bound(py)),
            ],
        )?);
    }
    let request: Bound<'py, PyAny> = call(
        &models.getattr("AdapterSetDifferenceComparisonRequest")?,
        &[("targets", PyTuple::new(py, requested)?.into_any())],
    )?;
    call(renderer, &[("request", request)])
}

fn require_payload<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
    class: &str,
    mode: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let payload: Bound<'py, PyAny> = loaded_test.getattr("payload")?;
    if payload.is_instance(&py.import(TEST_DISCOVERY_MODELS)?.getattr(class)?)? {
        return Ok(payload);
    }
    Err(assembly_error(
        py,
        format!(
            "SQL test '{}' is not a {mode}-mode test",
            loaded_test.getattr("file_path")?.str()?
        ),
    ))
}

/// The `SqlTestCase` of one test, with its quality identity.
fn test_case<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
    entries: &[(&str, Bound<'py, PyAny>)],
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let class: Bound<'py, PyAny> = py.import(TESTING_MODELS)?.getattr("SqlTestCase")?;
    let mut arguments: Vec<(&str, Bound<'py, PyAny>)> = vec![
        ("file_path", loaded_test.getattr("file_path")?),
        ("test_index", loaded_test.getattr("test_index")?),
        ("name", loaded_test.getattr("name")?),
    ];
    arguments.extend(entries.iter().cloned());
    let built: Bound<'py, PyAny> = call(&class, &arguments)?;
    let identity: Bound<'py, PyAny> =
        build_test_quality_identity(py, loaded_test, &built, dialect)?;
    arguments.push(("quality_identity", identity));
    call(&class, &arguments)
}

fn model_test_case<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
    chain: &Chain<'py>,
    rewriter: &Bound<'py, PyAny>,
    renderer: &Bound<'py, PyAny>,
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let payload: Bound<'py, PyAny> =
        require_payload(py, loaded_test, "SqlTestModelPayload", "model")?;
    let path: String = loaded_test.getattr("file_path")?.str()?.extract()?;
    let mut assembler: Assembler<'_, 'py> = Assembler::new(path, &payload, rewriter, chain)?;
    let mut authored: Ctes = Vec::new();
    for cte in loaded_test.getattr("authored_ctes")?.try_iter()? {
        let cte: Bound<'py, PyAny> = cte?;
        authored.push((
            cte.getattr("name")?.extract()?,
            cte.getattr("query")?.extract()?,
        ));
    }
    let targets: Vec<Bound<'py, PyAny>> = chain_steps(&mut assembler, &authored, dialect)?;
    let assertions: Vec<Bound<'py, PyAny>> = assertion_steps(&mut assembler, &authored, dialect)?;
    let query: Bound<'py, PyAny> = comparison_query(py, renderer, &targets, &assertions)?;
    test_case(
        py,
        loaded_test,
        &[
            ("query", query),
            ("target_cases", PyTuple::new(py, targets)?.into_any()),
            ("assertion_cases", PyTuple::new(py, assertions)?.into_any()),
            (
                "warnings",
                PyTuple::new(py, assembler.unreachable_mock_warnings()?)?.into_any(),
            ),
        ],
        dialect,
    )
}

fn macro_test_case<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
    renderer: &Bound<'py, PyAny>,
    dialect: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let payload: Bound<'py, PyAny> =
        require_payload(py, loaded_test, "SqlTestMacroPayload", "macro")?;
    let path: String = loaded_test.getattr("file_path")?.str()?.extract()?;
    let mut authored: Ctes = Vec::new();
    for cte in loaded_test.getattr("authored_ctes")?.try_iter()? {
        let cte: Bound<'py, PyAny> = cte?;
        authored.push((
            cte.getattr("name")?.extract()?,
            cte.getattr("query")?.extract()?,
        ));
    }
    let expected_query: String = payload.getattr("expected")?.getattr("query")?.extract()?;
    let actual_query: String = payload.getattr("actual")?.getattr("query")?.extract()?;
    let expected: Bound<'py, PyTuple> = column_names(
        py,
        &expected_query,
        &path,
        MACRO_EXPECTED_LABEL,
        &authored,
        dialect,
    )?;
    let actual: Bound<'py, PyTuple> = column_names(
        py,
        &actual_query,
        &path,
        MACRO_ACTUAL_LABEL,
        &authored,
        dialect,
    )?;
    if !actual.eq(&expected)? {
        let expected_names: Vec<String> = expected.extract()?;
        let actual_names: Vec<String> = actual.extract()?;
        return Err(assembly_error(
            py,
            format!(
                "SQL test '{path}' mode 'macro' must project the same column names in \
                 {MACRO_ACTUAL_LABEL} and {MACRO_EXPECTED_LABEL}; got {} versus {}",
                actual_names.join(", "),
                expected_names.join(", ")
            ),
        ));
    }
    let target: String = format!("{MACRO_TARGET_PREFIX}{}", node_name(loaded_test)?);
    let step: Bound<'py, PyAny> = chain_step(
        py,
        &target,
        expected,
        &authored,
        actual_query,
        expected_query,
    )?;
    let targets: Vec<Bound<'py, PyAny>> = vec![step];
    let query: Bound<'py, PyAny> = comparison_query(py, renderer, &targets, &[])?;
    test_case(
        py,
        loaded_test,
        &[
            ("query", query),
            ("target_cases", PyTuple::new(py, targets)?.into_any()),
        ],
        dialect,
    )
}

/// Assemble discovered SQL tests into executable comparison statements against the compiled
/// pipelines.
#[pyfunction]
#[pyo3(signature = (*, loaded_tests, compiled_pipelines, reference_rewriter, comparison_renderer, dialect))]
pub(crate) fn build_sql_test_cases<'py>(
    py: Python<'py>,
    loaded_tests: &Bound<'py, PyAny>,
    compiled_pipelines: &Bound<'py, PyAny>,
    reference_rewriter: &Bound<'py, PyAny>,
    comparison_renderer: &Bound<'py, PyAny>,
    dialect: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let chain: Chain<'py> = Chain::new(py, compiled_pipelines)?;
    let mut cases: Vec<Bound<'py, PyAny>> = Vec::new();
    for loaded_test in loaded_tests.try_iter()? {
        let loaded_test: Bound<'py, PyAny> = loaded_test?;
        cases.push(
            if loaded_test
                .getattr("mode")?
                .eq(PyString::new(py, MACRO_MODE))?
            {
                macro_test_case(py, &loaded_test, comparison_renderer, dialect)?
            } else {
                model_test_case(
                    py,
                    &loaded_test,
                    &chain,
                    reference_rewriter,
                    comparison_renderer,
                    dialect,
                )?
            },
        );
    }
    PyTuple::new(py, cases)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(replace_refs, module)?)?;
    module.add_function(wrap_pyfunction!(build_test_quality_identity, module)?)?;
    module.add_function(wrap_pyfunction!(build_audit_quality_identity, module)?)?;
    module.add_function(wrap_pyfunction!(require_quality_identity, module)?)?;
    module.add_function(wrap_pyfunction!(build_sql_test_cases, module)?)?;
    Ok(())
}
