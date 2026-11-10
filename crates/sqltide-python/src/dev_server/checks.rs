//! The dev server's manual audit and test runs, recorded like CLI runs, and the last-known outcome
//! of every audit and test read back from the observability tables.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PySlice, PyTuple};

use super::audits::{
    audit_name, fstring, literal, logical_project_identity, member, persist, terminal_invocation,
};
use super::routes::DEV_EXCEPTIONS;
use super::support::attr;
use crate::py_kwargs;
use crate::test_command::strict_pair;

const NATIVE: &str = "sqltide._native";
const NEVER_RUN_STATUS: &str = "never_run";

type Any<'py> = Bound<'py, PyAny>;

fn call<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    kwargs: Bound<'py, PyDict>,
) -> PyResult<Any<'py>> {
    attr(py, module, name)?.call((), Some(&kwargs))
}

fn list_of<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "list")?.call1((value,))
}

/// `[list(item) for item in rows]`, or of `rows[:limit]`.
fn row_lists<'py>(rows: &Any<'py>, limit: Option<isize>) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = rows.py();
    let rows: Any<'py> = match limit {
        Some(limit) => rows.get_item(PySlice::new(py, 0, limit, 1))?,
        None => rows.clone(),
    };
    let lists: Bound<'py, PyList> = PyList::empty(py);
    for row in rows.try_iter()? {
        lists.append(list_of(&row?)?)?;
    }
    Ok(lists)
}

fn dev_server_error<'py>(py: Python<'py>, parts: &[Any<'py>]) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(py, DEV_EXCEPTIONS, "DevServerError")?.call1((fstring(py, parts)?,))?,
    ))
}

fn require_quality_identity<'py>(node: &Any<'py>) -> PyResult<Any<'py>> {
    attr(node.py(), NATIVE, "require_quality_identity")?.call1((node.getattr("quality_identity")?,))
}

fn manual_trigger(py: Python<'_>) -> PyResult<Any<'_>> {
    member(
        py,
        "sqltide.executor.observability.types",
        "QualityResultTrigger",
        "MANUAL",
    )
}

fn source_summary(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let summary: Bound<'_, PyDict> = PyDict::new(py);
    summary.set_item("source", "dev_server")?;
    Ok(summary)
}

/// Model name to relation name for every compiled model relation.
fn model_resolver<'py>(analysis: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    let model: Any<'py> = member(
        py,
        "sqltide.compiler.compile.types",
        "LogicalResourceType",
        "MODEL",
    )?;
    let resolver: Bound<'py, PyDict> = PyDict::new(py);
    let relations: Any<'py> = analysis
        .getattr("realized_project")?
        .getattr("relation_name_by_logical_key")?;
    for item in relations.call_method0("items")?.try_iter()? {
        let item: Any<'py> = item?;
        let key: Any<'py> = item.get_item(0)?;
        if key.getattr("resource_type")?.eq(&model)? {
            resolver.set_item(key.getattr("name")?, item.get_item(1)?)?;
        }
    }
    Ok(resolver)
}

/// `tuple(sorted(set().union(audit.referenced_model_names for audit in audits)))`.
fn referenced_model_names<'py>(
    py: Python<'py>,
    audits: &[Any<'py>],
) -> PyResult<Bound<'py, PyTuple>> {
    let names: Bound<'py, PySet> = PySet::empty(py)?;
    for audit in audits {
        for name in audit.getattr("referenced_model_names")?.try_iter()? {
            names.add(name?)?;
        }
    }
    let sorted: Any<'py> = attr(py, "builtins", "sorted")?.call1((names,))?;
    PyTuple::new(py, sorted.try_iter()?.collect::<PyResult<Vec<_>>>()?)
}

fn load_model_anchors<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    database: &Any<'py>,
    model_names: Bound<'py, PyTuple>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    call(
        py,
        "sqltide.executor.auditing.main._load_model_anchors",
        "load_model_anchors",
        py_kwargs!(py;
            client = connection,
            metadata_database = database,
            target_database = database,
            model_names = model_names,
            virtual_environments = analysis.getattr("compile_inputs")?.getattr("virtual_environments")?,
        )?,
    )
}

