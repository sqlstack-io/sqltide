//! The project access policy: `access.yml` compiled natively into the access dataclasses.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_config::access::{
    self, AccessGrant, AccessPolicy, AccessPolicyError, AccessRole, PIPELINE_PERMISSIONS,
    PROJECT_PERMISSIONS, TARGET_PERMISSIONS,
};

const MODELS_MODULE: &str = "sqltide.compiler.access.models";
const TYPES_MODULE: &str = "sqltide.compiler.access.types";
const EXCEPTIONS_MODULE: &str = "sqltide.compiler.access.exceptions";
const DIAGNOSTICS_MODULE: &str = "sqltide.diagnostics.models";

struct Classes<'py> {
    grant: Bound<'py, PyAny>,
    role: Bound<'py, PyAny>,
    policy: Bound<'py, PyAny>,
    permission: Bound<'py, PyAny>,
    scope: Bound<'py, PyAny>,
}

impl<'py> Classes<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        let models = py.import(MODELS_MODULE)?;
        let types = py.import(TYPES_MODULE)?;
        Ok(Self {
            grant: models.getattr("CompiledAccessGrant")?,
            role: models.getattr("CompiledAccessRole")?,
            policy: models.getattr("CompiledAccessPolicy")?,
            permission: types.getattr("Permission")?,
            scope: types.getattr("GrantScope")?,
        })
    }

    fn grant(&self, py: Python<'py>, grant: &AccessGrant) -> PyResult<Bound<'py, PyAny>> {
        let permissions = grant
            .permissions
            .iter()
            .map(|permission| self.permission.call1((*permission,)))
            .collect::<PyResult<Vec<_>>>()?;
        let scope: Option<Bound<'py, PyAny>> = grant
            .scope
            .map(|scope| self.scope.call1((scope,)))
            .transpose()?;
        let arguments = PyDict::new(py);
        arguments.set_item("permissions", PyTuple::new(py, permissions)?)?;
        arguments.set_item("pipelines", PyTuple::new(py, &grant.pipelines)?)?;
        arguments.set_item("scope", scope)?;
        self.grant.call((), Some(&arguments))
    }

    fn role(&self, py: Python<'py>, role: &AccessRole) -> PyResult<Bound<'py, PyAny>> {
        let grants = role
            .grants
            .iter()
            .map(|grant| self.grant(py, grant))
            .collect::<PyResult<Vec<_>>>()?;
        let arguments = PyDict::new(py);
        arguments.set_item("name", &role.name)?;
        arguments.set_item("description", &role.description)?;
        arguments.set_item("grants", PyTuple::new(py, grants)?)?;
        self.role.call((), Some(&arguments))
    }

    fn policy(&self, py: Python<'py>, policy: &AccessPolicy) -> PyResult<Bound<'py, PyAny>> {
        let roles = policy
            .roles
            .iter()
            .map(|role| self.role(py, role))
            .collect::<PyResult<Vec<_>>>()?;
        let arguments = PyDict::new(py);
        arguments.set_item("roles", PyTuple::new(py, roles)?)?;
        arguments.set_item("fingerprint", &policy.fingerprint)?;
        self.policy.call((), Some(&arguments))
    }
}

/// The failure as `AccessPolicyError` at its line and column of `source_path`.
fn policy_error(py: Python<'_>, source_path: &Bound<'_, PyAny>, error: AccessPolicyError) -> PyErr {
    let raised = || -> PyResult<PyErr> {
        let location = PyDict::new(py);
        location.set_item("path", source_path)?;
        location.set_item("line", error.line)?;
        location.set_item("column", error.column)?;
        let location = py
            .import(DIAGNOSTICS_MODULE)?
            .getattr("SourceLocation")?
            .call((), Some(&location))?;
        let arguments = PyDict::new(py);
        arguments.set_item("location", location)?;
        let exception = py
            .import(EXCEPTIONS_MODULE)?
            .getattr("AccessPolicyError")?
            .call((&error.message,), Some(&arguments))?;
        Ok(PyErr::from_value(exception))
    };
    raised().unwrap_or_else(|failure| failure)
}

/// Compile `access.yml` text into `CompiledAccessPolicy`, raising `AccessPolicyError`.
#[pyfunction]
fn compile_access_policy<'py>(
    py: Python<'py>,
    contents: &str,
    source_path: &Bound<'py, PyAny>,
    pipeline_names: HashSet<String>,
) -> PyResult<Bound<'py, PyAny>> {
    match access::compile_access_policy(contents, &pipeline_names) {
        Ok(policy) => Classes::new(py)?.policy(py, &policy),
        Err(error) => Err(policy_error(py, source_path, error)),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(compile_access_policy, module)?)?;
    module.add("ACCESS_PIPELINE_PERMISSIONS", PIPELINE_PERMISSIONS)?;
    module.add("ACCESS_PROJECT_PERMISSIONS", PROJECT_PERMISSIONS)?;
    module.add("ACCESS_TARGET_PERMISSIONS", TARGET_PERMISSIONS)
}
