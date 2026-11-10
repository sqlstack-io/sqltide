//! `sqt audit`: analyze the project, select SQL audits by the models they reference, defer audits
//! still warming up unless forced, run the rest through the Python audit executor, render the
//! results as text or JSON and record the invocation and per-audit node results. The executor,
//! warehouse loaders, observability records, CLI styling and JSON encoding of warehouse sample
//! values stay in Python and are looked up when called.

use std::collections::{BTreeSet, HashMap, HashSet};

use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice};

use crate::build_cli::formatted;
use crate::build_command::{int, invocation_record, print};
use crate::direct_build::{Value, boolean, build, call, class, items, method, none, text, tuple};
use crate::handling::while_handling;
use crate::test_command::{
    ModelGraph, is_none_attribute, joined, manual_context, model_graph, model_names, neighbours,
    node_result_record, persist_observations, quality_identity, selector_target_names, strict_pair,
    styled, summary, user_error,
};

const NATIVE: &str = "sqltide._native";
const CLI_STYLE: &str = "sqltide._native";
const START_INVOCATION: &str = "sqltide._native";
const AUDIT_MODELS: &str = "sqltide.executor.auditing.models";
const AUDIT_TYPES: &str = "sqltide.executor.auditing.types";
const LOAD_MODEL_ANCHORS: &str = "sqltide._native";
const LOAD_MATERIALIZED: &str = "sqltide._native";
const EXECUTE_SQL_AUDITS: &str = "sqltide._native";
const DEFERRED_RESULT: &str = "sqltide._native";
const UPSTREAM_SELECTOR_PREFIX: &str = "+";
const MAX_PAYLOAD_ROWS: isize = 5;

// ---------------------------------------------------------------------------------------------
// Selection

/// Filter discovered SQL audits by referenced-model selectors.
#[pyfunction]
#[pyo3(signature = (*, loaded_audits, compiled_pipelines, selectors))]
fn select_loaded_sql_audits<'py>(
    py: Python<'py>,
    loaded_audits: &Value<'py>,
    compiled_pipelines: &Value<'py>,
    selectors: &Value<'py>,
) -> PyResult<Value<'py>> {
    if !selectors.is_truthy()? {
        return Ok(loaded_audits.clone());
    }
    let selected_names: HashSet<String> = selected_model_names(py, compiled_pipelines, selectors)?;
    let mut selected: Vec<Value<'py>> = Vec::new();
    for audit in items(loaded_audits)? {
        let mut matches: bool = false;
        for name in items(&audit.getattr("referenced_model_names")?)? {
            if selected_names.contains(&name.extract::<String>()?) {
                matches = true;
            }
        }
        if matches {
            selected.push(audit);
        }
    }
    if selected.is_empty() {
        return Err(user_error(
            py,
            "No SQL audits matched the requested selectors.".to_owned(),
        )?);
    }
    tuple(py, selected)
}

fn selected_model_names(
    py: Python<'_>,
    compiled_pipelines: &Value<'_>,
    selectors: &Value<'_>,
) -> PyResult<HashSet<String>> {
    let pipelines: Vec<Value<'_>> = items(compiled_pipelines)?;
    let mut pipeline_model_names: HashMap<String, Vec<String>> = HashMap::new();
    for pipeline in &pipelines {
        let name: String = pipeline.getattr("pipeline")?.getattr("name")?.extract()?;
        pipeline_model_names.insert(name, model_names(pipeline)?);
    }
    let (upstream, _): ModelGraph = model_graph(&pipelines)?;
    let mut selected: HashSet<String> = HashSet::new();
    for raw_selector in items(selectors)? {
        let raw_selector: String = raw_selector.extract()?;
        let include_upstream: bool = raw_selector.starts_with(UPSTREAM_SELECTOR_PREFIX);
        let selector: &str = raw_selector
            .strip_prefix(UPSTREAM_SELECTOR_PREFIX)
            .unwrap_or(&raw_selector);
        if selector.is_empty() || selector.contains(UPSTREAM_SELECTOR_PREFIX) {
            return Err(user_error(
                py,
                format!(
                    "Unsupported audit selector syntax '{raw_selector}'. Use bare model names, \
                     pipeline:<name>, or optional leading '+'."
                ),
            )?);
        }
        let base_names: Vec<String> =
            selector_target_names(py, "audit", selector, &pipeline_model_names, &upstream)?;
        selected.extend(base_names.iter().cloned());
        if include_upstream {
            for name in &base_names {
                selected.extend(neighbours(name, &upstream));
            }
        }
    }
    Ok(selected)
}