fn load_materialized_model_names<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    resolver: &Bound<'py, PyDict>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    call(
        py,
        "sqltide.executor.auditing.main._load_materialized_model_names",
        "load_materialized_model_names",
        py_kwargs!(py; client = connection, database = database, relation_name_by_model = resolver)?,
    )
}

fn start_invocation(py: Python<'_>) -> PyResult<Any<'_>> {
    attr(py, "sqltide._native", "start_invocation")?.call0()
}

fn node_result_record<'py>(fields: Bound<'py, PyDict>) -> PyResult<Any<'py>> {
    let py: Python<'py> = fields.py();
    call(py, "sqltide._native", "build_node_result_record", fields)
}

/// The UI status word of one manual audit result.
fn audit_status(result: &Any<'_>) -> PyResult<&'static str> {
    let py: Python<'_> = result.py();
    if result.getattr("deferred")?.is_truthy()? {
        return Ok("deferred");
    }
    if !result.getattr("error_message")?.is_none() {
        return Ok("error");
    }
    if result.getattr("passed")?.is_truthy()? {
        return Ok("passed");
    }
    let warning: Any<'_> = member(
        py,
        "sqltide.executor.auditing.types",
        "AuditSeverity",
        "WARNING",
    )?;
    if PyAnyMethods::eq(result.getattr("severity")?.str()?.as_any(), warning)? {
        return Ok("warning");
    }
    Ok("failed")
}

/// Execute one named audit and record its outcome like a CLI run would.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, name, project_dir, database))]
pub(crate) fn run_one_audit<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    name: &Any<'py>,
    project_dir: &Any<'py>,
    database: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let batch: Any<'py> = attr(py, NATIVE, "run_audit_batch")?.call(
        (),
        Some(&py_kwargs!(py;
            analysis = analysis,
            connection = connection,
            names = PyTuple::new(py, [name])?,
            project_dir = project_dir,
            database = database,
        )?),
    )?;
    batch.get_item(0)
}

