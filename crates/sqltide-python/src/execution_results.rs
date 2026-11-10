//! Build results decoded from authoritative workflow evidence: the direct build and audit
//! result, the virtual (backfill) execution result and the per-root backfill reports. Nothing
//! here reads the warehouse; every value comes from the request and the executed statements.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString, PyTuple};
use sqltide_compile::relations::RAW_TABLE_NAME_PREFIX;
use sqltide_executor::workflow::step_segment;

use crate::planner_support::{
    build_inspected_managed_table_state_from_catalog, inspect_root_deployment_state,
};

const DIRECT_MODELS: &str = "sqltide.executor.direct.models";
const AUDITING_MODELS: &str = "sqltide.executor.auditing.models";
const BACKFILL_MODELS: &str = "sqltide.executor.backfill.models";
const BACKFILL_EXCEPTIONS: &str = "sqltide.executor.backfill.exceptions";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";

type Value<'py> = Bound<'py, PyAny>;

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    keywords: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in keywords {
        arguments.set_item(keyword, value)?;
    }
    class(py, module, name)?.call((), Some(&arguments))
}

fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn text<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

fn boolean(py: Python<'_>, value: bool) -> PyResult<Value<'_>> {
    Ok(value.into_pyobject(py)?.to_owned().into_any())
}

/// `str(value)`.
fn string(value: &Value<'_>) -> PyResult<String> {
    value.str()?.extract()
}

/// `results[step_id]`, raising `KeyError(step_id)` like a dict lookup.
fn result_for<'py>(results: &Bound<'py, PyDict>, step_id: &str) -> PyResult<Value<'py>> {
    results.as_any().get_item(step_id)
}

fn backfill_error(py: Python<'_>, message: String) -> PyErr {
    match class(py, BACKFILL_EXCEPTIONS, "BackfillExecutionError")
        .and_then(|error| error.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// `{result.step_id: result for result in execution.statement_results}`.
fn results_by_step_id<'py>(
    py: Python<'py>,
    execution: &Value<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let results: Bound<'py, PyDict> = PyDict::new(py);
    for result in items(&execution.getattr("statement_results")?)? {
        results.set_item(result.getattr("step_id")?, &result)?;
    }
    Ok(results)
}

fn direct_boundaries<'py>(py: Python<'py>, captures: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    let mut boundaries: Vec<Value<'py>> = Vec::new();
    for capture in items(captures)? {
        let mode: Value<'py> = capture.getattr("boundary_mode")?;
        for replay_range in items(&capture.getattr("ranges")?)? {
            let partition: Value<'py> = replay_range.getattr("partition_value")?;
            let boundary_key: String = if partition.is_none() {
                format!("_replay_{}", string(&mode)?)
            } else {
                format!("_replay_partition={}", string(&partition)?)
            };
            boundaries.push(build(
                py,
                DIRECT_MODELS,
                "DirectReplayBoundary",
                &[
                    ("model_name", capture.getattr("logical_model_name")?),
                    (
                        "driving_input_relation_name",
                        capture.getattr("driving_input_relation_name")?,
                    ),
                    ("replay_boundary_mode", mode.clone()),
                    ("boundary_key", text(py, &boundary_key)),
                    ("cutoff_value", replay_range.getattr("replay_cutoff_value")?),
                    (
                        "cutoff_inclusive",
                        replay_range.getattr("cutoff_inclusive")?,
                    ),
                ],
            )?);
        }
    }
    Ok(boundaries)
}

fn direct_replay_results<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    execution: &Value<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let model_name_by_segment: Bound<'py, PyDict> = PyDict::new(py);
    for entry in items(&request.getattr("plan")?.getattr("entries")?)? {
        let name: Value<'py> = entry.getattr("model_key")?.getattr("name")?;
        model_name_by_segment.set_item(step_segment(&name.extract::<String>()?), &name)?;
    }
    let mut results: Vec<Value<'py>> = Vec::new();
    for result in items(&execution.getattr("statement_results")?)? {
        let step_id: String = result.getattr("step_id")?.extract()?;
        let mutation: Value<'py> = result.getattr("mutation_result")?;
        let Some(segment) = step_id.strip_prefix("replay_") else {
            continue;
        };
        if mutation.is_none() {
            continue;
        }
        results.push(build(
            py,
            DIRECT_MODELS,
            "DirectRootReplayResult",
            &[
                (
                    "model_name",
                    model_name_by_segment.as_any().get_item(segment)?,
                ),
                ("written_rows", mutation.getattr("written_rows")?),
            ],
        )?);
    }
    Ok(results)
}

