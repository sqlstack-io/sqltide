//! Build command support: direct build result and execution evidence rendering, post-build
//! audit selection, protected-pipeline confirmation and the committed pipeline limit. Terminal
//! I/O goes through Python's `print`, `input` and `sys.stderr` so redirection and tests see it;
//! styling comes from the Python CLI style facade.

use std::collections::BTreeSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyString};

use crate::artifacts::json_dumps;
use crate::direct_build::{Value, build, class, is_a, items, method, none, string, text, tuple};

const NATIVE: &str = "sqltide._native";
const CLI_STYLE: &str = "sqltide._native";
const BUILD_MODELS: &str = "sqltide.cli.build.models";
const BUILD_EXCEPTIONS: &str = "sqltide.cli.build.exceptions";
const ENTRY_CONSTANTS: &str = "sqltide.cli.entry.constants";
const DIRECT_MODELS: &str = "sqltide.executor.direct.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const DIRECT_MODE_LABEL: &str = "direct";
const NO_LINES: &str = "  - none";
const NO_REPLAYS: &str = "- none";

fn style<'py>(py: Python<'py>) -> PyResult<Value<'py>> {
    py.import(CLI_STYLE)?.getattr("cli_style")?.call0()
}

fn styled(style: &Value<'_>, name: &str, value: &str) -> PyResult<String> {
    style.call_method1(name, (value,))?.extract()
}

fn label_value(style: &Value<'_>, label: &str, value: &Value<'_>) -> PyResult<String> {
    method(
        style,
        "label_value",
        &[("label", text(style.py(), label)), ("value", value.clone())],
    )?
    .extract()
}

fn or_none(lines: Vec<String>) -> Vec<String> {
    if lines.is_empty() {
        vec![NO_LINES.to_owned()]
    } else {
        lines
    }
}

fn dict<'py>(py: Python<'py>, pairs: Vec<(&str, Value<'py>)>) -> PyResult<Value<'py>> {
    let dict: Bound<'py, PyDict> = PyDict::new(py);
    for (key, value) in pairs {
        dict.set_item(key, value)?;
    }
    Ok(dict.into_any())
}

fn list<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyList::new(py, values)?.into_any())
}

fn listed<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Value<'py>> {
    list(py, items(value)?)
}

fn str_of<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(value.str()?.into_any())
}

fn audit_label<'py>(audit: &Value<'py>) -> PyResult<Value<'py>> {
    let name: Value<'py> = audit.getattr("name")?;
    if name.is_truthy()? {
        return Ok(name);
    }
    audit.getattr("file_path")?.getattr("name")
}

fn audit_payload<'py>(py: Python<'py>, audit_result: &Value<'py>) -> PyResult<Value<'py>> {
    let mut audits: Vec<Value<'py>> = Vec::new();
    for audit in items(&audit_result.getattr("audit_results")?)? {
        audits.push(dict(
            py,
            vec![
                ("name", audit_label(&audit)?),
                ("passed", audit.getattr("passed")?),
                ("failing_row_count", audit.getattr("failing_row_count")?),
                ("severity", str_of(&audit.getattr("severity")?)?),
            ],
        )?);
    }
    list(py, audits)
}

