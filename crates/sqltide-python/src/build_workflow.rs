//! Connected workflow preparation shared by `sqt plan`, `sqt build` and the dev server: flag
//! validation, routing a selection to its direct, virtual or mixed preparation, assembling each
//! workflow once from a fresh preview, the configured pipeline limit, and the exact logical scope
//! a dev-server child must reproduce before it writes. Native planners are looked up on
//! `sqltide._native` when called; warehouse reads go through the Python adapter connection.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PyString};

use crate::artifacts::json_dumps;
use crate::build_cli::{build_protection_requirements, formatted};
use crate::direct_build::{Value, boolean, build, call, class, is_a, items, none, replace, tuple};
use crate::model_sql::chained;

const NATIVE: &str = "sqltide._native";
const CLI_STYLE: &str = "sqltide.cli.presentation.main._cli_style";
const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";
const BUILD_CONSTANTS: &str = "sqltide.cli.build.constants";
const BUILD_MODELS: &str = "sqltide.cli.build.models";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const DIRECT_MODELS: &str = "sqltide.executor.direct.models";
const BACKFILL_MODELS: &str = "sqltide.executor.backfill.models";
const DIRECT: &str = "direct";
const VIRTUAL: &str = "virtual";
const INVALID_SCOPE: &str = "Authorized build scope is invalid; re-plan the build";

fn native<'py>(py: Python<'py>, name: &str, pairs: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
    call(py, NATIVE, name, pairs)
}

fn user_error(py: Python<'_>, message: &str) -> PyErr {
    match class(py, CLI_EXCEPTIONS, "CliUserError").and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn truthy(value: &Value<'_>, name: &str) -> PyResult<bool> {
    value.getattr(name)?.is_truthy()
}

fn use_color(py: Python<'_>) -> PyResult<bool> {
    py.import(CLI_STYLE)?
        .getattr("cli_style")?
        .call0()?
        .getattr("use_color")?
        .is_truthy()
}

fn json_loads<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Value<'py>> {
    py.import("json")?.getattr("loads")?.call1((value,))
}

fn strings<'py>(py: Python<'py>, values: &[String]) -> PyResult<Value<'py>> {
    tuple(
        py,
        values
            .iter()
            .map(|value| PyString::new(py, value).into_any())
            .collect(),
    )
}

/// Return one complete workflow assembled from fresh connected inspection.
#[pyfunction]
#[pyo3(signature = (*, analysis, options, client, adapter_profile))]
fn prepare_build_workflow<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    validate_common_flags(py, options)?;
    reject_replayless_start_time(py, analysis, options)?;
    let start_time: Value<'py> = match options.getattr("start_time")? {
        start_time if start_time.is_none() => none(py),
        start_time => py
            .import(NATIVE)?
            .getattr("normalize_cli_start_time")?
            .call1((start_time,))?,
    };
    if truthy(options, "changed")? {
        reject_direct_unsupported_flags(py, options)?;
        return prepare_direct(py, analysis, options, client, adapter_profile, &start_time);
    }
    let names_by_mode: Vec<(String, Vec<String>)> =
        selected_model_names_by_mode(py, analysis, &options.getattr("selectors")?)?;
    let names = |mode: &str| -> Option<&Vec<String>> {
        names_by_mode
            .iter()
            .find(|(selected, _)| selected == mode)
            .map(|(_, names)| names)
    };
    match (names(DIRECT), names(VIRTUAL)) {
        (None, Some(_)) => {
            if truthy(options, "include_missing_upstream")? {
                return Err(user_error(
                    py,
                    "--include-missing-upstream is only supported for direct models",
                ));
            }
            return prepare_virtual(py, analysis, options, &start_time, client);
        }
        (Some(_), None) => {
            reject_direct_unsupported_flags(py, options)?;
            return prepare_direct(py, analysis, options, client, adapter_profile, &start_time);
        }
        (Some(direct_names), Some(virtual_names)) => {
            let virtual_options: Value<'py> = replace(
                options,
                &[
                    ("selectors", strings(py, virtual_names)?),
                    ("include_missing_upstream", boolean(py, false)?),
                ],
            )?;
            let direct_options: Value<'py> = replace(
                options,
                &[
                    ("selectors", strings(py, direct_names)?),
                    ("deployment_id", none(py)),
                    ("full_refresh", boolean(py, false)?),
                ],
            )?;
            let virtual_: Value<'py> =
                prepare_virtual(py, analysis, &virtual_options, &start_time, client)?;
            let direct: Value<'py> = prepare_direct(
                py,
                analysis,
                &direct_options,
                client,
                adapter_profile,
                &start_time,
            )?;
            return mixed_preparation(py, virtual_, direct);
        }
        (None, None) => {}
    }
    if analysis
        .getattr("compile_inputs")?
        .getattr("virtual_environments")?
        .is_truthy()?
    {
        return prepare_virtual(py, analysis, options, &start_time, client);
    }
    reject_direct_unsupported_flags(py, options)?;
    prepare_direct(py, analysis, options, client, adapter_profile, &start_time)
}

