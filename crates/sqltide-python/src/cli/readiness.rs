//! Deployment audit and promotion: candidate resolution, the staged quality checks, and the
//! audit, ambiguity and promotion reports.

use std::collections::HashMap;

use pyo3::exceptions::{PyIndexError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PySet, PyTuple};
use sqltide_executor::workflow::format_general;
use sqltide_json::JsonValue;
use sqltide_planner::cli_style::CliStyle as Style;

use crate::cli::commands::{atomicity_payload, published_view_lines, reported};
use crate::cli::output::{fields, joined, json_text, json_value, object, shown, shown_field};
use crate::cli::python::{print, print_to};
use crate::cli::render::title;
use crate::cli::style::terminal_style;
use crate::dev_server::support::attr;
use crate::direct_build::{build, call, items, or, replace};

const NATIVE: &str = "sqltide._native";
const READINESS_MODELS: &str = "sqltide.executor.readiness.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const NOT_AVAILABLE: &str = "n/a";
const READY: &str = "ready";

type Any<'py> = Bound<'py, PyAny>;

// ---------------------------------------------------------------------------------------------
// Formatting

/// `f"{value * 100:.1f}%"`.
fn percentage(value: &Any<'_>) -> PyResult<String> {
    let scaled: f64 = value.extract::<f64>()? * 100.0;
    Ok(if scaled.is_nan() {
        "nan%".to_owned()
    } else {
        format!("{scaled:.1}%")
    })
}

/// A count, or `n/a` when it is unknown.
fn count(value: &Any<'_>) -> PyResult<String> {
    if value.is_none() {
        Ok(NOT_AVAILABLE.to_owned())
    } else {
        shown(value)
    }
}

/// `"yes"` or `"no"`.
fn yes_no(value: &Any<'_>) -> PyResult<&'static str> {
    Ok(if value.is_truthy()? { "yes" } else { "no" })
}

/// A bounded range, or `n/a` when both bounds are unknown.
fn range(min_value: &Any<'_>, max_value: &Any<'_>) -> PyResult<String> {
    if min_value.is_none() && max_value.is_none() {
        return Ok(NOT_AVAILABLE.to_owned());
    }
    let bound = |value: &Any<'_>| -> PyResult<String> {
        if value.is_truthy()? {
            shown(value)
        } else {
            Ok(NOT_AVAILABLE.to_owned())
        }
    };
    Ok(format!("{} .. {}", bound(min_value)?, bound(max_value)?))
}

/// A warehouse timestamp as an ISO-8601 UTC string.
fn timestamp(value: &str) -> String {
    let normalized: String = value.replace(' ', "T");
    if normalized.ends_with('Z') {
        normalized
    } else {
        format!("{normalized}Z")
    }
}

/// `", ".join(...)` of a string tuple.
fn names(values: &[String]) -> String {
    values.join(", ")
}

/// `file_path` relative to `project_dir` when it lies beneath it.
fn display_path(file_path: &Any<'_>, project_dir: &Any<'_>) -> PyResult<String> {
    if project_dir.is_none() {
        return shown(file_path);
    }
    match file_path.call_method1("relative_to", (project_dir,)) {
        Ok(relative) => shown(&relative),
        Err(error) if error.is_instance_of::<PyValueError>(file_path.py()) => shown(file_path),
        Err(error) => Err(error),
    }
}

// ---------------------------------------------------------------------------------------------
// Candidates

/// The roots the ambiguity names: active bindings, else physical candidates.
fn candidate_root_names(inspected_state: &Any<'_>) -> PyResult<Vec<String>> {
    let sorted = |attribute: &str| -> PyResult<Vec<String>> {
        let mut names: Vec<String> = items(&inspected_state.getattr(attribute)?)?
            .iter()
            .map(|value| value.getattr("logical_name")?.extract())
            .collect::<PyResult<_>>()?;
        names.sort();
        names.dedup();
        Ok(names)
    };
    let active: Vec<String> = sorted("active_bindings")?;
    if !active.is_empty() {
        return Ok(active);
    }
    sorted("physical_candidates")
}