/// One completed direct build as deterministic JSON.
#[pyfunction]
#[pyo3(signature = (*, result, adapter_name, audit_result))]
fn render_direct_build_json<'py>(
    py: Python<'py>,
    result: &Value<'py>,
    adapter_name: &Value<'py>,
    audit_result: &Value<'py>,
) -> PyResult<String> {
    let mut boundaries: Vec<Value<'py>> = Vec::new();
    for boundary in items(&result.getattr("boundaries")?)? {
        boundaries.push(dict(
            py,
            vec![
                ("model", boundary.getattr("model_name")?),
                (
                    "driving_input_relation",
                    boundary.getattr("driving_input_relation_name")?,
                ),
                (
                    "replay_boundary_mode",
                    str_of(&boundary.getattr("replay_boundary_mode")?)?,
                ),
                ("boundary_key", boundary.getattr("boundary_key")?),
                ("cutoff_value", boundary.getattr("cutoff_value")?),
                ("cutoff_inclusive", boundary.getattr("cutoff_inclusive")?),
            ],
        )?);
    }
    let mut replays: Vec<Value<'py>> = Vec::new();
    for replay in items(&result.getattr("replay_results")?)? {
        replays.push(dict(
            py,
            vec![
                ("model", replay.getattr("model_name")?),
                ("warehouse_written_rows", replay.getattr("written_rows")?),
            ],
        )?);
    }
    let payload: Value<'py> = dict(
        py,
        vec![
            ("mode", text(py, DIRECT_MODE_LABEL)),
            ("adapter", adapter_name.clone()),
            ("database", result.getattr("database")?),
            ("start_time", result.getattr("effective_start_time")?),
            (
                "preserved_sources",
                listed(py, &result.getattr("preserved_source_relation_names")?)?,
            ),
            (
                "created_sources",
                listed(py, &result.getattr("created_source_relation_names")?)?,
            ),
            (
                "dropped",
                listed(py, &result.getattr("dropped_relation_names")?)?,
            ),
            (
                "created",
                listed(py, &result.getattr("created_relation_names")?)?,
            ),
            ("boundary_time", result.getattr("boundary_time")?),
            ("boundaries", list(py, boundaries)?),
            ("replays", list(py, replays)?),
            ("audits", audit_payload(py, audit_result)?),
        ],
    )?;
    json_dumps(&payload, Some(2), false)
}

/// One completed direct build as operator-facing text.
#[pyfunction]
#[pyo3(signature = (*, result, adapter_name, audit_result))]
fn render_direct_build_text<'py>(
    py: Python<'py>,
    result: &Value<'py>,
    adapter_name: &Value<'py>,
    audit_result: &Value<'py>,
) -> PyResult<String> {
    let style: Value<'py> = style(py)?;
    let mut lines: Vec<String> = vec![
        styled(&style, "title", "Direct Build Complete")?,
        label_value(&style, "Adapter", adapter_name)?,
        label_value(&style, "Mode", &text(py, DIRECT_MODE_LABEL))?,
        label_value(&style, "Database", &result.getattr("database")?)?,
    ];
    let start_time: Value<'py> = result.getattr("effective_start_time")?;
    if !start_time.is_none() {
        lines.push(label_value(&style, "Effective start time", &start_time)?);
    }
    let preserved: usize = result.getattr("preserved_source_relation_names")?.len()?;
    let replays: Vec<Value<'py>> = items(&result.getattr("replay_results")?)?;
    lines.push(label_value(
        &style,
        "Preserved sources",
        &text(py, &preserved.to_string()),
    )?);
    lines.push(label_value(
        &style,
        "Replay roots executed",
        &text(py, &replays.len().to_string()),
    )?);
    lines.push(String::new());
    lines.push(styled(&style, "section", "Relations")?);
    let mut dropped: Vec<String> = Vec::new();
    for name in items(&result.getattr("dropped_relation_names")?)? {
        dropped.push(format!("  dropped  {}", name.str()?));
    }
    lines.extend(or_none(dropped));
    for name in items(&result.getattr("created_relation_names")?)? {
        lines.push(format!("  created  {}", name.str()?));
    }
    lines.push(String::new());
    lines.push(styled(&style, "section", "Replay boundaries")?);
    lines.push(label_value(
        &style,
        "Boundary time",
        &result.getattr("boundary_time")?,
    )?);
    let mut boundaries: Vec<String> = Vec::new();
    for boundary in items(&result.getattr("boundaries")?)? {
        let edge: &str = if boundary.getattr("cutoff_inclusive")?.is_truthy()? {
            "<="
        } else {
            "<"
        };
        boundaries.push(format!(
            "  {} replays {} where {} {edge} {}",
            boundary.getattr("model_name")?.str()?,
            boundary.getattr("driving_input_relation_name")?.str()?,
            boundary.getattr("boundary_key")?.str()?,
            boundary.getattr("cutoff_value")?.str()?,
        ));
    }
    lines.extend(or_none(boundaries));
    lines.push(String::new());
    lines.push(styled(&style, "section", "Replay execution")?);
    let mut executed: Vec<String> = Vec::new();
    for replay in &replays {
        let written: Value<'py> = replay.getattr("written_rows")?;
        let written: String = if written.is_none() {
            "unavailable".to_owned()
        } else {
            written.str()?.extract()?
        };
        executed.push(format!(
            "  {}  warehouse-written rows: {written}",
            replay.getattr("model_name")?.str()?
        ));
    }
    lines.extend(or_none(executed));
    lines.push(String::new());
    lines.push(styled(&style, "section", "Audits")?);
    let mut audits: Vec<String> = Vec::new();
    for audit in items(&audit_result.getattr("audit_results")?)? {
        // `_AUDIT_STATUS_BY_OUTCOME[audit.passed]`: a dict lookup keyed by `True` and `False`.
        let passed: Value<'py> = audit.getattr("passed")?;
        let status: &str = if passed.eq(true)? {
            "pass"
        } else if passed.eq(false)? {
            "FAIL"
        } else {
            return Err(pyo3::exceptions::PyKeyError::new_err(passed.unbind()));
        };
        audits.push(format!(
            "  {status}  {}  ({} failing rows)",
            audit_label(&audit)?.str()?,
            audit.getattr("failing_row_count")?.str()?
        ));
    }
    lines.extend(or_none(audits));
    Ok(lines.join("\n"))
}