/// Selected model names per pipeline mode, each sorted, in first-seen mode order.
fn selected_model_names_by_mode<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    selectors: &Value<'py>,
) -> PyResult<Vec<(String, Vec<String>)>> {
    let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
    let selected_keys: Value<'py> = if selectors.is_truthy()? {
        native(
            py,
            "resolve_selected_logical_model_keys",
            &[
                ("compiled_pipelines", compiled_project.getattr("pipelines")?),
                ("selectors", selectors.clone()),
            ],
        )?
    } else {
        let keys: Vec<Value<'py>> = items(&compiled_project.getattr("models")?)?
            .iter()
            .map(|model| model.getattr("key"))
            .collect::<PyResult<_>>()?;
        PyFrozenSet::new(py, keys)?.into_any()
    };
    let pipeline_mode: Value<'py> = class(py, DISCOVERY_TYPES, "PipelineMode")?;
    let mut names_by_mode: Vec<(String, Vec<String>)> = Vec::new();
    for pipeline in items(&compiled_project.getattr("pipelines")?)? {
        let mut names: Vec<String> = Vec::new();
        for model in items(&pipeline.getattr("models")?)? {
            let key: Value<'py> = model.getattr("key")?;
            if selected_keys.contains(&key)? {
                names.push(key.getattr("name")?.extract()?);
            }
        }
        if names.is_empty() {
            continue;
        }
        let mode: String = pipeline_mode
            .call1((pipeline.getattr("pipeline")?.getattr("mode")?,))?
            .str()?
            .extract()?;
        match names_by_mode.iter_mut().find(|(known, _)| *known == mode) {
            Some((_, known)) => known.extend(names),
            None => names_by_mode.push((mode, names)),
        }
    }
    for (_, names) in &mut names_by_mode {
        names.sort();
    }
    Ok(names_by_mode)
}

fn mixed_preparation<'py>(
    py: Python<'py>,
    virtual_: Value<'py>,
    direct: Value<'py>,
) -> PyResult<Value<'py>> {
    let virtual_text: String = virtual_.getattr("plan_text")?.extract()?;
    let direct_text: String = direct.getattr("plan_text")?.extract()?;
    let plan_text: String = [
        "Mixed Build Plan",
        "Execution order  virtual (staged) -> direct (applied immediately)",
        "",
        "Phase 1/2  VIRTUAL - staged for later promotion",
        &virtual_text,
        "",
        "Phase 2/2  DIRECT - applied immediately after phase 1 succeeds",
        &direct_text,
        "",
        "The virtual deployment remains staged until it is promoted.",
    ]
    .join("\n");
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("mode", "mixed")?;
    payload.set_item("execution_order", PyList::new(py, [VIRTUAL, DIRECT])?)?;
    payload.set_item(
        VIRTUAL,
        json_loads(py, &virtual_.getattr("workflow")?.getattr("plan_json")?)?,
    )?;
    payload.set_item(
        DIRECT,
        json_loads(py, &direct.getattr("workflow")?.getattr("plan_json")?)?,
    )?;
    let plan_json: String = json_dumps(payload.as_any(), Some(2), false)?;
    let mut requirements: Vec<Value<'py>> = items(&virtual_.getattr("protection_requirements")?)?;
    requirements.extend(items(&direct.getattr("protection_requirements")?)?);
    build(
        py,
        BUILD_MODELS,
        "MixedWorkflowPreparation",
        &[
            ("virtual", virtual_),
            ("direct", direct),
            ("plan_text", PyString::new(py, &plan_text).into_any()),
            ("plan_json", PyString::new(py, &plan_json).into_any()),
            ("protection_requirements", tuple(py, requirements)?),
        ],
    )
}