/// The table and view root names of one loaded deployment, else its prepared table and views.
fn loaded_deployment_root_names(py: Python<'_>, loaded: &Any<'_>) -> PyResult<Vec<String>> {
    let relation_types: Bound<'_, PySet> = PySet::new(
        py,
        [
            attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_TABLE")?,
            attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_VIEW")?,
        ],
    )?;
    let relation_names = |keys: Vec<Any<'_>>| -> PyResult<Vec<String>> {
        let mut names: Vec<String> = Vec::new();
        for key in keys {
            if relation_types.contains(key.getattr("object_type")?)? {
                names.push(key.getattr("name")?.extract()?);
            }
        }
        names.sort();
        Ok(names)
    };
    let roots: Vec<String> = relation_names(items(&loaded.getattr("root_keys")?)?)?;
    if !roots.is_empty() {
        return Ok(roots);
    }
    relation_names(
        items(&loaded.getattr("prepared_object_mappings")?)?
            .iter()
            .map(|mapping| mapping.get_item(0))
            .collect::<PyResult<_>>()?,
    )
}

fn load_audit_deployment<'py>(
    py: Python<'py>,
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    deployment_id: Any<'py>,
) -> PyResult<Any<'py>> {
    call(
        py,
        NATIVE,
        "load_audit_deployment",
        &[
            ("client", client.clone()),
            ("metadata_database", metadata_database.clone()),
            ("deployment_id", deployment_id),
        ],
    )
}

/// Each candidate with its creation time, status and root names loaded.
fn enrich_candidates<'py>(
    py: Python<'py>,
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    candidates: &[Any<'py>],
) -> PyResult<Vec<Any<'py>>> {
    let mut enriched: Vec<Any<'py>> = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let deployment_id: Any<'py> = candidate.getattr("deployment_id")?;
        let loaded: Any<'py> =
            load_audit_deployment(py, client, metadata_database, deployment_id.clone())?;
        let root_names: Vec<String> = loaded_deployment_root_names(py, &loaded)?;
        enriched.push(build(
            py,
            READINESS_MODELS,
            "AuditDeploymentCandidate",
            &[
                ("deployment_id", deployment_id),
                ("created_at", loaded.getattr("created_at")?),
                ("deployment_status", loaded.getattr("status")?),
                ("root_names", PyTuple::new(py, root_names)?.into_any()),
            ],
        )?);
    }
    Ok(enriched)
}

/// The no-candidates or ambiguity message for `command_name`, or `None` when exactly one
/// candidate deployment exists.
fn candidate_message<'py>(
    py: Python<'py>,
    command_name: &str,
    candidates: &[Any<'py>],
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    database: &Any<'py>,
) -> PyResult<Option<String>> {
    let style: Style = terminal_style(py)?;
    if candidates.is_empty() {
        return Ok(Some(no_candidates_message(
            style,
            command_name,
            &shown(database)?,
        )));
    }
    if candidates.len() == 1 {
        return Ok(None);
    }
    let inspected_state: Any<'py> =
        client.call_method1("inspect_managed_table_state", (database,))?;
    let root_names: Vec<String> = candidate_root_names(&inspected_state)?;
    let enriched: Vec<Any<'py>> = enrich_candidates(py, client, metadata_database, candidates)?;
    ambiguous_message(
        style,
        command_name,
        &shown(database)?,
        &root_names,
        &enriched,
    )
    .map(Some)
}

fn no_candidates_message(style: Style, command_name: &str, database: &str) -> String {
    [
        style.title(&format!(
            "No staged deployment candidates are available for {command_name}"
        )),
        style.label_value("Database", database),
    ]
    .join("\n")
}