/// One readable graceful-cancellation message after terminal Ctrl-C output.
#[pyfunction]
fn render_interrupted_build_message(py: Python<'_>) -> PyResult<String> {
    Ok(format!(
        "\n{}  Build interrupted and recorded as cancelled.",
        styled(&style(py)?, "warning", "Cancelled")?
    ))
}

/// Bounded direct runtime evidence as JSON, without creating recovery state.
#[pyfunction]
#[pyo3(signature = (*, request, status, captures, execution, failed_step_id, error_message, audit_result=None))]
#[allow(clippy::too_many_arguments)]
fn render_direct_execution_json<'py>(
    py: Python<'py>,
    request: &Value<'py>,
    status: &Value<'py>,
    captures: &Value<'py>,
    execution: &Value<'py>,
    failed_step_id: &Value<'py>,
    error_message: &Value<'py>,
    audit_result: Option<&Value<'py>>,
) -> PyResult<String> {
    let mut captured: Vec<Value<'py>> = Vec::new();
    for capture in items(captures)? {
        let mut ranges: Vec<Value<'py>> = Vec::new();
        for range in items(&capture.getattr("ranges")?)? {
            ranges.push(dict(
                py,
                vec![
                    ("partition", range.getattr("partition_value")?),
                    ("lower", range.getattr("lower_value")?),
                    ("upper", range.getattr("upper_value")?),
                    ("cutoff", range.getattr("replay_cutoff_value")?),
                    ("cutoff_inclusive", range.getattr("cutoff_inclusive")?),
                ],
            )?);
        }
        captured.push(dict(
            py,
            vec![
                ("capture_id", capture.getattr("capture_id")?),
                ("model", capture.getattr("logical_model_name")?),
                (
                    "driving_input",
                    capture.getattr("driving_input_relation_name")?,
                ),
                ("boundary_mode", str_of(&capture.getattr("boundary_mode")?)?),
                ("captured_at", capture.getattr("captured_at")?),
                ("ranges", list(py, ranges)?),
            ],
        )?);
    }
    let results: Vec<Value<'py>> = items(&execution.getattr("statement_results")?)?;
    let mut completed: Vec<Value<'py>> = Vec::new();
    let mut outcomes: Vec<Value<'py>> = Vec::new();
    for result in &results {
        completed.push(result.getattr("step_id")?);
    }
    for result in &results {
        let mutation: Value<'py> = result.getattr("mutation_result")?;
        let query: Value<'py> = result.getattr("query_result")?;
        outcomes.push(dict(
            py,
            vec![
                ("step_id", result.getattr("step_id")?),
                ("error_message", result.getattr("error_message")?),
                (
                    "written_rows",
                    if mutation.is_none() {
                        none(py)
                    } else {
                        mutation.getattr("written_rows")?
                    },
                ),
                (
                    "returned_rows",
                    if query.is_none() {
                        none(py)
                    } else {
                        query.getattr("rows")?.len()?.into_pyobject(py)?.into_any()
                    },
                ),
            ],
        )?);
    }
    let audit_count = |name: &str| -> PyResult<Value<'py>> {
        match audit_result {
            Some(audit_result) if !audit_result.is_none() => audit_result.getattr(name),
            _ => Ok(none(py)),
        }
    };
    let payload: Value<'py> = dict(
        py,
        vec![
            ("status", status.clone()),
            ("workflow_id", request.getattr("workflow_id")?),
            ("database", request.getattr("database")?),
            ("captured_roots", list(py, captured)?),
            ("completed_steps", list(py, completed)?),
            ("failed_step", failed_step_id.clone()),
            ("error_message", error_message.clone()),
            (
                "audit_error_failure_count",
                audit_count("error_failure_count")?,
            ),
            (
                "audit_warning_failure_count",
                audit_count("warning_failure_count")?,
            ),
            ("statement_outcomes", list(py, outcomes)?),
        ],
    )?;
    Ok(json_dumps(&payload, Some(2), false)? + "\n")
}

