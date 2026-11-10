//! The warehouse operations commands: discover, doctor, active-view repair, janitor, deployment
//! inventory and comparison, rollback and reconcile. Each calls its executor and renders the
//! result as text or JSON.

use pyo3::prelude::*;
use sqltide_json::JsonValue;
use sqltide_planner::cli_style::CliStyle as Style;

use crate::cli::output::{
    confirmed, fields, joined, json_text, json_value, object, shown, shown_field,
};
use crate::cli::python::{print, print_to, raised_during, user_error};
use crate::cli::render::render_expected_warehouse_error;
use crate::cli::style::terminal_style;
use crate::dev_server::support::attr;
use crate::direct_build::{build, call, items, or};

const NATIVE: &str = "sqltide._native";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";

type Any<'py> = Bound<'py, PyAny>;

/// `module.name(**pairs)` of a Python collaborator.
fn run<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    pairs: &[(&str, Any<'py>)],
) -> PyResult<Any<'py>> {
    call(py, module, name, pairs)
}

/// The expected-warehouse-failure path every reporting command shares: print the rendered message
/// and fail, or re-raise any other error.
pub(crate) fn reported(
    py: Python<'_>,
    outcome: PyResult<i64>,
    command_name: &str,
    database: &Any<'_>,
) -> PyResult<i64> {
    let error: PyErr = match outcome {
        Ok(code) => return Ok(code),
        Err(error) => error,
    };
    if !error
        .value(py)
        .is_instance(&attr(py, ADAPTER_EXCEPTIONS, "AdapterWarehouseError")?)?
    {
        return Err(error);
    }
    let database: String = database.extract()?;
    match render_expected_warehouse_error(py, command_name, &database, error.value(py)) {
        Ok(Some(message)) => {
            print_to(py, "stderr", &message)?;
            Ok(1)
        }
        Ok(None) => Err(error),
        Err(raised) => Err(raised_during(py, raised, error)),
    }
}

/// Run direct-child pipeline directory discovery and print names.
#[pyfunction]
#[pyo3(signature = (*, pipelines_root, loaded_project, adapter_profile))]
fn run_discover(
    py: Python<'_>,
    pipelines_root: Any<'_>,
    loaded_project: Any<'_>,
    adapter_profile: Any<'_>,
) -> PyResult<i64> {
    let analysis: Any<'_> = run(
        py,
        NATIVE,
        "analyze_project",
        &[
            ("pipelines_root", pipelines_root),
            ("loaded_project", loaded_project),
            ("adapter_profile", adapter_profile),
        ],
    )?;
    let mut names: Vec<JsonValue> = Vec::new();
    for pipeline in items(&analysis.getattr("compiled_project")?.getattr("pipelines")?)? {
        names.push(json_value(&pipeline.getattr("pipeline")?.getattr("name")?)?);
    }
    print(py, &json_text(&JsonValue::Array(names), Some(2))?)?;
    Ok(0)
}

/// Run runtime diagnosis and print the result payload.
#[pyfunction]
#[pyo3(signature = (*, database, client))]
fn run_doctor(py: Python<'_>, database: Any<'_>, client: Any<'_>) -> PyResult<i64> {
    let outcome = (|| -> PyResult<i64> {
        let request: Any<'_> = build(
            py,
            "sqltide.executor.doctor.models",
            "DoctorRequest",
            &[("default_database", database.clone())],
        )?;
        let result: Any<'_> = run(
            py,
            "sqltide.executor.doctor.main.execute_doctor",
            "execute_doctor",
            &[("request", request), ("client", client)],
        )?;
        let mut views: Vec<JsonValue> = Vec::new();
        for status in items(&result.getattr("active_views")?)? {
            let candidates: Vec<JsonValue> = items(&status.getattr("candidate_deployment_ids")?)?
                .iter()
                .map(json_value)
                .collect::<PyResult<_>>()?;
            views.push(object(vec![
                ("table_name", json_value(&status.getattr("table_name")?)?),
                ("state_kind", json_value(&status.getattr("state_kind")?)?),
                (
                    "active_deployment_id",
                    json_value(&status.getattr("active_deployment_id")?)?,
                ),
                ("candidate_deployment_ids", JsonValue::Array(candidates)),
            ]));
        }
        print(
            py,
            &json_text(
                &object(vec![("active_views", JsonValue::Array(views))]),
                Some(2),
            )?,
        )?;
        Ok(0)
    })();
    reported(py, outcome, "doctor", &database)
}