fn ambiguous_message(
    style: Style,
    command_name: &str,
    database: &str,
    root_names: &[String],
    candidates: &[Any<'_>],
) -> PyResult<String> {
    let mut sorted: Vec<(String, &Any<'_>)> = candidates
        .iter()
        .map(|candidate| Ok((candidate.getattr("deployment_id")?.extract()?, candidate)))
        .collect::<PyResult<_>>()?;
    sorted.sort_by(|left, right| right.0.cmp(&left.0));
    let mut lines: Vec<String> = vec![
        style.title(&format!("{} selection is ambiguous", title(command_name))),
        style.label_value("Database", database),
        String::new(),
    ];
    if !root_names.is_empty() {
        lines.push(style.section("Affected roots"));
        lines.extend(root_names.iter().map(|name| format!("- {name}")));
        lines.push(String::new());
    }
    lines.push(style.section("Candidate deployments"));
    for (deployment_id, candidate) in &sorted {
        lines.push(format!("- {deployment_id}"));
        let created_at: Any<'_> = candidate.getattr("created_at")?;
        if !created_at.is_none() {
            lines.push(format!(
                "  created at: {}",
                timestamp(&created_at.extract::<String>()?)
            ));
        }
        let status: Any<'_> = candidate.getattr("deployment_status")?;
        if !status.is_none() {
            lines.push(format!("  status: {}", shown(&status)?));
        }
        if candidate.getattr("root_names")?.is_truthy()? {
            lines.push(format!("  roots: {}", joined(candidate, "root_names")?));
        }
    }
    lines.push(String::new());
    lines.push(style.section("Recommended"));
    let (recommended, _) = sorted
        .first()
        .ok_or_else(|| PyIndexError::new_err("tuple index out of range"))?;
    lines.push(format!("- sqt {command_name} {recommended}"));
    Ok(lines.join("\n"))
}

// ---------------------------------------------------------------------------------------------
// Quality checks

/// The SQL audits that read at least one staged relation.
fn selected_backfill_sql_audits<'py>(
    compiled_pipelines: &[Any<'py>],
    loaded_audits: &[Any<'py>],
    staged_logical_table_names: &[String],
) -> PyResult<Vec<Any<'py>>> {
    let mut relation_name_by_model_name: HashMap<String, String> = HashMap::new();
    for pipeline in compiled_pipelines {
        for model in items(&pipeline.getattr("models")?)? {
            relation_name_by_model_name.insert(
                model.getattr("key")?.getattr("name")?.extract()?,
                model.getattr("relation_name")?.extract()?,
            );
        }
    }
    let mut selected: Vec<Any<'py>> = Vec::new();
    for audit in loaded_audits {
        for model_name in items(&audit.getattr("referenced_model_names")?)? {
            let name: String = model_name.extract()?;
            let relation_name: &String = relation_name_by_model_name
                .get(&name)
                .ok_or_else(|| PyKeyError::new_err(model_name.clone().unbind()))?;
            if staged_logical_table_names.contains(relation_name) {
                selected.push(audit.clone());
                break;
            }
        }
    }
    Ok(selected)
}

/// Every compiled model's audit table: the staged physical name, else its relation name.
fn backfill_audit_resolver<'py>(
    py: Python<'py>,
    database: &str,
    compiled_pipelines: &[Any<'py>],
    prepared_object_mappings: &[(String, String)],
) -> PyResult<Bound<'py, PyDict>> {
    let staged_name_by_logical_name: HashMap<&str, &str> = prepared_object_mappings
        .iter()
        .map(|(logical, physical)| (logical.as_str(), physical.as_str()))
        .collect();
    let resolver: Bound<'py, PyDict> = PyDict::new(py);
    for pipeline in compiled_pipelines {
        for model in items(&pipeline.getattr("models")?)? {
            let relation_name: String = model.getattr("relation_name")?.extract()?;
            let table_name: &str = staged_name_by_logical_name
                .get(relation_name.as_str())
                .copied()
                .unwrap_or(&relation_name);
            resolver.set_item(
                model.getattr("key")?.getattr("name")?,
                format!("{database}.{table_name}"),
            )?;
        }
    }
    Ok(resolver)
}

/// Run the staged SQL audits and fold their failures into the assessment.
#[allow(clippy::too_many_arguments)]
fn execute_audit_quality_checks<'py>(
    py: Python<'py>,
    result: Any<'py>,
    project_dir: &Any<'py>,
    compiled_pipelines: &[Any<'py>],
    loaded_audits: &[Any<'py>],
    client: &Any<'py>,
    metadata_database: &Any<'py>,
    database: &Any<'py>,
    dialect: Any<'py>,
) -> PyResult<Any<'py>> {
    if project_dir.is_none() || compiled_pipelines.is_empty() {
        return Ok(result);
    }
    let loaded: Any<'py> = load_audit_deployment(
        py,
        client,
        metadata_database,
        result.getattr("deployment_id")?,
    )?;
    let mut mappings: Vec<(String, String)> = Vec::new();
    for mapping in items(&loaded.getattr("prepared_object_mappings")?)? {
        mappings.push((
            mapping.get_item(0)?.getattr("name")?.extract()?,
            mapping.get_item(1)?.extract()?,
        ));
    }
    let staged_names: Vec<String> = mappings.iter().map(|(name, _)| name.clone()).collect();
    let audits: Vec<Any<'py>> =
        selected_backfill_sql_audits(compiled_pipelines, loaded_audits, &staged_names)?;
    let resolver: Bound<'py, PyDict> =
        backfill_audit_resolver(py, &shown(database)?, compiled_pipelines, &mappings)?;
    let quality: Any<'py> = call(
        py,
        "sqltide._native",
        "execute_sql_audits",
        &[
            ("loaded_audits", PyTuple::new(py, audits)?.into_any()),
            ("resolver", resolver.into_any()),
            ("client", client.clone()),
            ("dialect", dialect),
        ],
    )?;
    let assessment: Any<'py> = if quality.getattr("error_failure_count")?.is_truthy()? {
        attr(py, "sqltide.executor.readiness.types", "AuditAssessment")?.getattr("NOT_READY")?
    } else {
        result.getattr("assessment")?
    };
    replace(
        &result,
        &[
            ("assessment", assessment),
            ("quality_check_results", quality.getattr("audit_results")?),
        ],
    )
}