/// Inspect connected direct state and assemble the complete workflow once.
fn prepare_direct<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    adapter_profile: &Value<'py>,
    effective_start_time: &Value<'py>,
) -> PyResult<Value<'py>> {
    let preview: Value<'py> = native(
        py,
        "build_direct_build_preview",
        &[
            ("options", options.clone()),
            ("client", client.clone()),
            ("analysis", analysis.clone()),
            ("effective_start_time", effective_start_time.clone()),
        ],
    )?;
    let audits: Value<'py> = native(
        py,
        "prepare_direct_build_audits",
        &[
            ("preview", preview.clone()),
            ("adapter_profile", adapter_profile.clone()),
        ],
    )?;
    let plan: Value<'py> = preview.getattr("plan")?;
    let adapter_name: Value<'py> = preview.getattr("adapter_name")?;
    let policy_changes: Value<'py> = preview.getattr("row_error_policy_changes")?;
    let request: Value<'py> = build(
        py,
        DIRECT_MODELS,
        "DirectBuildRequest",
        &[
            ("plan", plan.clone()),
            (
                "realized_project",
                preview.getattr("analysis")?.getattr("realized_project")?,
            ),
            ("database", preview.getattr("database")?),
            ("metadata_database", preview.getattr("metadata_database")?),
            (
                "tool_version",
                py.import(BUILD_CONSTANTS)?
                    .getattr("SQLTIDE_TOOL_VERSION")?,
            ),
            ("audits", audits),
            ("row_error_policy_changes", policy_changes.clone()),
        ],
    )?;
    let plan_json: Value<'py> = native(
        py,
        "render_direct_plan_json",
        &[
            ("plan", plan.clone()),
            ("adapter_name", adapter_name.clone()),
            ("row_error_policy_changes", policy_changes.clone()),
        ],
    )?;
    let mut workflow: Value<'py> = native(
        py,
        "assemble_direct_build_workflow",
        &[
            ("request", request.clone()),
            ("client", client.clone()),
            ("snapshot", preview.getattr("warehouse_snapshot")?),
            ("plan_json", plan_json),
        ],
    )?;
    if truthy(&workflow, "recreated_sources")? {
        let plan_json: Value<'py> = native(
            py,
            "render_direct_plan_json",
            &[
                ("plan", plan.clone()),
                ("adapter_name", adapter_name.clone()),
                ("source_recreations", workflow.getattr("recreated_sources")?),
                ("row_error_policy_changes", policy_changes.clone()),
            ],
        )?;
        let template: Value<'py> =
            replace(&workflow.getattr("template")?, &[("plan_json", plan_json)])?;
        workflow = replace(&workflow, &[("template", template)])?;
    }
    let plan_text: Value<'py> = native(
        py,
        "render_direct_plan_text",
        &[
            ("plan", plan.clone()),
            ("adapter_name", adapter_name),
            ("use_color", boolean(py, use_color(py)?)?),
            ("verbose", options.getattr("verbose")?),
            ("source_recreations", workflow.getattr("recreated_sources")?),
            ("row_error_policy_changes", policy_changes),
        ],
    )?;
    let execution_keys: Value<'py> =
        PyFrozenSet::new(py, items(&plan.getattr("execution_scope")?)?)?.into_any();
    let protection_requirements: Value<'py> =
        build_protection_requirements(py, &analysis.getattr("compiled_project")?, &execution_keys)?;
    build(
        py,
        BUILD_MODELS,
        "DirectWorkflowPreparation",
        &[
            ("preview", preview),
            ("request", request),
            ("workflow", workflow),
            ("plan_text", plan_text),
            ("protection_requirements", protection_requirements),
        ],
    )
}