/// Execute named audits with one shared setup and persistence boundary.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, names, project_dir, database))]
pub(crate) fn run_audit_batch<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    names: &Any<'py>,
    project_dir: &Any<'py>,
    database: &Any<'py>,
) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = analysis.py();
    let audits_by_name: Bound<'py, PyDict> = PyDict::new(py);
    for audit in analysis
        .getattr("compiled_project")?
        .getattr("audits")?
        .try_iter()?
    {
        let audit: Any<'py> = audit?;
        audits_by_name.set_item(audit_name(&audit)?, &audit)?;
    }
    let names: Vec<Any<'py>> = names.try_iter()?.collect::<PyResult<_>>()?;
    for name in &names {
        if !audits_by_name.contains(name)? {
            return Err(dev_server_error(
                py,
                &[
                    literal(py, "Unknown audit '"),
                    name.clone(),
                    literal(py, "'"),
                ],
            )?);
        }
    }
    let mut audits: Vec<Any<'py>> = Vec::new();
    for name in &names {
        audits.push(audits_by_name.as_any().get_item(name)?);
    }
    if audits.is_empty() {
        return Ok(PyList::empty(py));
    }
    let audits_tuple: Bound<'py, PyTuple> = PyTuple::new(py, &audits)?;
    let started: Any<'py> = start_invocation(py)?;
    let resolver: Bound<'py, PyDict> = model_resolver(analysis)?;
    let model_names: Bound<'py, PyTuple> = referenced_model_names(py, &audits)?;
    let anchors: Any<'py> = load_model_anchors(analysis, connection, database, model_names)?;
    let materialized: Any<'py> = load_materialized_model_names(connection, database, &resolver)?;
    let warehouse_now: Any<'py> = connection.call_method0("capture_warehouse_timestamp")?;
    let warmup_state: Any<'py> = attr(py, NATIVE, "resolve_audit_warmup_states")?.call(
        (),
        Some(&py_kwargs!(py;
            audits = &audits_tuple,
            anchors_by_model = anchors,
            materialized_model_names = materialized,
            warehouse_now = warehouse_now,
        )?),
    )?;
    let mut eligible: Vec<Any<'py>> = Vec::new();
    for audit in &audits {
        if warmup_state
            .get_item(audit_name(audit)?)?
            .getattr("eligible")?
            .is_truthy()?
        {
            eligible.push(audit.clone());
        }
    }
    let eligible_tuple: Bound<'py, PyTuple> = PyTuple::new(py, &eligible)?;
    let executed: Any<'py> = if eligible.is_empty() {
        PyTuple::empty(py).into_any()
    } else {
        call(
            py,
            "sqltide.executor.auditing.main.execute_sql_audits",
            "execute_sql_audits",
            py_kwargs!(py;
                loaded_audits = &eligible_tuple,
                resolver = &resolver,
                client = connection,
                dialect = analysis.getattr("adapter_profile")?.getattr("sql_analysis_dialect")?,
            )?,
        )?
        .getattr("audit_results")?
    };
    let executed_by_name: Bound<'py, PyDict> = PyDict::new(py);
    let mut eligible_iter = eligible_tuple.as_any().try_iter()?;
    let mut executed_iter = executed.try_iter()?;
    while let Some((audit, result)) = strict_pair(&mut eligible_iter, &mut executed_iter)? {
        executed_by_name.set_item(audit_name(&audit)?, result)?;
    }
    let mut results: Vec<Any<'py>> = Vec::new();
    for audit in &audits {
        let name: Any<'py> = audit_name(audit)?;
        let executed: Any<'py> = executed_by_name.as_any().call_method1("get", (&name,))?;
        results.push(if executed.is_truthy()? {
            executed
        } else {
            call(
                py,
                "sqltide.executor.auditing.main._deferred_audit_result",
                "deferred_audit_result",
                py_kwargs!(py; audit = audit, state = warmup_state.get_item(&name)?)?,
            )?
        });
    }
    persist_audit_observations(
        connection,
        database,
        project_dir,
        &started,
        &audits,
        &results,
    )?;
    let payloads: Bound<'py, PyList> = PyList::empty(py);
    for (name, result) in names.iter().zip(&results) {
        payloads.append(audit_result_payload(name, result)?)?;
    }
    Ok(payloads)
}

fn audit_result_payload<'py>(name: &Any<'py>, result: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = name.py();
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("name", name)?;
    payload.set_item("kind", "audit")?;
    payload.set_item("passed", result.getattr("passed")?)?;
    payload.set_item("severity", result.getattr("severity")?)?;
    payload.set_item("failingRowCount", result.getattr("failing_row_count")?)?;
    payload.set_item(
        "sampleColumns",
        list_of(&result.getattr("sample_column_names")?)?,
    )?;
    payload.set_item(
        "sampleRows",
        row_lists(&result.getattr("sample_rows")?, None)?,
    )?;
    payload.set_item("errorMessage", result.getattr("error_message")?)?;
    payload.set_item("deferredUntil", result.getattr("deferred_until")?)?;
    payload.set_item(
        "missingRelations",
        list_of(&result.getattr("missing_relation_names")?)?,
    )?;
    Ok(payload)
}