/// Repair a stable active view by rebinding it to a chosen deployment table.
#[pyfunction]
#[pyo3(signature = (*, database, table, deployment_id, client))]
fn run_repair_active_view(
    py: Python<'_>,
    database: Any<'_>,
    table: Any<'_>,
    deployment_id: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let outcome = (|| -> PyResult<i64> {
        let request: Any<'_> = build(
            py,
            "sqltide.executor.repair.models",
            "RepairActiveViewRequest",
            &[
                ("default_database", database.clone()),
                ("table_name", table),
                ("deployment_id", deployment_id),
            ],
        )?;
        let result: Any<'_> = run(
            py,
            "sqltide.executor.repair.main.execute_repair_active_view",
            "execute_repair_active_view",
            &[("request", request), ("client", client)],
        )?;
        print(
            py,
            &json_text(
                &fields(&result, &["table_name", "target_table_name"])?,
                Some(2),
            )?,
        )?;
        Ok(0)
    })();
    reported(py, outcome, "repair active-view", &database)
}

/// The janitor preview or apply report.
fn render_janitor_result(result: &Any<'_>, apply: bool, json_output: bool) -> PyResult<String> {
    let header = |title: &str| -> PyResult<String> {
        Ok(format!(
            "{title}\nDatabase: {}\nRetention days: {}\nMinimum rollback deployments: {}\n",
            shown_field(result, "database")?,
            shown_field(result, "retention_days")?,
            shown_field(result, "minimum_rollback_deployments")?,
        ))
    };
    if json_output {
        let mut payload: Vec<(&str, JsonValue)> = Vec::new();
        for name in ["database", "retention_days", "minimum_rollback_deployments"] {
            payload.push((name, json_value(&result.getattr(name)?)?));
        }
        if apply {
            for name in ["deleted_deployment_ids", "deleted_object_names"] {
                payload.push((name, json_value(&result.getattr(name)?)?));
            }
        } else {
            let candidates: Vec<JsonValue> = items(&result.getattr("candidates")?)?
                .iter()
                .map(|candidate| json_value(&candidate.getattr("__dict__")?))
                .collect::<PyResult<_>>()?;
            payload.push(("candidates", JsonValue::Array(candidates)));
        }
        return json_text(&object(payload), Some(2));
    }
    if apply {
        let mut lines: Vec<String> = vec![header("Janitor Apply")?];
        let deployment_ids: Vec<Any<'_>> = items(&result.getattr("deleted_deployment_ids")?)?;
        if deployment_ids.is_empty() {
            lines.push("Deleted deployments:\n- none".to_owned());
            return Ok(lines.join("\n"));
        }
        lines.push("Deleted deployments:".to_owned());
        for deployment_id in &deployment_ids {
            lines.push(format!("- {}", shown(deployment_id)?));
        }
        lines.push("\nDeleted physical objects:".to_owned());
        for object_name in items(&result.getattr("deleted_object_names")?)? {
            lines.push(format!("- {}", shown(&object_name)?));
        }
        return Ok(lines.join("\n"));
    }
    let mut lines: Vec<String> = vec![header("Janitor Preview")?];
    let candidates: Vec<Any<'_>> = items(&result.getattr("candidates")?)?;
    let mut deletable: Vec<&Any<'_>> = Vec::new();
    let mut kept: Vec<&Any<'_>> = Vec::new();
    for candidate in &candidates {
        if candidate.getattr("deletable")?.is_truthy()? {
            deletable.push(candidate);
        } else {
            kept.push(candidate);
        }
    }
    let describe = |lines: &mut Vec<String>, candidate: &Any<'_>| -> PyResult<()> {
        lines.push(format!("- {}", shown_field(candidate, "deployment_id")?));
        lines.push(format!(
            "  created at: {}",
            shown_field(candidate, "created_at")?
        ));
        if candidate.getattr("logical_view_names")?.is_truthy()? {
            lines.push(format!(
                "  roots: {}",
                joined(candidate, "logical_view_names")?
            ));
        }
        lines.push(format!("  reason: {}", shown_field(candidate, "reason")?));
        Ok(())
    };
    lines.push("Deletable deployments:".to_owned());
    if deletable.is_empty() {
        lines.push("- none".to_owned());
    }
    for candidate in deletable {
        describe(&mut lines, candidate)?;
        if candidate.getattr("physical_object_names")?.is_truthy()? {
            lines.push(format!(
                "  physical objects: {}",
                joined(candidate, "physical_object_names")?
            ));
        }
    }
    lines.push("\nKept deployments:".to_owned());
    if kept.is_empty() {
        lines.push("- none".to_owned());
    }
    for candidate in kept {
        describe(&mut lines, candidate)?;
    }
    Ok(lines.join("\n"))
}