/// Inspect connected virtual state and assemble the complete workflow once.
fn prepare_virtual<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    options: &Value<'py>,
    start_time_utc: &Value<'py>,
    client: &Value<'py>,
) -> PyResult<Value<'py>> {
    let preview: Value<'py> = native(
        py,
        "build_virtual_build_preview",
        &[
            ("options", options.clone()),
            ("start_time_utc", start_time_utc.clone()),
            ("client", client.clone()),
            ("analysis", analysis.clone()),
        ],
    )?;
    let plan: Value<'py> = preview.getattr("plan")?;
    let desired_state: Value<'py> = preview.getattr("desired_state")?;
    let mut plan_text: String = native(
        py,
        "render_plan_text",
        &[
            ("plan", plan.clone()),
            ("desired_state", desired_state.clone()),
            ("database", preview.getattr("database")?),
            ("verbose", options.getattr("verbose")?),
            ("use_color", boolean(py, use_color(py)?)?),
        ],
    )?
    .extract()?;
    let payload: Value<'py> = json_loads(
        py,
        &native(
            py,
            "render_plan_json",
            &[
                ("plan", plan.clone()),
                (
                    "adapter_name",
                    client.getattr("adapter_identity")?.getattr("name")?,
                ),
            ],
        )?,
    )?;
    payload.set_item("deployment_created_at", preview.getattr("created_at")?)?;
    let policy_changes: Value<'py> = preview.getattr("row_error_policy_changes")?;
    if policy_changes.is_truthy()? {
        let mut serialized: Vec<Value<'py>> = Vec::new();
        let mut lines: Vec<String> = vec![plan_text];
        for change in items(&policy_changes)? {
            let entry: Bound<'py, PyDict> = PyDict::new(py);
            entry.set_item("model_key", change.getattr("model_key")?.getattr("name")?)?;
            entry.set_item("desired_policy", change.getattr("desired_policy")?.str()?)?;
            entry.set_item("view", change.getattr("view_name")?)?;
            entry.set_item("dead_letter_view", change.getattr("dead_letter_view_name")?)?;
            entry.set_item(
                "operation",
                if truthy(&change, "landing_repair_only")? {
                    "repair_landing_view"
                } else {
                    "view_only"
                },
            )?;
            entry.set_item("replay_roots", 0)?;
            serialized.push(entry.into_any());
        }
        payload.set_item("row_error_policy_changes", PyList::new(py, serialized)?)?;
        let render_line: Value<'py> = py.import(NATIVE)?.getattr("render_row_error_policy_line")?;
        for change in items(&policy_changes)? {
            lines.push(render_line.call1((change,))?.extract()?);
        }
        plan_text = lines.join("\n");
    }
    let request: Value<'py> = build(
        py,
        BACKFILL_MODELS,
        "BackfillBootstrapRequest",
        &[
            ("desired_state", desired_state),
            ("default_database", preview.getattr("database")?),
            ("metadata_database", preview.getattr("metadata_database")?),
            (
                "replay_lineage_mode",
                preview.getattr("replay_lineage_mode")?,
            ),
            ("confirmed_plan", plan),
            ("deployment_id", preview.getattr("deployment_id")?),
            ("full_refresh_keys", preview.getattr("full_refresh_keys")?),
            ("start_time_keys", preview.getattr("start_time_keys")?),
            ("start_time", preview.getattr("start_time")?),
            ("created_at", preview.getattr("created_at")?),
            (
                "confirmed_target_catalog",
                preview.getattr("target_catalog")?,
            ),
            (
                "confirmed_metadata_catalog",
                preview.getattr("metadata_catalog")?,
            ),
            ("row_error_policy_changes", policy_changes),
        ],
    )?;
    let plan_json: String = json_dumps(&payload, Some(2), false)? + "\n";
    let workflow: Value<'py> = native(
        py,
        "assemble_virtual_build_workflow",
        &[
            ("request", request.clone()),
            ("client", client.clone()),
            ("plan_json", PyString::new(py, &plan_json).into_any()),
        ],
    )?;
    let protection_requirements: Value<'py> = build_protection_requirements(
        py,
        &analysis.getattr("compiled_project")?,
        &preview.getattr("execution_logical_model_keys")?,
    )?;
    build(
        py,
        BUILD_MODELS,
        "VirtualWorkflowPreparation",
        &[
            ("preview", preview),
            ("request", request),
            ("workflow", workflow),
            ("plan_text", PyString::new(py, &plan_text).into_any()),
            ("protection_requirements", protection_requirements),
        ],
    )
}