fn persist_audit_observations<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
    started: &Any<'py>,
    audits: &[Any<'py>],
    results: &[Any<'py>],
) -> PyResult<()> {
    let py: Python<'py> = connection.py();
    let warning: Any<'py> = member(
        py,
        "sqltide.executor.auditing.types",
        "AuditSeverity",
        "WARNING",
    )?;
    let mut hard_failure: bool = false;
    for result in results {
        if !result.getattr("deferred")?.is_truthy()?
            && (!result.getattr("error_message")?.is_none()
                || !result.getattr("passed")?.is_truthy()?)
            && PyAnyMethods::ne(result.getattr("severity")?.str()?.as_any(), &warning)?
        {
            hard_failure = true;
            break;
        }
    }
    let mut error_message: Any<'py> = py.None().into_bound(py);
    for result in results {
        let message: Any<'py> = result.getattr("error_message")?;
        if !message.is_none() {
            error_message = message;
            break;
        }
    }
    let invocation: Any<'py> = terminal_invocation(
        started,
        py_kwargs!(py;
            project_dir = project_dir,
            target_identity = database,
            command = "audit",
            mode = py.None(),
            outcome = if hard_failure { "failed" } else { "succeeded" },
            exit_code = i64::from(hard_failure),
            materialized_outcome = py.None(),
            deployment_id = py.None(),
            workflow_id = py.None(),
            selected_node_count = audits.len(),
            error_message = error_message,
            summary = source_summary(py)?,
        )?,
    )?;
    let mut node_results: Vec<Any<'py>> = Vec::new();
    for (audit, result) in audits.iter().zip(results) {
        let identity: Any<'py> = require_quality_identity(audit)?;
        let context: Any<'py> = attr(
            py,
            "sqltide.executor.observability.models",
            "QualityResultContext",
        )?
        .call(
            (),
            Some(&py_kwargs!(py;
                trigger = manual_trigger(py)?,
                cadence_seconds = audit.getattr("cadence_seconds")?,
                warmup_seconds = audit.getattr("warmup_seconds")?,
            )?),
        )?;
        let status: &str = audit_status(result)?;
        let severity: Any<'py> = result.getattr("severity")?;
        let failure_count: Any<'py> = result.getattr("failing_row_count")?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item(
            "sample_column_names",
            list_of(&result.getattr("sample_column_names")?)?,
        )?;
        payload.set_item(
            "sample_rows",
            row_lists(&result.getattr("sample_rows")?, Some(5))?,
        )?;
        payload.set_item("eligible_at", result.getattr("deferred_until")?)?;
        payload.set_item(
            "missing_relations",
            list_of(&result.getattr("missing_relation_names")?)?,
        )?;
        node_results.push(node_result_record(py_kwargs!(py;
            invocation = &invocation,
            identity = identity,
            context = context,
            status = status,
            severity = severity,
            failure_count = failure_count,
            payload = payload,
            error_message = result.getattr("error_message")?,
        )?)?);
    }
    persist(
        connection,
        database,
        &invocation,
        &PyTuple::new(py, node_results)?,
    )?;
    Ok(())
}

/// Execute one named SQL test and record its outcome like a CLI run would.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, name, project_dir, database))]
pub(crate) fn run_one_test<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    name: &Any<'py>,
    project_dir: &Any<'py>,
    database: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    let mut test_case: Option<Any<'py>> = None;
    for candidate in analysis
        .getattr("compiled_project")?
        .getattr("test_cases")?
        .try_iter()?
    {
        let candidate: Any<'py> = candidate?;
        if audit_name(&candidate)?.eq(name)? {
            test_case = Some(candidate);
            break;
        }
    }
    let Some(test_case) = test_case else {
        return Err(dev_server_error(
            py,
            &[
                literal(py, "Unknown test '"),
                name.clone(),
                literal(py, "'"),
            ],
        )?);
    };
    let started: Any<'py> = start_invocation(py)?;
    let results: Any<'py> = call(
        py,
        "sqltide._native",
        "execute_sql_tests",
        py_kwargs!(py; test_cases = PyTuple::new(py, [&test_case])?, client = connection)?,
    )?;
    let result: Any<'py> = results.get_item(0)?;
    persist_test_observation(
        connection,
        database,
        project_dir,
        &started,
        &test_case,
        &result,
    )?;
    let targets: Bound<'py, PyList> = PyList::empty(py);
    for target in result.getattr("target_results")?.try_iter()? {
        let target: Any<'py> = target?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("targetModelName", target.getattr("target_model_name")?)?;
        payload.set_item("passed", target.getattr("passed")?)?;
        payload.set_item("columns", list_of(&target.getattr("column_names")?)?)?;
        payload.set_item(
            "missingRows",
            row_lists(&target.getattr("missing_rows")?, None)?,
        )?;
        payload.set_item(
            "unexpectedRows",
            row_lists(&target.getattr("unexpected_rows")?, None)?,
        )?;
        targets.append(payload)?;
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("name", name)?;
    payload.set_item("kind", "test")?;
    payload.set_item("passed", result.getattr("passed")?)?;
    payload.set_item("targets", targets)?;
    payload.set_item("errorMessage", result.getattr("error_message")?)?;
    Ok(payload)
}