/// The project's configured readiness thresholds, else the defaults.
fn readiness_thresholds<'py>(py: Python<'py>, loaded_project: &Any<'py>) -> PyResult<Any<'py>> {
    let configuration: Any<'py> = if loaded_project.is_none() {
        loaded_project.clone()
    } else {
        loaded_project.getattr("effective_configuration")?
    };
    if configuration.is_none() {
        return build(py, READINESS_MODELS, "DeploymentReadinessThresholds", &[]);
    }
    let defaults: Any<'py> = configuration
        .getattr("defaults")?
        .getattr("deployment_readiness")?;
    build(
        py,
        READINESS_MODELS,
        "DeploymentReadinessThresholds",
        &[
            (
                "maximum_lag_seconds",
                defaults.getattr("maximum_lag_seconds")?,
            ),
            (
                "minimum_staged_row_ratio",
                defaults.getattr("minimum_staged_row_ratio")?,
            ),
        ],
    )
}

// ---------------------------------------------------------------------------------------------
// Audit report

fn optional_fields(value: &Any<'_>, names: &[&str]) -> PyResult<JsonValue> {
    if value.is_none() {
        Ok(JsonValue::Null)
    } else {
        fields(value, names)
    }
}

fn root_result_payload(root: &Any<'_>) -> PyResult<JsonValue> {
    let value = |name: &str| -> PyResult<JsonValue> { json_value(&root.getattr(name)?) };
    Ok(object(vec![
        (
            "name",
            json_value(&root.getattr("root_key")?.getattr("name")?)?,
        ),
        ("state", value("state")?),
        ("replay_source_name", value("replay_source_name")?),
        ("replay_source_row_count", value("replay_source_row_count")?),
        ("staged_exists", value("staged_exists")?),
        ("active_exists", value("active_exists")?),
        ("active_row_count", value("active_row_count")?),
        ("staged_row_count", value("staged_row_count")?),
        ("row_delta", value("row_delta")?),
        ("row_ratio", value("row_ratio")?),
        ("assessment", value("assessment")?),
        ("replay_lineage_mode", value("replay_lineage_mode")?),
        (
            "offset_catchup_summary",
            optional_fields(
                &root.getattr("offset_catchup_summary")?,
                &[
                    "active_partition_count",
                    "staged_partition_count",
                    "partitions_compared",
                    "missing_staged_partition_count",
                    "missing_freshness_partition_count",
                    "lagging_partition_count",
                    "max_offset_gap",
                    "average_offset_gap",
                    "lag_boundary_column",
                    "max_lag_seconds",
                    "average_lag_seconds",
                ],
            )?,
        ),
        (
            "scalar_catchup_summary",
            optional_fields(
                &root.getattr("scalar_catchup_summary")?,
                &[
                    "active_min_value",
                    "active_max_value",
                    "staged_min_value",
                    "staged_max_value",
                    "lag_seconds",
                ],
            )?,
        ),
        (
            "warnings",
            JsonValue::Array(
                items(&root.getattr("warnings")?)?
                    .iter()
                    .map(json_value)
                    .collect::<PyResult<_>>()?,
            ),
        ),
    ]))
}

