//! SQL audit execution: resolve each audit's model refs, count and sample its failing rows,
//! and load the model history that anchors audit warmup.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyFrozenSet, PyInt, PyString, PyTuple};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const MODELS: &str = "sqltide.executor.auditing.models";
const NATIVE: &str = "sqltide._native";

type Any<'py> = Bound<'py, PyAny>;

fn audit_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(
            py,
            "sqltide.executor.auditing.exceptions",
            "AuditExecutionError",
        )?
        .call1((message,))?,
    ))
}

/// The failing row count an audit's count query reports.
fn failing_row_count(py: Python<'_>, query: &str, client: &Any<'_>) -> PyResult<Py<PyAny>> {
    let sql: Any<'_> = attr(py, NATIVE, "render_clickhouse_audit_count_query")?.call1((query,))?;
    let rows: Any<'_> = client.call_method1("query", (sql,))?.getattr("rows")?;
    if !rows.is_truthy()? {
        return Err(audit_error(
            py,
            "Expected a count row from SQL audit execution",
        )?);
    }
    let value: Any<'_> = rows.get_item(0)?.get_item(0)?;
    let message: &str = "SQL audit count query returned a non-numeric row count";
    if !(value.is_instance_of::<PyInt>()
        || value.is_instance_of::<PyFloat>()
        || value.is_instance_of::<PyString>())
    {
        return Err(audit_error(py, message)?);
    }
    match attr(py, "builtins", "int")?.call1((&value,)) {
        Ok(count) => Ok(count.unbind()),
        Err(error)
            if error.is_instance_of::<pyo3::exceptions::PyOverflowError>(py)
                || error.is_instance_of::<pyo3::exceptions::PyValueError>(py) =>
        {
            let raised: PyErr = audit_error(py, message)?;
            let value: Any<'_> = raised.value(py).clone().into_any();
            value.setattr("__cause__", error.value(py))?;
            value.setattr("__context__", error.value(py))?;
            Err(raised)
        }
        Err(error) => Err(error),
    }
}

fn single_audit<'py>(
    audit: &Any<'py>,
    resolver: &Any<'py>,
    client: &Any<'py>,
    rewriter: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let resolved: String = attr(py, NATIVE, "replace_refs")?
        .call(
            (),
            Some(&py_kwargs!(py;
                sql = audit.getattr("query")?,
                resolver = PyDict::from_sequence(&resolver.call_method0("items")?)?,
                rewriter = rewriter,
            )?),
        )?
        .extract()?;
    let mut count: Py<PyAny> = 0_i64.into_pyobject(py)?.into_any().unbind();
    let mut sample_columns: Any<'py> = PyTuple::empty(py).into_any();
    let mut sample_rows: Any<'py> = PyTuple::empty(py).into_any();
    let attempt: PyResult<()> = (|| {
        count = failing_row_count(py, &resolved, client)?;
        if count.bind(py).is_truthy()? {
            let limit: Any<'py> = attr(
                py,
                "sqltide.executor.auditing.constants",
                "AUDIT_SAMPLE_LIMIT",
            )?;
            let sql: Any<'py> = attr(py, NATIVE, "render_clickhouse_audit_sample_query")?
                .call1((&resolved, limit))?;
            let sample: Any<'py> = client.call_method1("query", (sql,))?;
            sample_columns = sample.getattr("column_names")?;
            sample_rows = sample.getattr("rows")?;
        }
        Ok(())
    })();
    let fields = py_kwargs!(py;
        file_path = audit.getattr("file_path")?,
        referenced_model_names = audit.getattr("referenced_model_names")?,
        severity = audit.getattr("severity")?,
        sample_column_names = &sample_columns,
        sample_rows = &sample_rows,
        description = audit.getattr("description")?,
        name = audit.getattr("name")?,
    )?;
    match attempt {
        Ok(()) => {
            fields.set_item("passed", count.bind(py).eq(0)?)?;
            fields.set_item("failing_row_count", count.bind(py))?;
            fields.set_item("error_message", py.None())?;
        }
        Err(error) => {
            let handled: bool = error.value(py).is_instance(&attr(
                py,
                "sqltide.adapter.exceptions",
                "AdapterError",
            )?)? || error.value(py).is_instance(&attr(
                py,
                "sqltide.executor.auditing.exceptions",
                "AuditExecutionError",
            )?)?;
            if !handled {
                return Err(error);
            }
            let at_least_one: Any<'py> = attr(py, "builtins", "max")?.call1((count.bind(py), 1))?;
            fields.set_item("passed", false)?;
            fields.set_item("failing_row_count", at_least_one)?;
            fields.set_item("error_message", error.value(py).str()?)?;
        }
    }
    attr(py, MODELS, "SqlAuditResult")?.call((), Some(&fields))
}