/// Preview or apply janitor cleanup and print the report.
#[pyfunction]
#[pyo3(signature = (*, database, retention_days, minimum_rollback_deployments, apply, json_output, client))]
#[allow(clippy::too_many_arguments)]
fn run_janitor(
    py: Python<'_>,
    database: Any<'_>,
    retention_days: Any<'_>,
    minimum_rollback_deployments: Any<'_>,
    apply: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let request: Any<'_> = build(
        py,
        "sqltide.executor.janitor.models",
        "JanitorRequest",
        &[
            ("database", database.clone()),
            ("metadata_database", database),
            ("retention_days", retention_days),
            ("apply", apply.clone()),
            ("minimum_rollback_deployments", minimum_rollback_deployments),
        ],
    )?;
    let result: Any<'_> = run(
        py,
        "sqltide._native",
        "execute_janitor",
        &[("request", request), ("client", client)],
    )?;
    print(
        py,
        &render_janitor_result(&result, apply.is_truthy()?, json_output.is_truthy()?)?,
    )?;
    Ok(0)
}

const DEPLOYMENT_FIELDS: [&str; 9] = [
    "deployment_id",
    "state",
    "created_at",
    "persisted_status",
    "root_names",
    "physical_relation_names",
    "missing_physical_relation_names",
    "active_binding_names",
    "latest_published_at",
];

fn deployment_payload(deployment: &Any<'_>) -> PyResult<Vec<(&'static str, JsonValue)>> {
    let mut payload: Vec<(&str, JsonValue)> = Vec::with_capacity(DEPLOYMENT_FIELDS.len());
    for name in DEPLOYMENT_FIELDS {
        let value: Any<'_> = if name == "state" {
            deployment.getattr("state")?.getattr("value")?
        } else {
            deployment.getattr(name)?
        };
        payload.push((name, json_value(&value)?));
    }
    Ok(payload)
}

/// `value or fallback`, shown.
fn or_shown(value: &Any<'_>, fallback: &str) -> PyResult<String> {
    if value.is_truthy()? {
        shown(value)
    } else {
        Ok(fallback.to_owned())
    }
}

/// Render a complete deployment inventory.
fn render_deployment_inventory(inventory: &Any<'_>, json_output: bool) -> PyResult<String> {
    let deployments: Vec<Any<'_>> = items(&inventory.getattr("deployments")?)?;
    if json_output {
        let payloads: Vec<JsonValue> = deployments
            .iter()
            .map(|deployment| deployment_payload(deployment).map(object))
            .collect::<PyResult<_>>()?;
        return json_text(
            &object(vec![
                ("database", json_value(&inventory.getattr("database")?)?),
                ("deployments", JsonValue::Array(payloads)),
            ]),
            Some(2),
        );
    }
    let mut lines: Vec<String> = vec![
        "Deployments".to_owned(),
        format!("Database: {}", shown_field(inventory, "database")?),
    ];
    if deployments.is_empty() {
        lines.push("\n- none".to_owned());
        return Ok(lines.join("\n"));
    }
    for deployment in &deployments {
        lines.push(String::new());
        lines.push(format!("- {}", shown_field(deployment, "deployment_id")?));
        lines.push(format!(
            "  state: {}",
            shown(&deployment.getattr("state")?.getattr("value")?)?
        ));
        if !deployment.getattr("created_at")?.is_none() {
            lines.push(format!(
                "  created at: {}",
                shown_field(deployment, "created_at")?
            ));
        }
        if deployment.getattr("root_names")?.is_truthy()? {
            lines.push(format!("  roots: {}", joined(deployment, "root_names")?));
        }
    }
    Ok(lines.join("\n"))
}