fn audit_json(result: &Any<'_>, project_dir: &Any<'_>) -> PyResult<String> {
    let thresholds: Any<'_> = result.getattr("thresholds")?;
    let mut checks: Vec<JsonValue> = Vec::new();
    for audit in items(&result.getattr("quality_check_results")?)? {
        let value = |name: &str| -> PyResult<JsonValue> { json_value(&audit.getattr(name)?) };
        checks.push(object(vec![
            (
                "file_path",
                JsonValue::String(display_path(&audit.getattr("file_path")?, project_dir)?),
            ),
            ("name", value("name")?),
            ("severity", value("severity")?),
            ("passed", value("passed")?),
            (
                "referenced_model_names",
                JsonValue::Array(
                    items(&audit.getattr("referenced_model_names")?)?
                        .iter()
                        .map(json_value)
                        .collect::<PyResult<_>>()?,
                ),
            ),
            ("description", value("description")?),
            ("failing_row_count", value("failing_row_count")?),
        ]));
    }
    let payload: JsonValue = object(vec![
        (
            "deployment_id",
            json_value(&result.getattr("deployment_id")?)?,
        ),
        (
            "deployment_status",
            json_value(&result.getattr("deployment_status")?)?,
        ),
        ("assessment", json_value(&result.getattr("assessment")?)?),
        (
            "replay_lineage_mode",
            json_value(&result.getattr("replay_lineage_mode")?)?,
        ),
        (
            "warning_codes",
            JsonValue::Array(
                items(&result.getattr("warning_codes")?)?
                    .iter()
                    .map(json_value)
                    .collect::<PyResult<_>>()?,
            ),
        ),
        (
            "thresholds",
            fields(
                &thresholds,
                &["maximum_lag_seconds", "minimum_staged_row_ratio"],
            )?,
        ),
        (
            "root_results",
            JsonValue::Array(
                items(&result.getattr("root_results")?)?
                    .iter()
                    .map(root_result_payload)
                    .collect::<PyResult<_>>()?,
            ),
        ),
        ("quality_check_results", JsonValue::Array(checks)),
    ]);
    json_text(&payload, Some(2))
}

fn root_lines(style: Style, root: &Any<'_>) -> PyResult<Vec<String>> {
    let assessment: String = shown_field(root, "assessment")?;
    let label = |name: &str| style.label(name);
    let mut lines: Vec<String> = vec![
        format!(
            "- {}",
            style.assessment_value(
                &shown(&root.getattr("root_key")?.getattr("name")?)?,
                &assessment,
                true
            )
        ),
        format!("  {}: {}", label("state"), shown_field(root, "state")?),
        format!(
            "  {}: {}",
            label("assessment"),
            style.assessment_value(&assessment, &assessment, false)
        ),
    ];
    let source: Any<'_> = root.getattr("replay_source_name")?;
    if !source.is_none() {
        lines.push(format!(
            "  {}: {}",
            label("replay source"),
            style.object_name(&shown(&source)?)
        ));
        lines.push(format!(
            "  {}: {}",
            label("replay source rows"),
            count(&root.getattr("replay_source_row_count")?)?
        ));
    }
    lines.push(format!(
        "  {}: {}",
        label("staged exists"),
        yes_no(&root.getattr("staged_exists")?)?
    ));
    lines.push(format!(
        "  {}: {}",
        label("active exists"),
        yes_no(&root.getattr("active_exists")?)?
    ));
    lines.push(format!(
        "  {}: {}",
        label("staged rows"),
        count(&root.getattr("staged_row_count")?)?
    ));
    lines.push(format!(
        "  {}: {}",
        label("active rows"),
        count(&root.getattr("active_row_count")?)?
    ));
    let row_delta: Any<'_> = root.getattr("row_delta")?;
    let row_ratio: Any<'_> = root.getattr("row_ratio")?;
    if !row_delta.is_none() && !row_ratio.is_none() {
        let mut delta: String = format!("{:+}", row_delta.extract::<i128>()?);
        let mut ratio: String = percentage(&row_ratio)?;
        if assessment != READY {
            delta = style.assessment_value(&delta, &assessment, false);
            ratio = style.assessment_value(&ratio, &assessment, false);
        }
        lines.push(format!("  {}: {delta}", label("row delta")));
        lines.push(format!("  {}: {ratio}", label("row ratio")));
    }
    let scalar: Any<'_> = root.getattr("scalar_catchup_summary")?;
    if !scalar.is_none() {
        lines.push(format!(
            "  {}: {}",
            label("staged range"),
            range(
                &scalar.getattr("staged_min_value")?,
                &scalar.getattr("staged_max_value")?
            )?
        ));
        lines.push(format!(
            "  {}: {}",
            label("active range"),
            range(
                &scalar.getattr("active_min_value")?,
                &scalar.getattr("active_max_value")?
            )?
        ));
        let lag: Any<'_> = scalar.getattr("lag_seconds")?;
        if !lag.is_none() {
            let seconds: Any<'_> = root.py().get_type::<PyInt>().call1((lag,))?;
            lines.push(format!("  {}: {}", label("lag seconds"), shown(&seconds)?));
        }
    }
    if root.getattr("staged_row_count")?.eq(0)? {
        lines.push(format!(
            "  {}: {}",
            label("warning"),
            style.warning("staged table is empty")
        ));
    }
    for warning in items(&root.getattr("warnings")?)? {
        lines.push(format!(
            "  {}: {}",
            label("warning"),
            style.warning(&shown(&warning)?)
        ));
    }
    Ok(lines)
}