fn validate_common_flags(py: Python<'_>, options: &Value<'_>) -> PyResult<()> {
    let changed: bool = truthy(options, "changed")?;
    let selected: bool = truthy(options, "selectors")?;
    let full_refresh: bool = truthy(options, "full_refresh")?;
    let start_time: bool = !options.getattr("start_time")?.is_none();
    if changed && selected {
        return Err(user_error(py, "--changed cannot be combined with --select"));
    }
    if truthy(options, "include_missing_upstream")? && !(changed || selected) {
        return Err(user_error(
            py,
            "--include-missing-upstream requires --changed or --select",
        ));
    }
    if full_refresh && start_time {
        return Err(user_error(
            py,
            "--full-refresh cannot be combined with --start-time",
        ));
    }
    if (full_refresh || start_time) && !selected && !changed {
        let required_flag: &str = if full_refresh {
            "--full-refresh"
        } else {
            "--start-time"
        };
        return Err(user_error(
            py,
            &format!("{required_flag} requires --changed or at least one --select"),
        ));
    }
    Ok(())
}

/// Refuse a replay window when every selected model is refreshed by the warehouse.
fn reject_replayless_start_time<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    options: &Value<'py>,
) -> PyResult<()> {
    let selectors: Value<'py> = options.getattr("selectors")?;
    if options.getattr("start_time")?.is_none() || !selectors.is_truthy()? {
        return Ok(());
    }
    let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
    let selected_keys: Value<'py> = native(
        py,
        "resolve_selected_logical_model_keys",
        &[
            ("compiled_pipelines", compiled_project.getattr("pipelines")?),
            ("selectors", selectors),
        ],
    )?;
    let postgres_source_names: Bound<'py, PySet> = PySet::empty(py)?;
    for source in items(&compiled_project.getattr("sources")?)? {
        if is_a(
            &source.getattr("source")?,
            DISCOVERY_MODELS,
            "PostgresRefreshSourceStep",
        )? {
            postgres_source_names.add(source.getattr("key")?.getattr("name")?)?;
        }
    }
    if postgres_source_names.is_empty() {
        return Ok(());
    }
    let table_model: Value<'py> = class(py, COMPILE_MODELS, "CompiledTableModel")?;
    let mut replayless_names: Vec<String> = Vec::new();
    for model in items(&compiled_project.getattr("models")?)? {
        let key: Value<'py> = model.getattr("key")?;
        if selected_keys.contains(&key)?
            && model.is_instance(&table_model)?
            && postgres_source_names.contains(model.getattr("transform")?.getattr("source")?)?
        {
            replayless_names.push(key.getattr("name")?.extract()?);
        }
    }
    replayless_names.sort();
    if !replayless_names.is_empty() && replayless_names.len() == selected_keys.len()? {
        return Err(user_error(
            py,
            &format!(
                "--start-time is not available for scheduled postgres sources, which the \
                 warehouse refreshes in full: {}",
                replayless_names.join(", ")
            ),
        ));
    }
    Ok(())
}

fn reject_direct_unsupported_flags(py: Python<'_>, options: &Value<'_>) -> PyResult<()> {
    if !options.getattr("deployment_id")?.is_none() {
        return Err(user_error(
            py,
            "--deployment-id requires virtual environments",
        ));
    }
    if truthy(options, "full_refresh")? {
        return Err(user_error(
            py,
            "--full-refresh is a virtual-environment replay control and is not available in \
             direct mode. Set defaults.pipeline_mode = 'virtual' or select a virtual pipeline \
             to use it.",
        ));
    }
    Ok(())
}