/// `sum(len(target.<field>) for target in result.target_results)`.
fn row_total<'py>(result: &Any<'py>, field: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = result.py();
    let mut total: Any<'py> = 0_i64.into_pyobject(py)?.into_any();
    for target in result.getattr("target_results")?.try_iter()? {
        total = total.add(target?.getattr(field)?.len()?)?;
    }
    Ok(total)
}

fn persist_test_observation<'py>(
    connection: &Any<'py>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
    started: &Any<'py>,
    test_case: &Any<'py>,
    result: &Any<'py>,
) -> PyResult<()> {
    let py: Python<'py> = connection.py();
    let missing_count: Any<'py> = row_total(result, "missing_rows")?;
    let unexpected_count: Any<'py> = row_total(result, "unexpected_rows")?;
    let errored: bool = !result.getattr("error_message")?.is_none();
    let failed: bool = errored || !result.getattr("passed")?.is_truthy()?;
    let invocation: Any<'py> = terminal_invocation(
        started,
        py_kwargs!(py;
            project_dir = project_dir,
            target_identity = database,
            command = "test",
            mode = py.None(),
            outcome = if failed { "failed" } else { "succeeded" },
            exit_code = i64::from(failed),
            materialized_outcome = py.None(),
            deployment_id = py.None(),
            workflow_id = py.None(),
            selected_node_count = 1,
            error_message = result.getattr("error_message")?,
            summary = source_summary(py)?,
        )?,
    )?;
    let status: &str = if !result.getattr("error_message")?.is_none() {
        "error"
    } else if result.getattr("passed")?.is_truthy()? {
        "passed"
    } else {
        "failed"
    };
    let identity: Any<'py> = require_quality_identity(test_case)?;
    let context: Any<'py> = attr(
        py,
        "sqltide.executor.observability.models",
        "QualityResultContext",
    )?
    .call((), Some(&py_kwargs!(py; trigger = manual_trigger(py)?)?))?;
    let failure_count: Any<'py> = missing_count
        .add(&unexpected_count)?
        .add(i64::from(!result.getattr("error_message")?.is_none()))?;
    let recorded_targets: Bound<'py, PyList> = PyList::empty(py);
    for target in result.getattr("target_results")?.try_iter()? {
        let target: Any<'py> = target?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("target_model_name", target.getattr("target_model_name")?)?;
        payload.set_item("passed", target.getattr("passed")?)?;
        payload.set_item("columns", list_of(&target.getattr("column_names")?)?)?;
        payload.set_item(
            "missing_rows",
            row_lists(&target.getattr("missing_rows")?, Some(5))?,
        )?;
        payload.set_item(
            "unexpected_rows",
            row_lists(&target.getattr("unexpected_rows")?, Some(5))?,
        )?;
        recorded_targets.append(payload)?;
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("missing_count", &missing_count)?;
    payload.set_item("unexpected_count", &unexpected_count)?;
    payload.set_item("targets", recorded_targets)?;
    let record: Any<'py> = node_result_record(py_kwargs!(py;
        invocation = &invocation,
        identity = identity,
        context = context,
        status = status,
        severity = py.None(),
        failure_count = failure_count,
        payload = payload,
        error_message = result.getattr("error_message")?,
    )?)?;
    persist(
        connection,
        database,
        &invocation,
        &PyTuple::new(py, [record])?,
    )?;
    Ok(())
}