/// Audits whose complete managed-model reference set was rebuilt, or every audit on a full
/// build.
#[pyfunction]
#[pyo3(signature = (*, audits, execution_model_names, full_build))]
fn select_direct_build_audits<'py>(
    py: Python<'py>,
    audits: &Value<'py>,
    execution_model_names: &Value<'py>,
    full_build: bool,
) -> PyResult<Value<'py>> {
    if full_build {
        return Ok(audits.clone());
    }
    let mut selected: Vec<Value<'py>> = Vec::new();
    for audit in items(audits)? {
        let referenced: Bound<'py, PyFrozenSet> =
            PyFrozenSet::new(py, items(&audit.getattr("referenced_model_names")?)?)?;
        if referenced.le(execution_model_names)? {
            selected.push(audit);
        }
    }
    tuple(py, selected)
}

/// Selected direct audits with model references rewritten to their live relations, resolved
/// before authoritative workflow construction.
#[pyfunction]
#[pyo3(signature = (*, preview, adapter_profile))]
fn prepare_direct_build_audits<'py>(
    py: Python<'py>,
    preview: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    let plan: Value<'py> = preview.getattr("plan")?;
    let modes: Value<'py> = class(py, PLANNER_TYPES, "DirectSelectionMode")?;
    let selection_mode: Value<'py> = plan.getattr("selection_mode")?;
    if selection_mode.eq(modes.getattr("CHANGED")?)?
        && !plan.getattr("execution_scope")?.is_truthy()?
    {
        return tuple(py, Vec::new());
    }
    let analysis: Value<'py> = preview.getattr("analysis")?;
    let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
    let mut names: Vec<Value<'py>> = Vec::new();
    for key in items(&plan.getattr("execution_scope")?)? {
        names.push(key.getattr("name")?);
    }
    let selected: Value<'py> = select_direct_build_audits(
        py,
        &compiled_project.getattr("audits")?,
        &PyFrozenSet::new(py, names)?.into_any(),
        selection_mode.eq(modes.getattr("ALL_MODELS")?)?,
    )?;
    let mut audits: Vec<Value<'py>> = Vec::new();
    for audit in items(&selected)? {
        if audit.getattr("warmup_seconds")?.eq(0)? {
            audits.push(audit);
        }
    }
    let database: Value<'py> = preview.getattr("database")?;
    let relation_names: Value<'py> = analysis
        .getattr("realized_project")?
        .getattr("relation_name_by_logical_key")?;
    let resolver: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(&compiled_project.getattr("models")?)? {
        let key: Value<'py> = model.getattr("key")?;
        let relation: Value<'py> = relation_names.get_item(&key)?;
        resolver.set_item(
            key.getattr("name")?,
            PyString::new(py, &format!("{}.{}", database.str()?, relation.str()?)),
        )?;
    }
    let native: Bound<'py, PyModule> = py.import(NATIVE)?;
    let rewriter: Value<'py> = native.getattr("SqlReferenceRewriter")?.call(
        (),
        Some(&{
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("dialect", adapter_profile.getattr("sql_analysis_dialect")?)?;
            arguments
        }),
    )?;
    let replace_refs: Value<'py> = native.getattr("replace_refs")?;
    let mut prepared: Vec<Value<'py>> = Vec::new();
    for audit in audits {
        let arguments: Bound<'py, PyDict> = PyDict::new(py);
        arguments.set_item("sql", audit.getattr("query")?)?;
        arguments.set_item("resolver", &resolver)?;
        arguments.set_item("rewriter", &rewriter)?;
        let query: Value<'py> = replace_refs.call((), Some(&arguments))?;
        prepared.push(build(
            py,
            DIRECT_MODELS,
            "DirectBuildAudit",
            &[
                ("name", audit_label(&audit)?),
                ("query", query),
                ("severity", audit.getattr("severity")?),
                ("description", audit.getattr("description")?),
            ],
        )?);
    }
    tuple(py, prepared)
}