/// Prepare the exact connected workflow and enforce every configured scope gate.
#[pyfunction]
#[pyo3(signature = (*, analysis, options, client, adapter_profile))]
fn prepare_validated_build<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    let dynamic_selection: bool =
        truthy(options, "changed")? || truthy(options, "include_missing_upstream")?;
    if !dynamic_selection {
        native(
            py,
            "validate_build_pipeline_limit",
            &[
                ("analysis", analysis.clone()),
                ("selectors", options.getattr("selectors")?),
            ],
        )?;
    }
    let preparation: Value<'py> =
        prepare_build_workflow(py, analysis, options, client, adapter_profile)?;
    if dynamic_selection {
        native(
            py,
            "validate_build_pipeline_limit",
            &[
                ("analysis", analysis.clone()),
                ("preparation", preparation.clone()),
            ],
        )?;
    }
    validate_expected_build_scope(py, &preparation, analysis)?;
    Ok(preparation)
}

/// The direct and virtual halves of a direct, virtual or mixed preparation.
fn phases<'py>(preparation: &Value<'py>) -> PyResult<(Option<Value<'py>>, Option<Value<'py>>)> {
    if is_a(preparation, BUILD_MODELS, "MixedWorkflowPreparation")? {
        return Ok((
            Some(preparation.getattr("direct")?),
            Some(preparation.getattr("virtual")?),
        ));
    }
    let direct: bool = is_a(preparation, BUILD_MODELS, "DirectWorkflowPreparation")?;
    let virtual_: bool = is_a(preparation, BUILD_MODELS, "VirtualWorkflowPreparation")?;
    Ok((
        direct.then(|| preparation.clone()),
        virtual_.then(|| preparation.clone()),
    ))
}

/// Execution keys and prerequisite or context keys of one preparation.
fn scope_keys<'py>(preparation: &Value<'py>) -> PyResult<(Vec<Value<'py>>, Vec<Value<'py>>)> {
    let (direct, virtual_) = phases(preparation)?;
    let mut writes: Vec<Value<'py>> = Vec::new();
    let mut reads: Vec<Value<'py>> = Vec::new();
    if let Some(direct) = direct {
        let plan: Value<'py> = direct.getattr("preview")?.getattr("plan")?;
        writes.extend(items(&plan.getattr("execution_scope")?)?);
        for item in items(&plan.getattr("prerequisite_scope")?)? {
            reads.push(item.getattr("key")?);
        }
    }
    if let Some(virtual_) = virtual_ {
        let preview: Value<'py> = virtual_.getattr("preview")?;
        writes.extend(items(&preview.getattr("run_execution_scope")?)?);
        reads.extend(items(&preview.getattr("run_context_scope")?)?);
    }
    Ok((writes, reads))
}

pub(crate) fn identity(key: &Value<'_>) -> PyResult<String> {
    Ok(format!(
        "{}:{}",
        formatted(&key.getattr("resource_type")?)?,
        formatted(&key.getattr("name")?)?
    ))
}

fn identities<'py>(py: Python<'py>, keys: &[Value<'py>]) -> PyResult<Bound<'py, PyFrozenSet>> {
    let identities: Vec<String> = keys.iter().map(identity).collect::<PyResult<_>>()?;
    PyFrozenSet::new(py, identities)
}

/// Exact write and read identities of a connected preparation.
#[pyfunction]
fn resolve_prepared_build_scope<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
) -> PyResult<(Bound<'py, PyFrozenSet>, Bound<'py, PyFrozenSet>)> {
    let (writes, reads) = scope_keys(preparation)?;
    Ok((identities(py, &writes)?, identities(py, &reads)?))
}