/// The failing row count a count query returned: `int(str(rows[0][0]))`, or 0 without rows.
fn audit_count<'py>(py: Python<'py>, result: &Value<'py>) -> PyResult<Value<'py>> {
    let query: Value<'py> = result.getattr("query_result")?;
    if query.is_none() || !query.getattr("rows")?.is_truthy()? {
        return Ok(PyInt::new(py, 0).into_any());
    }
    let first: Value<'py> = query.getattr("rows")?.get_item(0)?.get_item(0)?;
    py.import("builtins")?
        .getattr("int")?
        .call1((first.str()?,))
}

fn direct_audit_result<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    results: &Bound<'py, PyDict>,
) -> PyResult<Value<'py>> {
    let path: Value<'py> = py.import("pathlib")?.getattr("Path")?;
    let mut audits: Vec<Value<'py>> = Vec::new();
    for (position, audit) in items(&request.getattr("audits")?)?.iter().enumerate() {
        let name: Value<'py> = audit.getattr("name")?;
        let prefix: String = format!(
            "audit_{}_{}",
            position + 1,
            step_segment(&name.extract::<String>()?)
        );
        let count_result: Value<'py> = result_for(results, &format!("{prefix}_count"))?;
        let sample_result: Value<'py> = result_for(results, &format!("{prefix}_sample"))?;
        let count_error: Value<'py> = count_result.getattr("error_message")?;
        let error_message: Value<'py> = if count_error.is_truthy()? {
            count_error.clone()
        } else {
            sample_result.getattr("error_message")?
        };
        let count: Value<'py> = if count_error.is_none() {
            audit_count(py, &count_result)?
        } else {
            PyInt::new(py, 1).into_any()
        };
        let sample: Value<'py> = sample_result.getattr("query_result")?;
        let (column_names, rows): (Value<'py>, Value<'py>) = if sample.is_none() {
            (tuple(py, Vec::new())?, tuple(py, Vec::new())?)
        } else {
            (sample.getattr("column_names")?, sample.getattr("rows")?)
        };
        let passed: bool = error_message.is_none() && count.eq(0)?;
        audits.push(build(
            py,
            AUDITING_MODELS,
            "SqlAuditResult",
            &[
                ("file_path", path.call1((&name,))?),
                ("referenced_model_names", tuple(py, Vec::new())?),
                ("severity", audit.getattr("severity")?),
                ("passed", boolean(py, passed)?),
                ("failing_row_count", count),
                ("sample_column_names", column_names),
                ("sample_rows", rows),
                ("description", audit.getattr("description")?),
                ("name", name),
                ("error_message", error_message),
            ],
        )?);
    }
    build(
        py,
        AUDITING_MODELS,
        "SqlAuditRunResult",
        &[("audit_results", tuple(py, audits)?)],
    )
}

/// The relation names of every managed source resource, in project order.
fn managed_source_relation_names(request: &Value<'_>) -> PyResult<Vec<String>> {
    let realized: Value<'_> = request.getattr("realized_project")?;
    let resources_by_key: Value<'_> = realized.getattr("resources_by_logical_key")?;
    let mut names: Vec<String> = Vec::new();
    for source in items(&realized.getattr("project")?.getattr("sources")?)? {
        for resource in items(&resources_by_key.get_item(source.getattr("key")?)?)? {
            if resource.hasattr("name")? {
                names.push(string(&resource.getattr("name")?)?);
            }
        }
    }
    Ok(names)
}

fn created_source_names(request: &Value<'_>, results: &Bound<'_, PyDict>) -> PyResult<Vec<String>> {
    let mut created: Vec<String> = Vec::new();
    for name in managed_source_relation_names(request)? {
        let segment: String = step_segment(&name);
        let mut ran: bool = false;
        for prefix in ["prepare_source_", "swap_source_", "activate_source_"] {
            if results.contains(format!("{prefix}{segment}"))? {
                ran = true;
                break;
            }
        }
        if ran {
            created.push(name);
        }
    }
    Ok(created)
}

fn string_tuple<'py>(py: Python<'py>, values: Vec<String>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn relation_names<'py>(py: Python<'py>, operations: &Value<'py>) -> PyResult<Value<'py>> {
    tuple(
        py,
        items(operations)?
            .iter()
            .map(|operation| operation.getattr("relation_name"))
            .collect::<PyResult<_>>()?,
    )
}

fn direct_boundary_time<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    boundaries: &[Value<'py>],
) -> PyResult<Value<'py>> {
    let requested: Value<'py> = request.getattr("boundary_time")?;
    if !requested.is_none() {
        return Ok(requested);
    }
    if let Some(scalar) = boundaries.last() {
        let offsets: Value<'py> =
            class(py, ADAPTER_TYPES, "AdapterReplayBoundaryMode")?.getattr("OFFSETS")?;
        if scalar.getattr("replay_boundary_mode")?.ne(offsets)? {
            return scalar.getattr("cutoff_value");
        }
    }
    Ok(text(py, "warehouse"))
}

