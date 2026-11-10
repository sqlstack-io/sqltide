//! Python bindings for SQL test queries, quality identities, and manifest contents.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_compile::quality::{self, AuditIdentityInput, QualityFingerprints, TestIdentityInput};
use sqltide_compile::sql_test_chain::{ChainError, resolve_chain};

/// `(binding key, definition fingerprint, execution fingerprint)` of one SQL test, from its
/// canonicalized SQL.
#[pyfunction]
pub(crate) fn quality_test_identity(
    node_name: &str,
    mode: &str,
    assertion_names: Vec<String>,
    expected_targets: Vec<String>,
    authored_ctes: Vec<(String, String)>,
    execution_sql: &str,
) -> QualityFingerprints {
    quality::test_identity(TestIdentityInput {
        node_name,
        mode,
        assertion_names: assertion_names.iter().map(String::as_str).collect(),
        expected_targets: expected_targets.iter().map(String::as_str).collect(),
        authored_ctes: authored_ctes
            .iter()
            .map(|(name, sql)| (name.as_str(), sql.as_str()))
            .collect(),
        execution_sql,
    })
}

/// `(binding key, definition fingerprint, execution fingerprint)` of one SQL audit, from its
/// canonicalized SQL.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(crate) fn quality_audit_identity(
    node_name: &str,
    attached_column: Option<&str>,
    attached_model: Option<&str>,
    attachment_kind: &str,
    referenced_models: Vec<String>,
    severity: &str,
    definition_sql: &str,
    execution_sql: &str,
) -> QualityFingerprints {
    quality::audit_identity(AuditIdentityInput {
        node_name,
        attached_column,
        attached_model,
        attachment_kind,
        referenced_models: referenced_models.iter().map(String::as_str).collect(),
        severity,
        definition_sql,
        execution_sql,
    })
}

/// Every compiled model's references, converted once per compile and shared by its tests.
#[pyclass(name = "SqlTestChainRegistry", module = "sqltide._native", frozen)]
struct SqlTestChainRegistry {
    refs_by_model: HashMap<String, Vec<String>>,
}

#[pymethods]
impl SqlTestChainRegistry {
    #[new]
    fn new(refs_by_model: HashMap<String, Vec<String>>) -> Self {
        Self { refs_by_model }
    }

    /// `(models newly assembled in dependency order, mocks reached)` for one logical relation.
    /// A cycle raises `ValueError(("cycle", name))`; an unknown relation raises
    /// `ValueError(("unresolved", name))`.
    fn resolve(
        &self,
        logical_name: &str,
        mocks: HashSet<String>,
        assembled: HashSet<String>,
    ) -> PyResult<(Vec<String>, Vec<String>)> {
        resolve_chain(logical_name, &self.refs_by_model, &mocks, &assembled)
            .map(|resolution| (resolution.assembled, resolution.used_mocks))
            .map_err(|error| match error {
                ChainError::Cycle(name) => PyValueError::new_err(("cycle", name)),
                ChainError::Unresolved(name) => PyValueError::new_err(("unresolved", name)),
            })
    }
}

/// `query` preceded by a `WITH` clause of `(name, query)` CTEs.
#[pyfunction]
#[allow(clippy::needless_pass_by_value)]
pub(crate) fn sql_test_wrap_with_ctes(query: &str, ctes: Vec<(String, String)>) -> String {
    let ctes: Vec<(&str, &str)> = ctes
        .iter()
        .map(|(name, cte_query)| (name.as_str(), cte_query.as_str()))
        .collect();
    sqltide_compile::sql_test_queries::wrap_with_ctes(query, &ctes)
}

/// `(actual query, typed expected query)` of one SQL test model target. Raises
/// `ValueError(missing column names)` when the model does not produce an expected column.
#[pyfunction]
#[allow(clippy::needless_pass_by_value)]
pub(crate) fn sql_test_chain_step_queries(
    expected_column_names: Vec<String>,
    output_columns: Vec<(String, String)>,
    actual_cte_name: &str,
    expected_query: &str,
) -> PyResult<(String, String)> {
    let expected: Vec<&str> = expected_column_names.iter().map(String::as_str).collect();
    let outputs: Vec<(&str, &str)> = output_columns
        .iter()
        .map(|(name, column_type)| (name.as_str(), column_type.as_str()))
        .collect();
    sqltide_compile::sql_test_queries::chain_step_queries(
        &expected,
        &outputs,
        actual_cte_name,
        expected_query,
    )
    .map_err(|missing| PyValueError::new_err((missing,)))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(sql_test_wrap_with_ctes, module)?)?;
    module.add_function(wrap_pyfunction!(sql_test_chain_step_queries, module)?)?;
    module.add_function(wrap_pyfunction!(quality_test_identity, module)?)?;
    module.add_function(wrap_pyfunction!(quality_audit_identity, module)?)?;
    module.add_class::<SqlTestChainRegistry>()
}