// ---------------------------------------------------------------------------------------------
// Rendering

fn display_path(file_path: &Value<'_>, project_dir: &Value<'_>) -> PyResult<String> {
    let py: Python<'_> = file_path.py();
    match file_path.call_method1("relative_to", (project_dir,)) {
        Ok(relative) => relative.str()?.extract(),
        Err(error) if error.is_instance_of::<PyValueError>(py) => file_path.str()?.extract(),
        Err(error) => Err(error),
    }
}

fn list_of<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(PyList::new(value.py(), items(value)?)?.into_any())
}

fn rows_of<'py>(rows: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = rows.py();
    let mut listed: Vec<Value<'py>> = Vec::new();
    for row in items(rows)? {
        listed.push(list_of(&row)?);
    }
    Ok(PyList::new(py, listed)?.into_any())
}

fn result_payload<'py>(result: &Value<'py>, project_dir: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = result.py();
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item(
        "file_path",
        display_path(&result.getattr("file_path")?, project_dir)?,
    )?;
    payload.set_item("name", result.getattr("name")?)?;
    payload.set_item("severity", result.getattr("severity")?)?;
    payload.set_item("passed", result.getattr("passed")?)?;
    payload.set_item(
        "referenced_model_names",
        list_of(&result.getattr("referenced_model_names")?)?,
    )?;
    payload.set_item("description", result.getattr("description")?)?;
    payload.set_item("failing_row_count", result.getattr("failing_row_count")?)?;
    payload.set_item(
        "sample_column_names",
        list_of(&result.getattr("sample_column_names")?)?,
    )?;
    payload.set_item("sample_rows", rows_of(&result.getattr("sample_rows")?)?)?;
    payload.set_item("deferred_until", result.getattr("deferred_until")?)?;
    payload.set_item(
        "missing_relation_names",
        list_of(&result.getattr("missing_relation_names")?)?,
    )?;
    Ok(payload.into_any())
}

fn heading(result: &Value<'_>, project_dir: &Value<'_>) -> PyResult<String> {
    let path: String = display_path(&result.getattr("file_path")?, project_dir)?;
    let name: Value<'_> = result.getattr("name")?;
    if name.is_none() {
        return Ok(format!("- {path}"));
    }
    Ok(format!("- {path}  [{}]", formatted(&name)?))
}

/// `" | ".join(str(value) for value in values)`.
fn piped(py: Python<'_>, values: &Value<'_>, stringify: bool) -> PyResult<String> {
    let cells: Value<'_> = if stringify {
        let mut cells: Vec<Value<'_>> = Vec::new();
        for value in items(values)? {
            cells.push(value.str()?.into_any());
        }
        PyList::new(py, cells)?.into_any()
    } else {
        values.clone()
    };
    pyo3::types::PyString::new(py, " | ")
        .call_method1("join", (cells,))?
        .extract()
}