/// Render one deployment summary.
fn render_deployment(
    deployment: &Any<'_>,
    database: &Any<'_>,
    json_output: bool,
) -> PyResult<String> {
    if json_output {
        let mut payload: Vec<(&str, JsonValue)> = vec![("database", json_value(database)?)];
        payload.extend(deployment_payload(deployment)?);
        return json_text(&object(payload), Some(2));
    }
    let listed = |name: &str| -> PyResult<Vec<String>> {
        items(&deployment.getattr(name)?)?
            .iter()
            .map(|value| Ok(format!("- {}", shown(value)?)))
            .collect()
    };
    let roots: String = if deployment.getattr("root_names")?.is_truthy()? {
        joined(deployment, "root_names")?
    } else {
        "none".to_owned()
    };
    let mut lines: Vec<String> = vec![
        "Deployment".to_owned(),
        format!("Database: {}", shown(database)?),
        format!(
            "Deployment ID: {}",
            shown_field(deployment, "deployment_id")?
        ),
        format!(
            "State: {}",
            shown(&deployment.getattr("state")?.getattr("value")?)?
        ),
        format!(
            "Created at: {}",
            or_shown(&deployment.getattr("created_at")?, "unknown")?
        ),
        format!(
            "Persisted status: {}",
            or_shown(&deployment.getattr("persisted_status")?, "missing")?
        ),
        format!("Roots: {roots}"),
        "Physical relations:".to_owned(),
    ];
    lines.extend(listed("physical_relation_names")?);
    lines.push("Missing physical relations:".to_owned());
    lines.extend(listed("missing_physical_relation_names")?);
    lines.push("Active bindings:".to_owned());
    lines.extend(listed("active_binding_names")?);
    lines.push(format!(
        "Latest publication: {}",
        or_shown(&deployment.getattr("latest_published_at")?, "none")?
    ));
    Ok(lines.join("\n"))
}

/// Print authoritative deployment inventory.
#[pyfunction]
#[pyo3(signature = (*, database, metadata_database, json_output, client))]
fn run_deployment_list(
    py: Python<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let inventory: Any<'_> = run(
        py,
        "sqltide._native",
        "load_deployments",
        &[
            ("client", client),
            ("metadata_database", or(metadata_database, &database)?),
            ("default_database", database),
        ],
    )?;
    print(
        py,
        &render_deployment_inventory(&inventory, json_output.is_truthy()?)?,
    )?;
    Ok(0)
}

/// Print one authoritative deployment summary.
#[pyfunction]
#[pyo3(signature = (*, deployment_id, database, metadata_database, json_output, client))]
fn run_deployment_show(
    py: Python<'_>,
    deployment_id: Any<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let deployment: Any<'_> = run(
        py,
        "sqltide._native",
        "load_deployment",
        &[
            ("deployment_id", deployment_id),
            ("client", client),
            ("metadata_database", or(metadata_database, &database)?),
            ("default_database", database.clone()),
        ],
    )?;
    print(
        py,
        &render_deployment(&deployment, &database, json_output.is_truthy()?)?,
    )?;
    Ok(0)
}

fn diff_count(value: &Any<'_>) -> PyResult<String> {
    if value.is_none() {
        Ok("missing".to_owned())
    } else {
        shown(value)
    }
}

fn diff_columns(columns: &Any<'_>) -> PyResult<String> {
    let columns: Vec<Any<'_>> = items(columns)?;
    if columns.is_empty() {
        return Ok("missing".to_owned());
    }
    Ok(columns
        .iter()
        .map(|column| {
            Ok(format!(
                "{} {}",
                shown_field(column, "name")?,
                shown_field(column, "type")?
            ))
        })
        .collect::<PyResult<Vec<String>>>()?
        .join(", "))
}