/// `type:name=pipeline` for every scoped key that a compiled model owns.
#[pyfunction]
#[pyo3(signature = (*, preparation, analysis))]
fn prepared_pipeline_scope<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let (writes, reads) = scope_keys(preparation)?;
    let keys: Bound<'py, PySet> = PySet::new(py, writes.iter().chain(reads.iter()))?;
    let pipeline_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(&analysis.getattr("compiled_project")?.getattr("models")?)? {
        pipeline_by_key.set_item(model.getattr("key")?, model.getattr("pipeline_name")?)?;
    }
    let mut scope: Vec<String> = Vec::new();
    for key in keys.iter() {
        if let Some(pipeline) = pipeline_by_key.get_item(&key)? {
            scope.push(format!("{}={}", identity(&key)?, formatted(&pipeline)?));
        }
    }
    PyFrozenSet::new(py, scope)
}

fn scope_from_json<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Bound<'py, PyFrozenSet>> {
    let parsed: Value<'py> = match json_loads(py, value) {
        Ok(parsed) => parsed,
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            let cause: Value<'py> = error.into_value(py).into_bound(py).into_any();
            let replacement: PyErr = user_error(py, INVALID_SCOPE);
            chained(replacement.value(py).as_any(), &cause, Some(&cause))?;
            return Err(replacement);
        }
        Err(error) => return Err(error),
    };
    let Ok(list) = parsed.downcast::<PyList>() else {
        return Err(user_error(py, INVALID_SCOPE));
    };
    for item in list.iter() {
        if !item.is_instance_of::<PyString>() {
            return Err(user_error(py, INVALID_SCOPE));
        }
    }
    PyFrozenSet::new(py, list.iter())
}

/// Abort when a dev-server child replans to a scope that was not authorized.
#[pyfunction]
#[pyo3(name = "validate_expected_build_scope", signature = (*, preparation, analysis))]
fn validate_expected_build_scope_py<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<()> {
    validate_expected_build_scope(py, preparation, analysis)
}

fn validate_expected_build_scope<'py>(
    py: Python<'py>,
    preparation: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<()> {
    let constants: Bound<'py, PyModule> = py.import(BUILD_CONSTANTS)?;
    let environ: Value<'py> = py.import("os")?.getattr("environ")?;
    let read = |name: &str| -> PyResult<Value<'py>> {
        environ.call_method1("get", (constants.getattr(name)?,))
    };
    let raw_writes: Value<'py> = read("EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR")?;
    let raw_reads: Value<'py> = read("EXPECTED_BUILD_READ_SCOPE_ENV_VAR")?;
    let raw_pipelines: Value<'py> = read("EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR")?;
    let missing: [bool; 3] = [
        raw_writes.is_none(),
        raw_reads.is_none(),
        raw_pipelines.is_none(),
    ];
    if missing.iter().all(|missing| *missing) {
        return Ok(());
    }
    if missing.iter().any(|missing| *missing) {
        return Err(user_error(
            py,
            "Authorized build scope is incomplete; re-plan the build",
        ));
    }
    let expected_writes: Bound<'py, PyFrozenSet> = scope_from_json(py, &raw_writes)?;
    let expected_reads: Bound<'py, PyFrozenSet> = scope_from_json(py, &raw_reads)?;
    let expected_pipelines: Bound<'py, PyFrozenSet> = scope_from_json(py, &raw_pipelines)?;
    let (actual_writes, actual_reads) = resolve_prepared_build_scope(py, preparation)?;
    let actual_pipelines: Bound<'py, PyFrozenSet> =
        prepared_pipeline_scope(py, preparation, analysis)?;
    if !(actual_writes.eq(&expected_writes)?
        && actual_reads.eq(&expected_reads)?
        && actual_pipelines.eq(&expected_pipelines)?)
    {
        return Err(user_error(
            py,
            "Build scope changed after server authorization; review and start the plan again",
        ));
    }
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(prepare_build_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(prepare_validated_build, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_prepared_build_scope, module)?)?;
    module.add_function(wrap_pyfunction!(prepared_pipeline_scope, module)?)?;
    module.add_function(wrap_pyfunction!(validate_expected_build_scope_py, module)?)
}