fn group(
    py: Python<'_>,
    title: &str,
    results: &[Value<'_>],
    project_dir: &Value<'_>,
) -> PyResult<Vec<String>> {
    let mut lines: Vec<String> = vec![styled(py, "section", title)?];
    if results.is_empty() {
        lines.push("(none)".to_owned());
        return Ok(lines);
    }
    for result in results {
        lines.push(heading(result, project_dir)?);
        lines.push(format!(
            "    models: {}",
            joined(py, &result.getattr("referenced_model_names")?)?
        ));
        let description: Value<'_> = result.getattr("description")?;
        if !description.is_none() {
            lines.push(format!("    description: {}", formatted(&description)?));
        }
        let deferred_until: Value<'_> = result.getattr("deferred_until")?;
        if !deferred_until.is_none() {
            lines.push(format!(
                "    eligible at: {} UTC",
                formatted(&deferred_until)?
            ));
            continue;
        }
        let missing: Value<'_> = result.getattr("missing_relation_names")?;
        if missing.is_truthy()? {
            lines.push(format!("    missing relations: {}", joined(py, &missing)?));
            continue;
        }
        lines.push(format!(
            "    failing rows: {}",
            formatted(&result.getattr("failing_row_count")?)?
        ));
        let sample_rows: Value<'_> = result.getattr("sample_rows")?;
        if sample_rows.is_truthy()? {
            lines.push("    sample:".to_owned());
            lines.push(format!(
                "      | {} |",
                piped(py, &result.getattr("sample_column_names")?, false)?
            ));
            for row in items(&result.getattr("sample_rows")?)? {
                lines.push(format!("      | {} |", piped(py, &row, true)?));
            }
        }
    }
    Ok(lines)
}

/// Results of `severity` that failed and were not deferred, or every deferred result.
fn grouped<'py>(
    results: &[Value<'py>],
    severity: Option<&Value<'py>>,
) -> PyResult<Vec<Value<'py>>> {
    let mut chosen: Vec<Value<'py>> = Vec::new();
    for result in results {
        let included: bool = match severity {
            Some(severity) => {
                result.getattr("severity")?.eq(severity)?
                    && !result.getattr("passed")?.is_truthy()?
                    && !result.getattr("deferred")?.is_truthy()?
            }
            None => result.getattr("deferred")?.is_truthy()?,
        };
        if included {
            chosen.push(result.clone());
        }
    }
    Ok(chosen)
}

/// Render a live SQL audit run result.
fn render<'py>(
    py: Python<'py>,
    result: &Value<'py>,
    database: &Value<'py>,
    project_dir: &Value<'py>,
    json_output: bool,
) -> PyResult<Value<'py>> {
    if json_output {
        let payload: Bound<'_, PyDict> = PyDict::new(py);
        payload.set_item("database", database)?;
        payload.set_item(
            "error_failure_count",
            result.getattr("error_failure_count")?,
        )?;
        payload.set_item(
            "warning_failure_count",
            result.getattr("warning_failure_count")?,
        )?;
        let mut audit_results: Vec<Value<'_>> = Vec::new();
        for audit_result in items(&result.getattr("audit_results")?)? {
            audit_results.push(result_payload(&audit_result, project_dir)?);
        }
        payload.set_item("audit_results", PyList::new(py, audit_results)?)?;
        let arguments: Bound<'_, PyDict> = PyDict::new(py);
        arguments.set_item("indent", 2)?;
        return py
            .import("json")?
            .getattr("dumps")?
            .call((payload,), Some(&arguments));
    }
    let style: Value<'_> = py.import(CLI_STYLE)?.getattr("cli_style")?.call0()?;
    let title: String = style.call_method1("title", ("Audit Results",))?.extract()?;
    let database_line: String = method(
        &py.import(CLI_STYLE)?.getattr("cli_style")?.call0()?,
        "label_value",
        &[("label", text(py, "Database")), ("value", database.clone())],
    )?
    .extract()?;
    let mut lines: Vec<String> = vec![title, database_line, String::new()];
    let severities: Value<'_> = class(py, AUDIT_TYPES, "AuditSeverity")?;
    let results: Vec<Value<'_>> = items(&result.getattr("audit_results")?)?;
    let errors: Vec<Value<'_>> = grouped(&results, Some(&severities.getattr("ERROR")?))?;
    lines.extend(group(py, "Errors", &errors, project_dir)?);
    lines.push(String::new());
    let results: Vec<Value<'_>> = items(&result.getattr("audit_results")?)?;
    let warnings: Vec<Value<'_>> = grouped(&results, Some(&severities.getattr("WARNING")?))?;
    lines.extend(group(py, "Warnings", &warnings, project_dir)?);
    lines.push(String::new());
    let results: Vec<Value<'_>> = items(&result.getattr("audit_results")?)?;
    let deferred: Vec<Value<'_>> = grouped(&results, None)?;
    lines.extend(group(py, "Deferred", &deferred, project_dir)?);
    lines.push(String::new());
    let verdict: &str = if result.getattr("error_failure_count")?.is_truthy()? {
        "FAIL"
    } else {
        "PASS"
    };
    lines.push(format!(
        "Result: {verdict} ({} errors, {} warnings)",
        formatted(&result.getattr("error_failure_count")?)?,
        formatted(&result.getattr("warning_failure_count")?)?
    ));
    Ok(text(py, &lines.join("\n")))
}