fn relation_payload(relation: &Any<'_>) -> PyResult<JsonValue> {
    let columns = |name: &str| -> PyResult<JsonValue> {
        items(&relation.getattr(name)?)?
            .iter()
            .map(|column| json_value(&column.getattr("__dict__")?))
            .collect::<PyResult<_>>()
            .map(JsonValue::Array)
    };
    Ok(object(vec![
        ("database", json_value(&relation.getattr("database")?)?),
        (
            "logical_name",
            json_value(&relation.getattr("logical_name")?)?,
        ),
        (
            "status",
            JsonValue::String(shown_field(relation, "status")?),
        ),
        (
            "from_physical_name",
            json_value(&relation.getattr("from_physical_name")?)?,
        ),
        (
            "to_physical_name",
            json_value(&relation.getattr("to_physical_name")?)?,
        ),
        ("from_columns", columns("from_columns")?),
        ("to_columns", columns("to_columns")?),
        (
            "from_row_count",
            json_value(&relation.getattr("from_row_count")?)?,
        ),
        (
            "to_row_count",
            json_value(&relation.getattr("to_row_count")?)?,
        ),
    ]))
}

/// Render one deployment comparison as text or JSON.
fn render_deployment_diff(style: Style, result: &Any<'_>, json_output: bool) -> PyResult<String> {
    let relations: Vec<Any<'_>> = items(&result.getattr("relations")?)?;
    if json_output {
        return json_text(
            &object(vec![
                ("database", json_value(&result.getattr("database")?)?),
                ("from", json_value(&result.getattr("from_endpoint")?)?),
                ("to", json_value(&result.getattr("to_endpoint")?)?),
                (
                    "relations",
                    JsonValue::Array(
                        relations
                            .iter()
                            .map(relation_payload)
                            .collect::<PyResult<_>>()?,
                    ),
                ),
            ]),
            Some(2),
        );
    }
    let mut lines: Vec<String> = vec![
        style.title("Deployment Diff"),
        style.label_value("Database", &shown_field(result, "database")?),
        style.label_value(
            "Comparison",
            &format!(
                "{}:{}",
                shown_field(result, "from_endpoint")?,
                shown_field(result, "to_endpoint")?
            ),
        ),
        String::new(),
        style.section("Relations"),
    ];
    if relations.is_empty() {
        lines.push("- none".to_owned());
        return Ok(lines.join("\n"));
    }
    for relation in &relations {
        lines.push(format!(
            "- {}.{}: {}",
            shown_field(relation, "database")?,
            shown_field(relation, "logical_name")?,
            shown_field(relation, "status")?
        ));
        lines.push(format!(
            "  rows: {} -> {}",
            diff_count(&relation.getattr("from_row_count")?)?,
            diff_count(&relation.getattr("to_row_count")?)?
        ));
        let from_columns: Any<'_> = relation.getattr("from_columns")?;
        let to_columns: Any<'_> = relation.getattr("to_columns")?;
        if from_columns.ne(&to_columns)? {
            lines.push(format!(
                "  columns: {} -> {}",
                diff_columns(&from_columns)?,
                diff_columns(&to_columns)?
            ));
        }
    }
    Ok(lines.join("\n"))
}

/// Compare two deployment endpoints and print their differences.
#[pyfunction]
#[pyo3(signature = (*, database, metadata_database, comparison, json_output, client))]
fn run_deployment_diff(
    py: Python<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    comparison: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let request: Any<'_> = build(
        py,
        "sqltide.executor.deployment.models",
        "DeploymentDiffRequest",
        &[
            ("database", database.clone()),
            ("metadata_database", or(metadata_database, &database)?),
            ("comparison", comparison),
        ],
    )?;
    let result: Any<'_> = run(
        py,
        "sqltide._native",
        "execute_deployment_diff",
        &[("request", request), ("client", client)],
    )?;
    let rendered: String =
        render_deployment_diff(terminal_style(py)?, &result, json_output.is_truthy()?)?;
    print(py, &rendered)?;
    Ok(0)
}

/// The published views and atomicity of one rollback or promotion.
fn published_views_payload(result: &Any<'_>) -> PyResult<JsonValue> {
    items(&result.getattr("published_views")?)?
        .iter()
        .map(|view| fields(view, &["view_name", "target_table_name"]))
        .collect::<PyResult<_>>()
        .map(JsonValue::Array)
}

pub(crate) fn atomicity_payload(result: &Any<'_>) -> PyResult<JsonValue> {
    fields(
        result,
        &["per_relation_atomic_replace", "graph_atomic_publish"],
    )
}