/// Protected pipelines containing at least one executing model.
#[pyfunction]
#[pyo3(signature = (*, compiled_project, execution_model_keys))]
pub(crate) fn build_protection_requirements<'py>(
    py: Python<'py>,
    compiled_project: &Value<'py>,
    execution_model_keys: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut requirements: Vec<Value<'py>> = Vec::new();
    for compiled_pipeline in items(&compiled_project.getattr("pipelines")?)? {
        let pipeline: Value<'py> = compiled_pipeline.getattr("pipeline")?;
        let protection: Value<'py> = pipeline.getattr("protection")?;
        if protection.is_none() {
            continue;
        }
        let mut executing: bool = false;
        for model in items(&compiled_pipeline.getattr("models")?)? {
            if execution_model_keys.contains(model.getattr("key")?)? {
                executing = true;
                break;
            }
        }
        if !executing {
            continue;
        }
        requirements.push(build(
            py,
            BUILD_MODELS,
            "BuildProtectionRequirement",
            &[
                ("pipeline_name", pipeline.getattr("name")?),
                ("warning", protection.getattr("warning")?),
                ("confirmation", protection.getattr("confirmation")?),
            ],
        )?);
    }
    tuple(py, requirements)
}

/// `print(message, file=sys.stderr)`, reading `sys.stderr` when called.
fn print_error(py: Python<'_>, message: &str) -> PyResult<()> {
    let arguments: Bound<'_, PyDict> = PyDict::new(py);
    arguments.set_item("file", py.import("sys")?.getattr("stderr")?)?;
    py.import("builtins")?
        .getattr("print")?
        .call((message,), Some(&arguments))?;
    Ok(())
}

/// `input(prompt).strip()`.
fn answer<'py>(py: Python<'py>, prompt: &str) -> PyResult<Value<'py>> {
    py.import("builtins")?
        .getattr("input")?
        .call1((prompt,))?
        .call_method0("strip")
}

/// Render the connected plan and apply the common build confirmation policy.
#[pyfunction]
#[pyo3(signature = (*, options, plan_text, protection_requirements=None))]
fn confirm_build<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    plan_text: &Value<'py>,
    protection_requirements: Option<&Value<'py>>,
) -> PyResult<bool> {
    let json_output: bool = options.getattr("json_output")?.is_truthy()?;
    let events_output: bool = options.getattr("events_output")?.is_truthy()?;
    if !json_output && !events_output {
        py.import("builtins")?
            .getattr("print")?
            .call1((plan_text,))?;
    }
    let requirements: Vec<Value<'py>> = match protection_requirements {
        Some(requirements) => items(requirements)?,
        None => Vec::new(),
    };
    let auto_approve = || -> PyResult<bool> { options.getattr("auto_approve")?.is_truthy() };
    if !requirements.is_empty() {
        let provided: Bound<'py, PyFrozenSet> =
            PyFrozenSet::new(py, items(&options.getattr("confirmations")?)?)?;
        for requirement in requirements {
            let pipeline_name: String = string(&requirement, "pipeline_name")?;
            let confirmation: Value<'py> = requirement.getattr("confirmation")?;
            print_error(
                py,
                &format!(
                    "PROTECTED PIPELINE: {pipeline_name}\n{}",
                    requirement.getattr("warning")?.str()?
                ),
            )?;
            if provided.contains(&confirmation)? {
                continue;
            }
            if auto_approve()? || json_output || events_output {
                print_error(
                    py,
                    &format!(
                        "Confirmation required: rerun with --confirm {}",
                        confirmation.str()?
                    ),
                )?;
                return Ok(false);
            }
            let entered: Value<'py> = answer(
                py,
                &format!(
                    "Type {} to build protected pipeline '{pipeline_name}': ",
                    confirmation.str()?
                ),
            )?;
            if entered.ne(&confirmation)? {
                print_error(py, "Protected pipeline confirmation did not match.")?;
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if auto_approve()? {
        return Ok(true);
    }
    let response: Value<'py> = answer(py, "Proceed with build? [y/N] ")?.call_method0("lower")?;
    py.import(ENTRY_CONSTANTS)?
        .getattr("AFFIRMATIVE_RESPONSES")?
        .contains(response)
}

/// The logical model keys a prepared build executes.
fn prepared_execution_keys<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let mut keys: Vec<Value<'py>> = Vec::new();
    if is_a(preparation, BUILD_MODELS, "MixedWorkflowPreparation")? {
        keys.extend(items(
            &preparation
                .getattr("virtual")?
                .getattr("preview")?
                .getattr("run_execution_scope")?,
        )?);
        keys.extend(items(
            &preparation
                .getattr("direct")?
                .getattr("preview")?
                .getattr("plan")?
                .getattr("execution_scope")?,
        )?);
    } else if is_a(preparation, BUILD_MODELS, "VirtualWorkflowPreparation")? {
        keys.extend(items(
            &preparation
                .getattr("preview")?
                .getattr("run_execution_scope")?,
        )?);
    } else {
        keys.extend(items(
            &preparation
                .getattr("preview")?
                .getattr("plan")?
                .getattr("execution_scope")?,
        )?);
    }
    PyFrozenSet::new(py, keys)
}