// ---------------------------------------------------------------------------------------------
// Command

/// `audit.name or audit.file_path.stem`.
fn audit_key<'py>(audit: &Value<'py>) -> PyResult<Value<'py>> {
    let name: Value<'py> = audit.getattr("name")?;
    if name.is_truthy()? {
        return Ok(name);
    }
    audit.getattr("file_path")?.getattr("stem")
}

/// `{model.key.name: <relation name>}` over every compiled model, the relation optionally
/// qualified by `database`.
fn relation_names<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    database: Option<&Value<'py>>,
) -> PyResult<Value<'py>> {
    let relations: Value<'py> = analysis
        .getattr("realized_project")?
        .getattr("relation_name_by_logical_key")?;
    let names: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(&analysis.getattr("compiled_project")?.getattr("models")?)? {
        let key: Value<'py> = model.getattr("key")?;
        let name: Value<'py> = key.getattr("name")?;
        let relation: Value<'py> = relations.get_item(&key)?;
        let value: Value<'py> = match database {
            Some(database) => text(
                py,
                &format!("{}.{}", formatted(database)?, formatted(&relation)?),
            ),
            None => relation,
        };
        names.set_item(name, value)?;
    }
    Ok(names.into_any())
}

/// Sorted unique model names the audits reference.
fn referenced_model_names<'py>(py: Python<'py>, audits: &Value<'py>) -> PyResult<Value<'py>> {
    let mut names: BTreeSet<String> = BTreeSet::new();
    for audit in items(audits)? {
        for name in items(&audit.getattr("referenced_model_names")?)? {
            names.insert(name.extract()?);
        }
    }
    tuple(py, names.into_iter().map(|name| text(py, &name)).collect())
}

/// Run the eligible audits and merge in deferred results for the ones still warming up.
fn execute_selected_audits<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    selected: &Value<'py>,
    database: &Value<'py>,
    client: &Value<'py>,
    adapter_profile: &Value<'py>,
    force: bool,
) -> PyResult<Value<'py>> {
    let anchors: Value<'py> = if force {
        PyDict::new(py).into_any()
    } else {
        call(
            py,
            LOAD_MODEL_ANCHORS,
            "load_model_anchors",
            &[
                ("client", client.clone()),
                ("metadata_database", database.clone()),
                ("target_database", database.clone()),
                ("model_names", referenced_model_names(py, selected)?),
                (
                    "virtual_environments",
                    analysis
                        .getattr("compile_inputs")?
                        .getattr("virtual_environments")?,
                ),
            ],
        )?
    };
    let materialized: Value<'py> = call(
        py,
        LOAD_MATERIALIZED,
        "load_materialized_model_names",
        &[
            ("client", client.clone()),
            ("database", database.clone()),
            (
                "relation_name_by_model",
                relation_names(py, analysis, None)?,
            ),
        ],
    )?;
    let warmup_states: Value<'py> = call(
        py,
        NATIVE,
        "resolve_audit_warmup_states",
        &[
            ("audits", selected.clone()),
            ("anchors_by_model", anchors),
            ("materialized_model_names", materialized),
            (
                "warehouse_now",
                client.call_method0("capture_warehouse_timestamp")?,
            ),
        ],
    )?;
    let mut executable: Vec<Value<'py>> = Vec::new();
    for audit in items(selected)? {
        if force
            || warmup_states
                .get_item(audit_key(&audit)?)?
                .getattr("eligible")?
                .is_truthy()?
        {
            executable.push(audit);
        }
    }
    let executable: Value<'py> = tuple(py, executable)?;
    let executed: Value<'py> = call(
        py,
        EXECUTE_SQL_AUDITS,
        "execute_sql_audits",
        &[
            ("loaded_audits", executable.clone()),
            ("resolver", relation_names(py, analysis, Some(database))?),
            ("client", client.clone()),
            ("dialect", adapter_profile.getattr("sql_analysis_dialect")?),
        ],
    )?;
    let executed_by_name: Bound<'py, PyDict> = PyDict::new(py);
    let mut audits = executable.try_iter()?;
    let mut results = executed.getattr("audit_results")?.try_iter()?;
    while let Some((audit, result)) = strict_pair(&mut audits, &mut results)? {
        executed_by_name.set_item(audit_key(&audit)?, result)?;
    }
    let mut audit_results: Vec<Value<'py>> = Vec::new();
    for audit in items(selected)? {
        let executed: Value<'py> = executed_by_name.call_method1("get", (audit_key(&audit)?,))?;
        audit_results.push(if executed.is_truthy()? {
            executed
        } else {
            call(
                py,
                DEFERRED_RESULT,
                "deferred_audit_result",
                &[
                    ("audit", audit.clone()),
                    ("state", warmup_states.get_item(audit_key(&audit)?)?),
                ],
            )?
        });
    }
    build(
        py,
        AUDIT_MODELS,
        "SqlAuditRunResult",
        &[("audit_results", tuple(py, audit_results)?)],
    )
}