fn quality_check_lines(
    py: Python<'_>,
    style: Style,
    results: &[Any<'_>],
    project_dir: &Any<'_>,
) -> PyResult<Vec<String>> {
    if results.is_empty() {
        return Ok(Vec::new());
    }
    let warning_severity: Any<'_> =
        attr(py, "sqltide.executor.auditing.types", "AuditSeverity")?.getattr("WARNING")?;
    let mut lines: Vec<String> = vec![String::new(), style.section("Quality Checks")];
    for audit in results {
        let passed: bool = audit.getattr("passed")?.is_truthy()?;
        let status: &str = if passed {
            "PASS"
        } else if audit.getattr("severity")?.eq(&warning_severity)? {
            "WARN"
        } else {
            "FAIL"
        };
        let mut display_name: String = display_path(&audit.getattr("file_path")?, project_dir)?;
        let name: Any<'_> = audit.getattr("name")?;
        if !name.is_none() {
            display_name = format!("{display_name}  [{}]", shown(&name)?);
        }
        lines.push(format!("- {status}  {display_name}"));
        let description: Any<'_> = audit.getattr("description")?;
        if !description.is_none() {
            lines.push(format!(
                "  {}: {}",
                style.label("description"),
                shown(&description)?
            ));
        }
        if !passed {
            lines.push(format!(
                "  {}: {}",
                style.label("failing rows"),
                count(&audit.getattr("failing_row_count")?)?
            ));
        }
    }
    Ok(lines)
}

fn audit_text(
    py: Python<'_>,
    style: Style,
    result: &Any<'_>,
    database: &Any<'_>,
    project_dir: &Any<'_>,
) -> PyResult<String> {
    let assessment: String = shown_field(result, "assessment")?;
    let thresholds: Any<'_> = result.getattr("thresholds")?;
    let mut lines: Vec<String> = vec![
        format!(
            "{}: {}",
            style.title("Audit"),
            style.assessment_value(&assessment, &assessment, false)
        ),
        style.label_value("Database", &shown(database)?),
        style.label_value("Deployment", &shown_field(result, "deployment_id")?),
        style.label_value(
            "Deployment status",
            &shown_field(result, "deployment_status")?,
        ),
        style.label_value(
            "Readiness thresholds",
            &format!(
                "maximum lag {}s, minimum staged rows {}",
                format_general(thresholds.getattr("maximum_lag_seconds")?.extract()?),
                percentage(&thresholds.getattr("minimum_staged_row_ratio")?)?
            ),
        ),
    ];
    let lineage: Any<'_> = result.getattr("replay_lineage_mode")?;
    if !lineage.is_none() {
        lines.push(style.label_value("Replay lineage", &shown(&lineage)?));
    }
    let warning_codes: Vec<String> = items(&result.getattr("warning_codes")?)?
        .iter()
        .map(|code| code.extract())
        .collect::<PyResult<_>>()?;
    if !warning_codes.is_empty() {
        lines.push(style.warning(&format!("Warnings: {}", names(&warning_codes))));
    }
    lines.push(String::new());
    lines.push(style.section("Roots"));
    let roots: Vec<Any<'_>> = items(&result.getattr("root_results")?)?;
    for root in &roots {
        lines.extend(root_lines(style, root)?);
    }
    lines.extend(quality_check_lines(
        py,
        style,
        &items(&result.getattr("quality_check_results")?)?,
        project_dir,
    )?);
    lines.push(String::new());
    lines.push(style.section("Next"));
    if assessment == READY {
        lines.push(format!(
            "- sqt deployment promote {}",
            shown_field(result, "deployment_id")?
        ));
        return Ok(lines.join("\n"));
    }
    let mut affected: Vec<String> = Vec::new();
    for root in &roots {
        if shown_field(root, "assessment")? != READY {
            affected.push(shown(&root.getattr("root_key")?.getattr("name")?)?);
        }
    }
    lines.push(if affected.is_empty() {
        "- investigate audit findings before publish".to_owned()
    } else {
        format!(
            "- investigate audit findings before publish, especially for {}",
            names(&affected)
        )
    });
    Ok(lines.join("\n"))
}

/// Render the deployment audit result as text or JSON.
#[pyfunction]
#[pyo3(signature = (*, result, database, json_output, project_dir=None))]
fn render_deployment_audit_result(
    py: Python<'_>,
    result: &Any<'_>,
    database: &Any<'_>,
    json_output: bool,
    project_dir: Option<Any<'_>>,
) -> PyResult<String> {
    let project_dir: Any<'_> = project_dir.unwrap_or_else(|| py.None().into_bound(py));
    if json_output {
        return audit_json(result, &project_dir);
    }
    audit_text(py, terminal_style(py)?, result, database, &project_dir)
}

/// Render the message shown when a deployment choice is ambiguous.
#[pyfunction]
#[pyo3(signature = (*, command_name, database, root_names, candidates))]
fn render_ambiguous_deployment_message(
    py: Python<'_>,
    command_name: &str,
    database: &Any<'_>,
    root_names: Vec<String>,
    candidates: &Any<'_>,
) -> PyResult<String> {
    ambiguous_message(
        terminal_style(py)?,
        command_name,
        &shown(database)?,
        &root_names,
        &items(candidates)?,
    )
}

/// Audit a staged backfill deployment and print the result payload.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, project_dir, database, metadata_database, deployment_id, json_output,
    client, loaded_project, adapter_profile,
))]
#[allow(clippy::too_many_arguments)]
fn run_deployment_audit(
    py: Python<'_>,
    pipelines_root: Any<'_>,
    project_dir: Any<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    deployment_id: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
    loaded_project: Any<'_>,
    adapter_profile: Any<'_>,
) -> PyResult<i64> {
    let metadata_database: Any<'_> = or(metadata_database, &database)?;
    let outcome = (|| -> PyResult<Result<Any<'_>, i64>> {
        let (compiled_pipelines, loaded_audits): (Vec<Any<'_>>, Vec<Any<'_>>) =
            if pipelines_root.is_none() {
                (Vec::new(), Vec::new())
            } else {
                let analysis: Any<'_> = call(
                    py,
                    NATIVE,
                    "analyze_project",
                    &[
                        ("pipelines_root", pipelines_root.clone()),
                        ("loaded_project", loaded_project.clone()),
                        ("adapter_profile", adapter_profile.clone()),
                    ],
                )?;
                let compiled: Any<'_> = analysis.getattr("compiled_project")?;
                (
                    items(&compiled.getattr("pipelines")?)?,
                    items(&compiled.getattr("audits")?)?,
                )
            };
        if deployment_id.is_none() {
            let candidates: Vec<Any<'_>> = items(&call(
                py,
                NATIVE,
                "build_audit_deployment_candidates",
                &[
                    ("client", client.clone()),
                    ("metadata_database", metadata_database.clone()),
                    ("default_database", database.clone()),
                ],
            )?)?;
            if let Some(message) = candidate_message(
                py,
                "deployment audit",
                &candidates,
                &client,
                &metadata_database,
                &database,
            )? {
                print_to(py, "stderr", &message)?;
                return Ok(Err(1));
            }
        }
        let request: Any<'_> = build(
            py,
            READINESS_MODELS,
            "DeploymentAuditRequest",
            &[
                ("deployment_id", deployment_id.clone()),
                ("metadata_database", metadata_database.clone()),
                ("default_database", database.clone()),
                ("thresholds", readiness_thresholds(py, &loaded_project)?),
            ],
        )?;
        let result: Any<'_> = call(
            py,
            NATIVE,
            "execute_deployment_audit",
            &[("request", request), ("client", client.clone())],
        )?;
        execute_audit_quality_checks(
            py,
            result,
            &project_dir,
            &compiled_pipelines,
            &loaded_audits,
            &client,
            &metadata_database,
            &database,
            adapter_profile.getattr("sql_analysis_dialect")?,
        )
        .map(Ok)
    })();
    let result: Any<'_> = match outcome {
        Ok(Ok(result)) => result,
        Ok(Err(code)) => return Ok(code),
        Err(error) => return reported(py, Err(error), "deployment audit", &database),
    };
    let rendered: String = render_deployment_audit_result(
        py,
        &result,
        &database,
        json_output.is_truthy()?,
        Some(project_dir),
    )?;
    print(py, &rendered)?;
    Ok(0)
}