/// Reject a build whose final execution closure spans more pipelines than its target allows.
#[pyfunction]
#[pyo3(signature = (*, analysis, selectors=None, preparation=None))]
fn validate_build_pipeline_limit<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    selectors: Option<&Value<'py>>,
    preparation: Option<&Value<'py>>,
) -> PyResult<()> {
    let execution_keys: Value<'py> = match preparation.filter(|value| !value.is_none()) {
        Some(preparation) => prepared_execution_keys(py, preparation)?.into_any(),
        None => {
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("realized_project", analysis.getattr("realized_project")?)?;
            arguments.set_item("graph", analysis.getattr("graph")?)?;
            match selectors {
                Some(selectors) => arguments.set_item("selectors", selectors)?,
                None => arguments.set_item("selectors", tuple(py, Vec::new())?)?,
            }
            py.import(NATIVE)?
                .getattr("resolve_selection")?
                .call((), Some(&arguments))?
                .getattr("execution_logical_model_keys")?
        }
    };
    let loaded_project: Value<'py> = analysis
        .getattr("discovered_inputs")?
        .getattr("loaded_project")?;
    if loaded_project.is_none() {
        return Ok(());
    }
    let effective: Value<'py> = loaded_project.getattr("effective_configuration")?;
    if effective.is_none() {
        return Ok(());
    }
    let max_pipelines: Value<'py> = effective.getattr("build")?.getattr("max_pipelines")?;
    if max_pipelines.is_none() {
        return Ok(());
    }
    let mut pipeline_names: BTreeSet<String> = BTreeSet::new();
    for model in items(&analysis.getattr("compiled_project")?.getattr("models")?)? {
        if execution_keys.contains(model.getattr("key")?)? {
            pipeline_names.insert(string(&model, "pipeline_name")?);
        }
    }
    if max_pipelines.ge(pipeline_names.len())? {
        return Ok(());
    }
    let names: Vec<String> = pipeline_names.into_iter().collect();
    let message: String = format!(
        "Build affects {} pipelines, exceeding max_pipelines={} for target '{}': {}",
        names.len(),
        max_pipelines.str()?,
        effective.getattr("target_name")?.str()?,
        names.join(", ")
    );
    Err(PyErr::from_value(
        class(py, BUILD_EXCEPTIONS, "BuildPipelineLimitError")?.call1((message,))?,
    ))
}