fn audit_status(py: Python<'_>, result: &Value<'_>) -> PyResult<&'static str> {
    if result.getattr("deferred")?.is_truthy()? {
        return Ok("deferred");
    }
    if !is_none_attribute(result, "error_message")? {
        return Ok("error");
    }
    if result.getattr("passed")?.is_truthy()? {
        return Ok("passed");
    }
    let warning: Value<'_> = class(py, AUDIT_TYPES, "AuditSeverity")?.getattr("WARNING")?;
    Ok(if result.getattr("severity")?.eq(warning)? {
        "warning"
    } else {
        "failed"
    })
}

fn audit_node_results<'py>(
    py: Python<'py>,
    invocation: &Value<'py>,
    audits: &Value<'py>,
    result: &Value<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let mut records: Vec<Value<'py>> = Vec::new();
    let mut audits = audits.try_iter()?;
    let mut results = result.getattr("audit_results")?.try_iter()?;
    while let Some((audit, audit_result)) = strict_pair(&mut audits, &mut results)? {
        let status: &str = audit_status(py, &audit_result)?;
        let identity: Value<'py> = quality_identity(&audit)?;
        let context: Value<'py> = manual_context(
            py,
            &[
                ("cadence_seconds", audit.getattr("cadence_seconds")?),
                ("warmup_seconds", audit.getattr("warmup_seconds")?),
            ],
        )?;
        let severity: Value<'py> = audit_result.getattr("severity")?;
        let failure_count: Value<'py> = audit_result.getattr("failing_row_count")?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item(
            "sample_column_names",
            list_of(&audit_result.getattr("sample_column_names")?)?,
        )?;
        payload.set_item(
            "sample_rows",
            rows_of(&audit_result.getattr("sample_rows")?.get_item(PySlice::new(
                py,
                0,
                MAX_PAYLOAD_ROWS,
                1,
            ))?)?,
        )?;
        payload.set_item("eligible_at", audit_result.getattr("deferred_until")?)?;
        payload.set_item(
            "missing_relations",
            list_of(&audit_result.getattr("missing_relation_names")?)?,
        )?;
        records.push(node_result_record(
            py,
            &[
                ("invocation", invocation.clone()),
                ("identity", identity),
                ("context", context),
                ("status", text(py, status)),
                ("severity", severity),
                ("failure_count", failure_count),
                ("payload", payload.into_any()),
                ("error_message", audit_result.getattr("error_message")?),
            ],
        )?);
    }
    Ok(records)
}

/// The inputs of one `sqt audit` invocation.
struct AuditCommand<'a, 'py> {
    py: Python<'py>,
    started: Value<'py>,
    pipelines_root: &'a Value<'py>,
    project_dir: &'a Value<'py>,
    database: &'a Value<'py>,
    selectors: &'a Value<'py>,
    json_output: bool,
    client: &'a Value<'py>,
    loaded_project: &'a Value<'py>,
    adapter_profile: &'a Value<'py>,
    force: bool,
}