pub(crate) fn published_view_lines(result: &Any<'_>) -> PyResult<Vec<String>> {
    items(&result.getattr("published_views")?)?
        .iter()
        .map(|view| {
            Ok(format!(
                "- {} -> {}",
                shown_field(view, "view_name")?,
                shown_field(view, "target_table_name")?
            ))
        })
        .collect()
}

/// Render the binding-level rollback confirmation summary.
fn render_rollback_plan(style: Style, plan: &Any<'_>, database: &Any<'_>) -> PyResult<String> {
    let mut lines: Vec<String> = vec![
        style.title("Rollback Preview"),
        style.label_value("Database", &shown(database)?),
        style.label_value(
            "Current deployment",
            &shown_field(plan, "current_deployment_id")?,
        ),
        style.label_value(
            "Target deployment",
            &shown_field(plan, "target_deployment_id")?,
        ),
        String::new(),
        style.section("Stable bindings"),
    ];
    for view_name in items(&plan.getattr("logical_view_names")?)? {
        lines.push(format!("- {}", shown(&view_name)?));
    }
    Ok(lines.join("\n"))
}

/// Render one completed rollback.
fn render_rollback_result(
    style: Style,
    result: &Any<'_>,
    database: &Any<'_>,
    json_output: bool,
) -> PyResult<String> {
    if json_output {
        return json_text(
            &object(vec![
                ("operation", json_value(&result.getattr("operation")?)?),
                ("database", json_value(database)?),
                (
                    "previous_deployment_id",
                    json_value(&result.getattr("previous_deployment_id")?)?,
                ),
                (
                    "deployment_id",
                    json_value(&result.getattr("deployment_id")?)?,
                ),
                ("atomicity", atomicity_payload(result)?),
                ("published_views", published_views_payload(result)?),
            ]),
            Some(2),
        );
    }
    let mut lines: Vec<String> = vec![
        style.title("Rollback Complete"),
        style.label_value("Database", &shown(database)?),
        style.label_value(
            "Previous deployment",
            &or_shown(&result.getattr("previous_deployment_id")?, "unknown")?,
        ),
        style.label_value("Active deployment", &shown_field(result, "deployment_id")?),
        String::new(),
        style.section("Published views"),
    ];
    lines.extend(published_view_lines(result)?);
    lines.extend([
        String::new(),
        style.section("Atomicity"),
        "- Bindings were replaced one relation at a time".to_owned(),
        format!(
            "- Entire rollback graph atomic: {}",
            shown_field(result, "graph_atomic_publish")?
        ),
    ]);
    Ok(lines.join("\n"))
}

/// Resolve, confirm, and execute one whole-deployment rollback.
#[pyfunction]
#[pyo3(signature = (*, database, metadata_database, deployment_id, previous, auto_approve, json_output, client))]
#[allow(clippy::too_many_arguments)]
fn run_deployment_rollback(
    py: Python<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    deployment_id: Any<'_>,
    previous: Any<'_>,
    auto_approve: Any<'_>,
    json_output: Any<'_>,
    client: Any<'_>,
) -> PyResult<i64> {
    let json_output: bool = json_output.is_truthy()?;
    let auto_approve: bool = auto_approve.is_truthy()?;
    if json_output && !auto_approve {
        return Err(user_error(
            py,
            "deployment rollback --json requires --auto-approve",
            None,
        ));
    }
    let metadata_database: Any<'_> = or(metadata_database, &database)?;
    let request: Any<'_> = build(
        py,
        "sqltide.executor.promotion.models",
        "RollbackRequest",
        &[
            ("deployment_id", deployment_id),
            ("previous", previous),
            ("metadata_database", metadata_database.clone()),
            ("default_database", database.clone()),
        ],
    )?;
    let plan: Any<'_> = run(
        py,
        NATIVE,
        "resolve_deployment_rollback",
        &[("request", request), ("client", client.clone())],
    )?;
    if !json_output {
        print(
            py,
            &render_rollback_plan(terminal_style(py)?, &plan, &database)?,
        )?;
    }
    if !auto_approve && !confirmed(py, "Proceed with rollback? [y/N] ")? {
        print(py, "Rollback cancelled.")?;
        return Ok(1);
    }
    let request: Any<'_> = build(
        py,
        "sqltide.executor.promotion.models",
        "PublishRequest",
        &[
            ("deployment_id", plan.getattr("target_deployment_id")?),
            ("metadata_database", metadata_database),
            ("default_database", database.clone()),
            ("operation", "rollback".into_pyobject(py)?.into_any()),
            (
                "previous_deployment_id",
                plan.getattr("current_deployment_id")?,
            ),
        ],
    )?;
    let result: Any<'_> = run(
        py,
        "sqltide.executor.promotion.main.execute_deployment_promotion",
        "execute_publish",
        &[("request", request), ("client", client)],
    )?;
    let rendered: String =
        render_rollback_result(terminal_style(py)?, &result, &database, json_output)?;
    print(py, &rendered)?;
    Ok(0)
}