/// A virtual-environment build result for CLI output.
#[pyfunction]
#[pyo3(signature = (*, result, database, json_output))]
fn render_virtual_build_result<'py>(
    py: Python<'py>,
    result: &Value<'py>,
    database: &Value<'py>,
    json_output: bool,
) -> PyResult<String> {
    let bootstrap: Value<'py> = result.getattr("bootstrap")?;
    let deployment_id: Value<'py> = bootstrap.getattr("deployment_id")?;
    let replay_results: Value<'py> = result.getattr("replay_results")?;
    let root_reports: Value<'py> = bootstrap.getattr("root_reports")?;
    if json_output {
        let mut replays: Vec<Value<'py>> = Vec::new();
        for replay in items(&replay_results)? {
            replays.push(dict(
                py,
                vec![
                    ("root", replay.getattr("root_key")?.getattr("name")?),
                    ("warehouse_written_rows", replay.getattr("written_rows")?),
                ],
            )?);
        }
        let mut reports: Vec<Value<'py>> = Vec::new();
        for report in items(&root_reports)? {
            reports.push(dict(
                py,
                vec![
                    ("name", report.getattr("root_key")?.getattr("name")?),
                    ("state_kind", report.getattr("state_kind")?),
                    ("replay_strategy", report.getattr("replay_strategy")?),
                    (
                        "active_deployment_id",
                        report.getattr("active_deployment_id")?,
                    ),
                ],
            )?);
        }
        let payload: Value<'py> = dict(
            py,
            vec![
                ("deployment_id", deployment_id),
                ("boundary_time", result.getattr("boundary_time")?),
                ("replays", list(py, replays)?),
                ("root_reports", list(py, reports)?),
            ],
        )?;
        return json_dumps(&payload, Some(2), false);
    }
    let style: Value<'py> = style(py)?;
    let object_name = |name: Value<'py>| -> PyResult<String> {
        method(&style, "object_name", &[("text", name)])?.extract()
    };
    let deployment: String = formatted(&deployment_id)?;
    let mut lines: Vec<String> = vec![
        styled(&style, "title", "Virtual Build Ready")?,
        label_value(&style, "Database", database)?,
        label_value(&style, "Deployment", &deployment_id)?,
        label_value(&style, "Boundary time", &result.getattr("boundary_time")?)?,
        String::new(),
        styled(&style, "section", "Roots")?,
    ];
    for report in items(&root_reports)? {
        lines.push(format!(
            "- {}",
            object_name(report.getattr("root_key")?.getattr("name")?)?
        ));
        lines.push(format!(
            "  {}: {}",
            styled(&style, "label", "state")?,
            formatted(&report.getattr("state_kind")?)?
        ));
        lines.push(format!(
            "  {}: {}",
            styled(&style, "label", "strategy")?,
            formatted(&report.getattr("replay_strategy")?)?
        ));
        let active: Value<'py> = report.getattr("active_deployment_id")?;
        if !active.is_none() {
            lines.push(format!(
                "  {}: {}",
                styled(&style, "label", "active deployment")?,
                formatted(&active)?
            ));
        }
    }
    lines.push(String::new());
    lines.push(styled(&style, "section", "Replay execution")?);
    let replays: Vec<Value<'py>> = items(&replay_results)?;
    for replay in &replays {
        let written_rows: Value<'py> = replay.getattr("written_rows")?;
        let written_rows: String = if written_rows.is_none() {
            "unavailable".to_owned()
        } else {
            written_rows.str()?.extract()?
        };
        lines.push(format!(
            "- {}  warehouse-written rows: {written_rows}",
            object_name(replay.getattr("root_key")?.getattr("name")?)?
        ));
    }
    if !replay_results.is_truthy()? {
        lines.push(NO_REPLAYS.to_owned());
    }
    lines.extend([
        String::new(),
        styled(&style, "section", "Next")?,
        format!("- sqt deployment audit {deployment}"),
        format!("- sqt deployment promote {deployment}"),
    ]);
    Ok(lines.join("\n"))
}

/// `f"{value}"`.
pub(crate) fn formatted(value: &Value<'_>) -> PyResult<String> {
    value.call_method1("__format__", ("",))?.extract()
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_direct_build_json, module)?)?;
    module.add_function(wrap_pyfunction!(render_direct_build_text, module)?)?;
    module.add_function(wrap_pyfunction!(render_interrupted_build_message, module)?)?;
    module.add_function(wrap_pyfunction!(render_direct_execution_json, module)?)?;
    module.add_function(wrap_pyfunction!(select_direct_build_audits, module)?)?;
    module.add_function(wrap_pyfunction!(prepare_direct_build_audits, module)?)?;
    module.add_function(wrap_pyfunction!(build_protection_requirements, module)?)?;
    module.add_function(wrap_pyfunction!(confirm_build, module)?)?;
    module.add_function(wrap_pyfunction!(render_virtual_build_result, module)?)?;
    module.add_function(wrap_pyfunction!(validate_build_pipeline_limit, module)?)
}