/// Execute discovered SQL audits with resolved model refs.
#[pyfunction]
#[pyo3(signature = (*, loaded_audits, resolver, client, dialect))]
fn execute_sql_audits<'py>(
    loaded_audits: &Any<'py>,
    resolver: &Any<'py>,
    client: &Any<'py>,
    dialect: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let rewriter: Any<'py> = attr(py, NATIVE, "SqlReferenceRewriter")?
        .call((), Some(&py_kwargs!(py; dialect = dialect)?))?;
    let mut results: Vec<Any<'py>> = Vec::new();
    for audit in loaded_audits.try_iter()? {
        results.push(single_audit(&audit?, resolver, client, &rewriter)?);
    }
    attr(py, MODELS, "SqlAuditRunResult")?.call(
        (),
        Some(&py_kwargs!(py; audit_results = PyTuple::new(py, results)?)?),
    )
}

/// Latest successful apply or publication timestamps by logical model.
#[pyfunction]
#[pyo3(signature = (*, client, metadata_database, target_database, model_names, virtual_environments))]
fn load_model_anchors<'py>(
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    target_database: &Any<'py>,
    model_names: &Any<'py>,
    virtual_environments: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = client.py();
    let anchors: Bound<'py, PyDict> = PyDict::new(py);
    if virtual_environments {
        let inventory: Any<'py> =
            client.call_method1("load_deployment_inventory", (metadata_database,))?;
        for event in inventory.getattr("publish_events")?.try_iter()? {
            let event: Any<'py> = event?;
            let published_at: Any<'py> = event.getattr("published_at")?;
            for model_name in event.getattr("logical_view_names")?.try_iter()? {
                let model_name: Any<'py> = model_name?;
                let current: Any<'py> = match anchors.get_item(&model_name)? {
                    Some(current) => current,
                    None => PyString::new(py, "").into_any(),
                };
                if model_names.contains(&model_name)? && published_at.gt(current)? {
                    anchors.set_item(&model_name, &published_at)?;
                }
            }
        }
        return Ok(anchors);
    }
    let prefix: String = format!("{}.", target_database.str()?);
    let identities: Vec<String> = model_names
        .try_iter()?
        .map(|name| Ok(format!("{prefix}{}", name?.str()?)))
        .collect::<PyResult<_>>()?;
    let snapshot: Any<'py> = client.call_method(
        "load_direct_fingerprints",
        (),
        Some(&py_kwargs!(py;
            database = metadata_database,
            logical_model_identities = PyTuple::new(py, identities)?,
        )?),
    )?;
    for record in snapshot.getattr("baselines")?.try_iter()? {
        let record: Any<'py> = record?;
        let applied_at: Any<'py> = record.getattr("applied_at")?;
        if applied_at.is_none() {
            continue;
        }
        let identity: Any<'py> = record
            .getattr("logical_model_identity")?
            .call_method1("removeprefix", (&prefix,))?;
        anchors.set_item(identity, applied_at)?;
    }
    Ok(anchors)
}

/// Logical model names backed by a relation in the target catalog.
#[pyfunction]
#[pyo3(signature = (*, client, database, relation_name_by_model))]
fn load_materialized_model_names<'py>(
    client: &Any<'py>,
    database: &Any<'py>,
    relation_name_by_model: &Any<'py>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let py: Python<'py> = client.py();
    let relation_names: Any<'py> = client
        .call_method1("load_catalog", (database,))?
        .call_method0("relation_names")?;
    let mut names: Vec<Any<'py>> = Vec::new();
    for item in relation_name_by_model.call_method0("items")?.try_iter()? {
        let (model_name, relation_name): (Any<'py>, Any<'py>) = item?.extract()?;
        if relation_names.contains(relation_name)? {
            names.push(model_name);
        }
    }
    PyFrozenSet::new(py, names)
}

/// A warming audit's result, without fabricating a pass or failure.
#[pyfunction]
#[pyo3(signature = (*, audit, state))]
fn deferred_audit_result<'py>(audit: &Any<'py>, state: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = audit.py();
    attr(py, MODELS, "SqlAuditResult")?.call(
        (),
        Some(&py_kwargs!(py;
            file_path = audit.getattr("file_path")?,
            referenced_model_names = audit.getattr("referenced_model_names")?,
            severity = audit.getattr("severity")?,
            passed = false,
            failing_row_count = 0,
            sample_column_names = PyTuple::empty(py),
            sample_rows = PyTuple::empty(py),
            description = audit.getattr("description")?,
            name = audit.getattr("name")?,
            deferred_until = state.getattr("eligible_at")?,
            missing_relation_names = state.getattr("missing_model_names")?,
        )?),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_sql_audits, module)?)?;
    module.add_function(wrap_pyfunction!(load_model_anchors, module)?)?;
    module.add_function(wrap_pyfunction!(load_materialized_model_names, module)?)?;
    module.add_function(wrap_pyfunction!(deferred_audit_result, module)?)
}