// ---------------------------------------------------------------------------------------------
// Promotion

fn promotion_text(style: Style, result: &Any<'_>, database: &Any<'_>) -> PyResult<String> {
    let atomic = |name: &str| -> PyResult<&'static str> {
        Ok(if result.getattr(name)?.is_truthy()? {
            "atomic"
        } else {
            "not atomic"
        })
    };
    let mut lines: Vec<String> = vec![
        style.title("Promotion Complete"),
        style.label_value("Database", &shown(database)?),
        style.label_value("Deployment", &shown_field(result, "deployment_id")?),
        String::new(),
        style.section("Published views"),
    ];
    lines.extend(published_view_lines(result)?);
    lines.extend([
        String::new(),
        style.section("Atomicity"),
        format!(
            "- Each logical binding replacement: {}",
            atomic("per_relation_atomic_replace")?
        ),
        format!(
            "- Entire deployment promotion: {}",
            atomic("graph_atomic_publish")?
        ),
        "- Bindings are replaced one relation at a time".to_owned(),
        String::new(),
        style.section("Next"),
        "- audit the live logical views in ClickHouse if you want a post-promotion check"
            .to_owned(),
    ]);
    Ok(lines.join("\n"))
}

/// Render a deployment promotion result for CLI output.
#[pyfunction]
#[pyo3(signature = (*, result, database, json_output))]
fn render_promotion_result(
    py: Python<'_>,
    result: &Any<'_>,
    database: &Any<'_>,
    json_output: bool,
) -> PyResult<String> {
    if json_output {
        let views: Vec<JsonValue> = items(&result.getattr("published_views")?)?
            .iter()
            .map(|view| fields(view, &["view_name", "target_table_name"]))
            .collect::<PyResult<_>>()?;
        return json_text(
            &object(vec![
                (
                    "deployment_id",
                    json_value(&result.getattr("deployment_id")?)?,
                ),
                ("atomicity", atomicity_payload(result)?),
                ("published_views", JsonValue::Array(views)),
            ]),
            Some(2),
        );
    }
    promotion_text(terminal_style(py)?, result, database)
}