/// `sorted({record.key.name for record in records})`.
fn record_names(records: &Any<'_>) -> PyResult<Vec<String>> {
    let mut names: Vec<String> = items(records)?
        .iter()
        .map(|record| record.getattr("key")?.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    names.sort();
    names.dedup();
    Ok(names)
}

fn rejected_targets_payload(value: &Any<'_>) -> PyResult<JsonValue> {
    items(&value.getattr("rejected_targets")?)?
        .iter()
        .map(|target| {
            let reasons: Vec<JsonValue> = items(&target.getattr("reasons")?)?
                .iter()
                .map(json_value)
                .collect::<PyResult<_>>()?;
            Ok(object(vec![
                ("target_name", json_value(&target.getattr("target_name")?)?),
                ("reasons", JsonValue::Array(reasons)),
            ]))
        })
        .collect::<PyResult<_>>()
        .map(JsonValue::Array)
}

fn names_value(names: Vec<String>) -> JsonValue {
    JsonValue::Array(names.into_iter().map(JsonValue::String).collect())
}

fn render_reconcile_preview(preview: &Any<'_>, json_output: bool) -> PyResult<String> {
    let names: Vec<String> = record_names(&preview.getattr("eligible_records")?)?;
    if json_output {
        return json_text(
            &object(vec![
                ("database", json_value(&preview.getattr("database")?)?),
                (
                    "reconcile_id",
                    json_value(&preview.getattr("reconcile_id")?)?,
                ),
                ("eligible_target_names", names_value(names)),
                ("rejected_targets", rejected_targets_payload(preview)?),
            ]),
            None,
        );
    }
    let mut lines: Vec<String> = vec![
        format!(
            "Reconcile Preview\nDatabase: {}",
            shown_field(preview, "database")?
        ),
        format!("Eligible targets: {}", names.len()),
    ];
    lines.extend(names.iter().map(|name| format!("- {name}")));
    let rejected: Vec<Any<'_>> = items(&preview.getattr("rejected_targets")?)?;
    if !rejected.is_empty() {
        lines.push("Rejected targets:".to_owned());
        for target in &rejected {
            lines.push(format!(
                "- {}: {}",
                shown_field(target, "target_name")?,
                joined(target, "reasons")?
            ));
        }
    }
    Ok(lines.join("\n"))
}

fn render_reconcile_result(result: &Any<'_>, json_output: bool) -> PyResult<String> {
    let names: Vec<String> = record_names(&result.getattr("reconciled_records")?)?;
    if json_output {
        return json_text(
            &object(vec![
                ("database", json_value(&result.getattr("database")?)?),
                (
                    "reconcile_id",
                    json_value(&result.getattr("reconcile_id")?)?,
                ),
                ("reconciled_target_names", names_value(names)),
                ("rejected_targets", rejected_targets_payload(result)?),
            ]),
            None,
        );
    }
    Ok(format!(
        "Reconcile Applied\nDatabase: {}\nReconcile id: {}\nReconciled targets: {}",
        shown_field(result, "database")?,
        shown_field(result, "reconcile_id")?,
        names.len()
    ))
}

/// Preview metadata reconciliation for the selected models and apply it when confirmed.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, database, metadata_database, selectors, json_output, apply, client,
    loaded_project, adapter_profile,
))]
#[allow(clippy::too_many_arguments)]
fn run_reconcile(
    py: Python<'_>,
    pipelines_root: Any<'_>,
    database: Any<'_>,
    metadata_database: Any<'_>,
    selectors: Any<'_>,
    json_output: Any<'_>,
    apply: Any<'_>,
    client: Any<'_>,
    loaded_project: Any<'_>,
    adapter_profile: Any<'_>,
) -> PyResult<i64> {
    let analysis: Any<'_> = run(
        py,
        NATIVE,
        "analyze_project",
        &[
            ("pipelines_root", pipelines_root),
            ("loaded_project", loaded_project),
            ("adapter_profile", adapter_profile),
        ],
    )?;
    let pipelines: Any<'_> = pyo3::types::PyList::new(
        py,
        items(&analysis.getattr("compile_inputs")?.getattr("pipelines")?)?,
    )?
    .into_any();
    let database: Any<'_> =
        crate::cli::invocation::resolve_default_database(py, &pipelines, database)?;
    let metadata_database: Any<'_> = or(metadata_database, &database)?;
    let selection: Any<'_> = run(
        py,
        NATIVE,
        "resolve_selection",
        &[
            ("realized_project", analysis.getattr("realized_project")?),
            ("graph", analysis.getattr("graph")?),
            ("selectors", selectors),
        ],
    )?;
    let desired_state: Any<'_> = selection.getattr("desired_state")?;
    let selected: Any<'_> = selection.getattr("selected_model_keys")?;
    let virtual_environments: bool = analysis
        .getattr("compile_inputs")?
        .getattr("virtual_environments")?
        .is_truthy()?;
    let actual_state: Option<Any<'_>> = if virtual_environments {
        Some(run(
            py,
            NATIVE,
            "load_actual_state",
            &[
                ("client", client.clone()),
                ("desired_state", desired_state.clone()),
                ("database", database.clone()),
            ],
        )?)
    } else {
        None
    };
    let reconcile = |apply: bool| -> PyResult<Any<'_>> {
        let mut pairs: Vec<(&str, Any<'_>)> = vec![
            ("client", client.clone()),
            ("target_database", database.clone()),
            ("metadata_database", metadata_database.clone()),
            ("desired_state", desired_state.clone()),
        ];
        let (module, name) = match &actual_state {
            Some(actual_state) => {
                pairs.push(("actual_state", actual_state.clone()));
                pairs.push(("selected_model_keys", selected.clone()));
                (
                    "sqltide.executor.reconcile.main.execute_reconcile",
                    "execute_reconcile",
                )
            }
            None => {
                pairs.push((
                    "catalog",
                    client.call_method1("load_catalog", (&database,))?,
                ));
                pairs.push((
                    "models",
                    analysis
                        .getattr("realized_project")?
                        .getattr("project")?
                        .getattr("models")?,
                ));
                pairs.push(("selected_model_keys", selected.clone()));
                pairs.push((
                    "tool_version",
                    attr(py, "sqltide.cli.build.constants", "SQLTIDE_TOOL_VERSION")?,
                ));
                (
                    "sqltide.executor.reconcile.main.execute_direct_reconcile",
                    "execute_direct_reconcile",
                )
            }
        };
        if apply {
            pairs.push(("apply", true.into_pyobject(py)?.to_owned().into_any()));
        }
        run(py, module, name, &pairs)
    };
    let json_output: bool = json_output.is_truthy()?;
    print(
        py,
        &render_reconcile_preview(&reconcile(false)?, json_output)?,
    )?;
    if !apply.is_truthy()? {
        return Ok(0);
    }
    if !confirmed(py, "Proceed with reconcile? [y/N] ")? {
        print(py, "Reconcile cancelled.")?;
        return Ok(1);
    }
    print(
        py,
        &render_reconcile_result(&reconcile(true)?, json_output)?,
    )?;
    Ok(0)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_discover, module)?)?;
    module.add_function(wrap_pyfunction!(run_doctor, module)?)?;
    module.add_function(wrap_pyfunction!(run_repair_active_view, module)?)?;
    module.add_function(wrap_pyfunction!(run_janitor, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_list, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_show, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_diff, module)?)?;
    module.add_function(wrap_pyfunction!(run_deployment_rollback, module)?)?;
    module.add_function(wrap_pyfunction!(run_reconcile, module)?)
}