fn quality_node<'py>(identity: &Any<'py>, audit: Option<&Any<'py>>) -> PyResult<Any<'py>> {
    let py: Python<'py> = identity.py();
    let kwargs: Bound<'py, PyDict> = py_kwargs!(py;
        node_kind = identity.getattr("node_kind")?,
        node_name = identity.getattr("node_name")?,
        binding_key = identity.getattr("binding_key")?,
        definition_fingerprint = identity.getattr("definition_fingerprint")?,
        execution_fingerprint = identity.getattr("execution_fingerprint")?,
    )?;
    if let Some(audit) = audit {
        kwargs.set_item("cadence_seconds", audit.getattr("cadence_seconds")?)?;
        kwargs.set_item("warmup_seconds", audit.getattr("warmup_seconds")?)?;
    }
    attr(py, "sqltide.adapter.models", "AdapterCurrentQualityNode")?.call((), Some(&kwargs))
}

/// Last-known outcome per audit and test from the observability tables.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, database, project_dir))]
pub(crate) fn build_checks_status_payload<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    database: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = analysis.py();
    let resolver: Bound<'py, PyDict> = model_resolver(analysis)?;
    let audits: Vec<Any<'py>> = analysis
        .getattr("compiled_project")?
        .getattr("audits")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let model_names: Bound<'py, PyTuple> = referenced_model_names(py, &audits)?;
    let anchors: Any<'py> = load_model_anchors(analysis, connection, database, model_names)?;
    let materialized: Any<'py> = load_materialized_model_names(connection, database, &resolver)?;
    let missing_by_audit: Bound<'py, PyDict> = PyDict::new(py);
    for audit in analysis
        .getattr("compiled_project")?
        .getattr("audits")?
        .try_iter()?
    {
        let audit: Any<'py> = audit?;
        let missing: Bound<'py, PyList> = PyList::empty(py);
        for model_name in audit.getattr("referenced_model_names")?.try_iter()? {
            let model_name: Any<'py> = model_name?;
            if !anchors.contains(&model_name)? && !materialized.contains(&model_name)? {
                missing.append(model_name)?;
            }
        }
        missing_by_audit.set_item(audit_name(&audit)?, missing)?;
    }
    let mut nodes: Vec<Any<'py>> = Vec::new();
    for audit in analysis
        .getattr("compiled_project")?
        .getattr("audits")?
        .try_iter()?
    {
        let audit: Any<'py> = audit?;
        nodes.push(quality_node(
            &require_quality_identity(&audit)?,
            Some(&audit),
        )?);
    }
    for test_case in analysis
        .getattr("compiled_project")?
        .getattr("test_cases")?
        .try_iter()?
    {
        nodes.push(quality_node(&require_quality_identity(&test_case?)?, None)?);
    }
    let table: Any<'py> = attr(
        py,
        "sqltide.adapter.constants",
        "METADATA_NODE_RESULTS_TABLE_NAME",
    )?;
    let missing_lookup: Any<'py> = missing_by_audit.as_any().getattr("get")?;
    let statuses: Bound<'py, PyList> = PyList::empty(py);
    if !connection
        .call_method(
            "metadata_columns",
            (),
            Some(&py_kwargs!(py; database = database, table = table)?),
        )?
        .is_truthy()?
    {
        for node in &nodes {
            let node_name: Any<'py> = node.getattr("node_name")?;
            let deferred: bool = missing_lookup.call1((&node_name,))?.is_truthy()?;
            let payload: Bound<'py, PyDict> = PyDict::new(py);
            payload.set_item("kind", node.getattr("node_kind")?)?;
            payload.set_item("name", &node_name)?;
            payload.set_item(
                "status",
                if deferred {
                    "deferred"
                } else {
                    NEVER_RUN_STATUS
                },
            )?;
            payload.set_item("driftReasons", PyList::empty(py))?;
            payload.set_item("severity", py.None())?;
            payload.set_item("failureCount", 0)?;
            payload.set_item("completedAt", py.None())?;
            payload.set_item("payload", py.None())?;
            payload.set_item("errorMessage", py.None())?;
            payload.set_item(
                "missingRelations",
                missing_lookup.call1((&node_name, PyList::empty(py)))?,
            )?;
            statuses.append(payload)?;
        }
        return Ok(statuses);
    }
    let query: Any<'py> = connection.call_method(
        "render_latest_node_status_query",
        (),
        Some(&py_kwargs!(py;
            database = database,
            project_identity = logical_project_identity(project_dir)?,
            target_identity = database,
            nodes = PyTuple::new(py, &nodes)?,
        )?),
    )?;
    for row in connection
        .call_method1("query", (query,))?
        .call_method0("named_rows")?
        .try_iter()?
    {
        let row: Any<'py> = attr(py, "builtins", "dict")?.call1((row?,))?;
        let payload: Bound<'py, PyDict> = status_row_payload(&row)?;
        let missing: Any<'py> =
            missing_lookup.call1((payload.as_any().get_item("name")?.str()?, PyList::empty(py)))?;
        if missing.is_truthy()? {
            payload.set_item("status", "deferred")?;
        }
        payload.set_item("missingRelations", missing)?;
        statuses.append(payload)?;
    }
    Ok(statuses)
}