/// Promote a staged deployment and print the result payload.
#[pyfunction]
#[pyo3(signature = (*, database, metadata_database, deployment_id, json_output, client))]
fn run_deployment_promotion(
    py: Python<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    deployment_id: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let metadata_database: Any<'_> = or(metadata_database, &database)?;
    let outcome = (|| -> PyResult<Result<Any<'_>, i64>> {
        if deployment_id.is_none() {
            let candidates: Vec<Any<'_>> = items(&call(
                py,
                NATIVE,
                "build_publish_deployment_candidates",
                &[
                    ("client", client.clone()),
                    ("metadata_database", metadata_database.clone()),
                    ("default_database", database.clone()),
                ],
            )?)?;
            if let Some(message) = candidate_message(
                py,
                "deployment promote",
                &candidates,
                &client,
                &metadata_database,
                &database,
            )? {
                print_to(py, "stderr", &message)?;
                return Ok(Err(1));
            }
        }
        let request: Any<'_> = build(
            py,
            "sqltide.executor.promotion.models",
            "PublishRequest",
            &[
                ("deployment_id", deployment_id.clone()),
                ("metadata_database", metadata_database.clone()),
                ("default_database", database.clone()),
            ],
        )?;
        call(
            py,
            "sqltide._native",
            "execute_publish",
            &[("request", request), ("client", client.clone())],
        )
        .map(Ok)
    })();
    let result: Any<'_> = match outcome {
        Ok(Ok(result)) => result,
        Ok(Err(code)) => return Ok(code),
        Err(error) => return reported(py, Err(error), "deployment promote", &database),
    };
    let rendered: String =
        render_promotion_result(py, &result, &database, json_output.is_truthy()?)?;
    print(py, &rendered)?;
    Ok(0)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_deployment_audit_result, module)?)?;
    module.add_function(wrap_pyfunction!(
        render_ambiguous_deployment_message,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(render_promotion_result, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_audit, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_promotion, module)?)
}