/// Direct lifecycle evidence decoded without reading target artifacts or warehouse state.
#[pyfunction]
#[pyo3(signature = (*, request, execution, captures))]
fn build_direct_execution_result<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    execution: &Value<'py>,
    captures: &Value<'py>,
) -> PyResult<Value<'py>> {
    let results: Bound<'py, PyDict> = results_by_step_id(py, execution)?;
    let boundaries: Vec<Value<'py>> = direct_boundaries(py, captures)?;
    let created: Vec<String> = created_source_names(request, &results)?;
    let preserved: Vec<String> = managed_source_relation_names(request)?
        .into_iter()
        .filter(|name| !created.contains(name))
        .collect();
    let plan: Value<'py> = request.getattr("plan")?;
    let dropped: Value<'py> = relation_names(py, &plan.getattr("teardown_operations")?)?;
    let created_relations: Value<'py> = relation_names(py, &plan.getattr("creation_operations")?)?;
    let boundary_time: Value<'py> = direct_boundary_time(py, request, &boundaries)?;
    let replay_results: Value<'py> = tuple(py, direct_replay_results(py, request, execution)?)?;
    let build_result: Value<'py> = build(
        py,
        DIRECT_MODELS,
        "DirectBuildResult",
        &[
            ("database", request.getattr("database")?),
            (
                "preserved_source_relation_names",
                string_tuple(py, preserved)?,
            ),
            ("created_source_relation_names", string_tuple(py, created)?),
            ("dropped_relation_names", dropped),
            ("created_relation_names", created_relations),
            ("boundary_time", boundary_time),
            ("boundaries", tuple(py, boundaries)?),
            ("replay_results", replay_results),
            (
                "effective_start_time",
                request.getattr("effective_start_time")?,
            ),
        ],
    )?;
    let audit_result: Value<'py> = direct_audit_result(py, request, &results)?;
    build(
        py,
        DIRECT_MODELS,
        "DirectBuildExecutionResult",
        &[
            ("build_result", build_result),
            ("audit_result", audit_result),
        ],
    )
}

/// The written rows of one replay root's seed and replay statements, `None` when any of them
/// reported no count.
fn root_written_rows<'py>(
    py: Python<'py>,
    execution: &Value<'py>,
    root_key: &Value<'py>,
) -> PyResult<Value<'py>> {
    let name: String = root_key.getattr("name")?.extract()?;
    let segment: String = step_segment(&name);
    let steps: [String; 2] = [format!("seed_{segment}"), format!("replay_{segment}")];
    let mut results: Vec<Value<'py>> = Vec::new();
    for result in items(&execution.getattr("statement_results")?)? {
        let step_id: String = result.getattr("step_id")?.extract()?;
        if steps.contains(&step_id) {
            results.push(result);
        }
    }
    if results.is_empty() {
        return Err(backfill_error(
            py,
            format!("Virtual workflow did not execute replay root '{name}'"),
        ));
    }
    let mut total: Value<'py> = PyInt::new(py, 0).into_any();
    for result in &results {
        let mutation: Value<'py> = result.getattr("mutation_result")?;
        if mutation.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let written: Value<'py> = mutation.getattr("written_rows")?;
        if written.is_none() {
            return Ok(py.None().into_bound(py));
        }
        total = total.add(written)?;
    }
    Ok(total)
}

fn virtual_boundary_time<'py>(py: Python<'py>, execution: &Value<'py>) -> PyResult<Value<'py>> {
    let mut found: Option<Value<'py>> = None;
    for result in items(&execution.getattr("statement_results")?)? {
        if result.getattr("step_id")?.eq("read_boundary_time")? {
            found = Some(result);
            break;
        }
    }
    let Some(result) = found else {
        return Err(backfill_error(
            py,
            "Virtual workflow omitted required result 'read_boundary_time'".to_owned(),
        ));
    };
    let query: Value<'py> = result.getattr("query_result")?;
    let missing = || {
        backfill_error(
            py,
            "Virtual workflow returned no captured boundary time".to_owned(),
        )
    };
    if query.is_none() || !query.getattr("rows")?.is_truthy()? {
        return Err(missing());
    }
    let boundary: Bound<'py, PyString> = query.getattr("rows")?.get_item(0)?.get_item(0)?.str()?;
    if boundary.to_str()?.is_empty() {
        return Err(missing());
    }
    Ok(boundary.into_any())
}