impl<'py> AuditCommand<'_, 'py> {
    fn invocation(
        &self,
        outcome: &str,
        exit_code: i64,
        selected_node_count: i64,
        error_message: Value<'py>,
        summary: Value<'py>,
    ) -> PyResult<Value<'py>> {
        let py: Python<'py> = self.py;
        invocation_record(
            py,
            &self.started,
            &[
                ("project_dir", self.project_dir.clone()),
                ("target_identity", self.database.clone()),
                ("command", text(py, "audit")),
                ("mode", none(py)),
                ("outcome", text(py, outcome)),
                ("exit_code", int(py, exit_code)?),
                ("materialized_outcome", none(py)),
                ("deployment_id", none(py)),
                ("workflow_id", none(py)),
                ("selected_node_count", int(py, selected_node_count)?),
                ("error_message", error_message),
                ("summary", summary),
            ],
        )
    }

    /// The command's `try` body: the selected audits and their run result.
    fn execute(&self, selected_node_count: &mut i64) -> PyResult<(Value<'py>, Value<'py>)> {
        let py: Python<'py> = self.py;
        self.client
            .call_method1("validate_metadata_state", (self.database,))?;
        let analysis: Value<'py> = call(
            py,
            NATIVE,
            "analyze_project",
            &[
                ("pipelines_root", self.pipelines_root.clone()),
                ("loaded_project", self.loaded_project.clone()),
                ("adapter_profile", self.adapter_profile.clone()),
            ],
        )?;
        let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
        let selected: Value<'py> = select_loaded_sql_audits(
            py,
            &compiled_project.getattr("audits")?,
            &compiled_project.getattr("pipelines")?,
            self.selectors,
        )?;
        *selected_node_count = selected.len()? as i64;
        let result: Value<'py> = execute_selected_audits(
            py,
            &analysis,
            &selected,
            self.database,
            self.client,
            self.adapter_profile,
            self.force,
        )?;
        let rendered: Value<'py> = render(
            py,
            &result,
            self.database,
            self.project_dir,
            self.json_output,
        )?;
        print(py, &rendered, false)?;
        Ok((selected, result))
    }
}

/// Run user-defined SQL audits against published logical views.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, project_dir, database, selectors, json_output, client, loaded_project,
    adapter_profile, force=None
))]
#[allow(clippy::too_many_arguments)]
fn run_audit<'py>(
    py: Python<'py>,
    pipelines_root: &Value<'py>,
    project_dir: &Value<'py>,
    database: &Value<'py>,
    selectors: &Value<'py>,
    json_output: &Value<'py>,
    client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
    force: Option<Value<'py>>,
) -> PyResult<i64> {
    let command: AuditCommand<'_, 'py> = AuditCommand {
        py,
        started: call(py, START_INVOCATION, "start_invocation", &[])?,
        pipelines_root,
        project_dir,
        database,
        selectors,
        json_output: json_output.is_truthy()?,
        client,
        loaded_project,
        adapter_profile,
        force: match force {
            Some(force) => force.is_truthy()?,
            None => false,
        },
    };
    let mut selected_node_count: i64 = 0;
    let (selected, result) = match command.execute(&mut selected_node_count) {
        Ok(executed) => executed,
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let record = || -> PyResult<()> {
                let message: Value<'py> = error.value(py).str()?.into_any();
                let invocation: Value<'py> = command.invocation(
                    "failed",
                    1,
                    selected_node_count,
                    message,
                    summary(py, "failed_before_results", boolean(py, true)?)?,
                )?;
                persist_observations(client, database, invocation, Vec::new())
            };
            if let Err(failure) = record() {
                return Err(while_handling(py, failure, &error));
            }
            return Err(error);
        }
        Err(error) => return Err(error),
    };
    let exit_code: i64 = i64::from(result.getattr("error_failure_count")?.is_truthy()?);
    let counts: Bound<'py, PyDict> = PyDict::new(py);
    counts.set_item(
        "error_failure_count",
        result.getattr("error_failure_count")?,
    )?;
    counts.set_item(
        "warning_failure_count",
        result.getattr("warning_failure_count")?,
    )?;
    let invocation: Value<'py> = command.invocation(
        if exit_code != 0 {
            "failed"
        } else {
            "succeeded"
        },
        exit_code,
        selected.len()? as i64,
        none(py),
        counts.into_any(),
    )?;
    let node_results: Vec<Value<'py>> = audit_node_results(py, &invocation, &selected, &result)?;
    persist_observations(client, database, invocation, node_results)?;
    Ok(exit_code)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_audit, module)?)?;
    module.add_function(wrap_pyfunction!(select_loaded_sql_audits, module)?)
}