fn status_row_payload<'py>(row: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = row.py();
    let kind = row.get_item("node_kind")?.str()?;
    let node_name = row.get_item("node_name")?.str()?;
    let status = row.get_item("current_status")?.str()?;
    let recorded: bool = PyAnyMethods::ne(status.as_any(), NEVER_RUN_STATUS)?;
    let raw_drift_reasons: Any<'py> = row.call_method1("get", ("drift_reasons",))?;
    let drift_reasons: Bound<'py, PyList> = PyList::empty(py);
    if raw_drift_reasons.is_instance_of::<PyList>() || raw_drift_reasons.is_instance_of::<PyTuple>()
    {
        for reason in raw_drift_reasons.try_iter()? {
            drift_reasons.append(reason?.str()?)?;
        }
    }
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("kind", kind)?;
    payload.set_item("name", node_name)?;
    payload.set_item("status", status)?;
    payload.set_item("driftReasons", drift_reasons)?;
    payload.set_item("severity", row.get_item("severity")?)?;
    payload.set_item(
        "failureCount",
        if recorded {
            attr(py, "builtins", "int")?.call1((row.get_item("failure_count")?.str()?,))?
        } else {
            0_i64.into_pyobject(py)?.into_any()
        },
    )?;
    payload.set_item(
        "completedAt",
        if recorded {
            row.get_item("completed_at")?.str()?.into_any()
        } else {
            py.None().into_bound(py)
        },
    )?;
    payload.set_item(
        "payload",
        if recorded {
            parsed_payload(&row.get_item("payload_json")?)?
        } else {
            py.None().into_bound(py)
        },
    )?;
    payload.set_item("errorMessage", row.get_item("error_message")?)?;
    Ok(payload)
}

/// `json.loads(str(payload_json))` when it is a JSON object, otherwise `None`.
fn parsed_payload<'py>(payload_json: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = payload_json.py();
    let parsed: PyResult<Any<'py>> = payload_json
        .str()
        .and_then(|text| attr(py, "json", "loads")?.call1((text,)));
    match parsed {
        Ok(parsed) if parsed.is_instance_of::<PyDict>() => Ok(parsed),
        Ok(_) => Ok(py.None().into_bound(py)),
        Err(error)
            if error.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
                || error.is_instance_of::<pyo3::exceptions::PyValueError>(py) =>
        {
            Ok(py.None().into_bound(py))
        }
        Err(error) => Err(error),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_one_audit, module)?)?;
    module.add_function(wrap_pyfunction!(run_audit_batch, module)?)?;
    module.add_function(wrap_pyfunction!(run_one_test, module)?)?;
    module.add_function(wrap_pyfunction!(build_checks_status_payload, module)?)
}