/// Virtual build output decoded from exact workflow execution evidence.
#[pyfunction]
#[pyo3(signature = (*, request, root_reports, existing_relation_names, execution))]
fn build_virtual_execution_result<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    root_reports: &Value<'py>,
    existing_relation_names: &Value<'py>,
    execution: &Value<'py>,
) -> PyResult<Value<'py>> {
    let deployment_plan: Value<'py> = request.getattr("confirmed_plan")?;
    if deployment_plan.is_none() {
        return Err(backfill_error(
            py,
            "Virtual execution result requires a confirmed plan".to_owned(),
        ));
    }
    let deployment_id: Value<'py> = request.getattr("deployment_id")?;
    if deployment_id.is_none() {
        return Err(backfill_error(
            py,
            "Virtual execution result requires a confirmed deployment".to_owned(),
        ));
    }
    let created_at: Value<'py> = request.getattr("created_at")?;
    if created_at.is_none() {
        return Err(backfill_error(
            py,
            "Virtual execution result requires a creation timestamp".to_owned(),
        ));
    }
    let boundary_time: Value<'py> = virtual_boundary_time(py, execution)?;
    let mut replay_results: Vec<Value<'py>> = Vec::new();
    for subtree in items(&deployment_plan.getattr("rebuild_subtrees")?)? {
        if !subtree.getattr("replay_required")?.is_truthy()? {
            continue;
        }
        let root_key: Value<'py> = subtree.getattr("root_key")?;
        let written_rows: Value<'py> = root_written_rows(py, execution, &root_key)?;
        replay_results.push(build(
            py,
            BACKFILL_MODELS,
            "BackfillRootReplayResult",
            &[("root_key", root_key), ("written_rows", written_rows)],
        )?);
    }
    let bootstrap: Value<'py> = build(
        py,
        BACKFILL_MODELS,
        "BackfillBootstrapResult",
        &[
            ("deployment_id", deployment_id),
            ("created_at", created_at),
            ("deployment_plan", deployment_plan),
            ("root_reports", root_reports.clone()),
            ("existing_relation_names", existing_relation_names.clone()),
        ],
    )?;
    build(
        py,
        BACKFILL_MODELS,
        "BackfillExecutionResult",
        &[
            ("bootstrap", bootstrap),
            ("boundary_time", boundary_time),
            ("replay_results", tuple(py, replay_results)?),
        ],
    )
}

/// User-facing rebuild strategy reports for the managed roots of one desired state.
#[pyfunction]
#[pyo3(signature = (*, catalog, desired_state))]
fn build_root_backfill_reports<'py>(
    py: Python<'py>,
    catalog: &Value<'py>,
    desired_state: &Value<'py>,
) -> PyResult<Value<'py>> {
    let inspected: Value<'py> = build_inspected_managed_table_state_from_catalog(py, catalog)?;
    let state_kind: Value<'py> = class(py, PLANNER_TYPES, "RootDeploymentStateKind")?;
    let strategy_by_state: Bound<'py, PyDict> = PyDict::new(py);
    strategy_by_state.set_item(state_kind.getattr("ACTIVE_VIEW_PRESENT")?, "bounded_replay")?;
    strategy_by_state.set_item(state_kind.getattr("GREENFIELD")?, "create_from_scratch")?;
    strategy_by_state.set_item(
        state_kind.getattr("LOGICAL_VIEW_MISSING")?,
        "full_rebuild_required",
    )?;
    let table: Value<'py> = class(py, COMPILE_MODELS, "DesiredTable")?;
    let mut root_keys: Vec<Value<'py>> = Vec::new();
    for object in items(&desired_state.getattr("objects")?)? {
        if object.is_instance(&table)?
            && !object
                .getattr("name")?
                .extract::<String>()?
                .starts_with(RAW_TABLE_NAME_PREFIX)
        {
            root_keys.push(object.getattr("key")?);
        }
    }
    let mut reports: Vec<Value<'py>> = Vec::new();
    for root_key in root_keys {
        let inspection: Value<'py> = inspect_root_deployment_state(py, &inspected, &root_key)?;
        let kind: Value<'py> = inspection.getattr("state_kind")?;
        let strategy: Value<'py> = match strategy_by_state.get_item(&kind)? {
            Some(strategy) => strategy,
            None => return Err(PyKeyError::new_err((kind.unbind(),))),
        };
        reports.push(build(
            py,
            BACKFILL_MODELS,
            "RootBackfillReport",
            &[
                ("root_key", inspection.getattr("root_key")?),
                ("state_kind", kind),
                ("replay_strategy", strategy),
                (
                    "active_deployment_id",
                    inspection.getattr("active_deployment_id")?,
                ),
            ],
        )?);
    }
    tuple(py, reports)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_direct_execution_result, module)?)?;
    module.add_function(wrap_pyfunction!(build_virtual_execution_result, module)?)?;
    module.add_function(wrap_pyfunction!(build_root_backfill_reports, module)?)
}
