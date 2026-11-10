//! The connected plans a build renders and confirms before it writes: the direct preview with
//! `--changed` selection from SQL baselines and drifted sources, and the virtual preview with its
//! fixed deployment identity, execution and context scopes. Native planners are looked up on
//! `sqltide._native` when called; warehouse reads go through the Python adapter connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet};

use crate::build_cli::build_protection_requirements;
use crate::direct_build::{Value, build, call, class, is_a, items, none, or, string, text, tuple};

const NATIVE: &str = "sqltide._native";
const DEFAULT_DATABASE: &str = "sqltide.cli.entry.main._resolve_default_database";
const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";
const BUILD_MODELS: &str = "sqltide.cli.build.models";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_TYPES: &str = "sqltide.compiler.compile.types";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const GRAPH_CONSTANTS: &str = "sqltide.compiler.graph.constants";
const GRAPH_TYPES: &str = "sqltide.compiler.graph.types";
const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";

fn native<'py>(py: Python<'py>, name: &str, pairs: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
    call(py, NATIVE, name, pairs)
}

fn error(py: Python<'_>, module: &str, name: &str, message: String) -> PyErr {
    match class(py, module, name).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn empty_frozenset(py: Python<'_>) -> PyResult<Value<'_>> {
    Ok(PyFrozenSet::empty(py)?.into_any())
}

fn resolve_default_database<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    override_: Value<'py>,
) -> PyResult<Value<'py>> {
    let pipelines: Value<'py> = PyList::new(
        py,
        items(&analysis.getattr("compile_inputs")?.getattr("pipelines")?)?,
    )?
    .into_any();
    call(
        py,
        DEFAULT_DATABASE,
        "resolve_default_database",
        &[("loaded_pipelines", pipelines), ("override", override_)],
    )
}

fn validate_declared_external_sources<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    catalog: &Value<'py>,
    database: &Value<'py>,
) -> PyResult<()> {
    native(
        py,
        "validate_declared_external_sources",
        &[
            ("catalog", catalog.clone()),
            (
                "external_source_replay_configs",
                analysis
                    .getattr("realized_project")?
                    .getattr("desired_state")?
                    .getattr("external_source_replay_configs")?,
            ),
            ("database", database.clone()),
        ],
    )?;
    Ok(())
}

fn is_direct_pipeline(py: Python<'_>, pipeline: &Value<'_>) -> PyResult<bool> {
    let modes: Value<'_> = class(py, DISCOVERY_TYPES, "PipelineMode")?;
    modes
        .call1((pipeline.getattr("pipeline")?.getattr("mode")?,))?
        .eq(modes.getattr("DIRECT")?)
}

/// Direct models whose compiled SQL drifted from their persisted baseline.
fn changed_direct_model_keys<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    snapshot: &Value<'py>,
    database: &Value<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let fingerprints: Value<'py> = snapshot.getattr("fingerprints")?;
    let unavailable: Value<'py> =
        class(py, ADAPTER_TYPES, "AdapterOptionalStateStatus")?.getattr("UNAVAILABLE")?;
    if fingerprints.getattr("status")?.eq(unavailable)? {
        let detail: Value<'py> = or(
            fingerprints.getattr("warning")?,
            &text(py, "direct fingerprints are unavailable"),
        )?;
        return Err(error(
            py,
            PLANNER_EXCEPTIONS,
            "DirectPlanError",
            format!("Cannot select changed models: {}", detail.str()?),
        ));
    }
    let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
    let direct: Bound<'py, PySet> = PySet::empty(py)?;
    for pipeline in items(&compiled_project.getattr("pipelines")?)? {
        if is_direct_pipeline(py, &pipeline)? {
            for model in items(&pipeline.getattr("models")?)? {
                direct.add(model.getattr("key")?)?;
            }
        }
    }
    if direct.is_empty() {
        return Err(error(
            py,
            PLANNER_EXCEPTIONS,
            "DirectPlanError",
            "--changed is only supported for direct models".to_owned(),
        ));
    }
    let baselines: Bound<'py, PyDict> = PyDict::new(py);
    for baseline in items(&fingerprints.getattr("baselines")?)? {
        baselines.set_item(baseline.getattr("logical_model_identity")?, baseline)?;
    }
    let fingerprint: Value<'py> = py.import(NATIVE)?.getattr("DirectModelFingerprint")?;
    let mut changed: Vec<Value<'py>> = Vec::new();
    for model in items(&compiled_project.getattr("models")?)? {
        let key: Value<'py> = model.getattr("key")?;
        if !direct.contains(&key)? {
            continue;
        }
        let identity: String = format!("{}.{}", database.str()?, key.getattr("name")?.str()?);
        let baseline: Value<'py> = baselines.as_any().call_method1("get", (identity,))?;
        let reasons: Value<'py> = fingerprint.call_method(
            "drift_reasons",
            (),
            Some(&{
                let arguments = PyDict::new(py);
                arguments.set_item("model", &model)?;
                arguments.set_item("baseline", baseline)?;
                arguments
            }),
        )?;
        if reasons.is_truthy()? {
            changed.push(key);
        }
    }
    Ok(changed)
}

/// Whether the downstream closure of `selected` reaches any of the pipelines' models.
fn rebuilds_any_pipeline<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    selected: &Bound<'py, PySet>,
    pipeline_keys: &[Value<'py>],
) -> PyResult<bool> {
    if selected.is_empty() {
        return Ok(false);
    }
    let rebuilt: Bound<'py, PyFrozenSet> = PyFrozenSet::new(
        py,
        items(&native(
            py,
            "collect_reachable_keys",
            &[
                ("graph", analysis.getattr("graph")?),
                (
                    "root_keys",
                    PyFrozenSet::new(py, selected.iter())?.into_any(),
                ),
                (
                    "direction",
                    class(py, GRAPH_TYPES, "GraphTraversalDirection")?.getattr("DOWNSTREAM")?,
                ),
                (
                    "edge_types",
                    class(py, GRAPH_CONSTANTS, "ALL_DEPENDENCY_EDGE_TYPES")?,
                ),
            ],
        )?)?,
    )?;
    for keys in pipeline_keys {
        if rebuilt.call_method1("intersection", (keys,))?.is_truthy()? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// One direct owner per drifted source, preferring unprotected owners, then by name.
fn source_drift_selections<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    snapshot: &Value<'py>,
    sql_changed: &[Value<'py>],
) -> PyResult<Vec<Value<'py>>> {
    let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
    let direct_models: Bound<'py, PyDict> = PyDict::new(py);
    for pipeline in items(&compiled_project.getattr("pipelines")?)? {
        if is_direct_pipeline(py, &pipeline)? {
            let mut keys: Vec<Value<'py>> = Vec::new();
            for model in items(&pipeline.getattr("models")?)? {
                keys.push(model.getattr("key")?);
            }
            direct_models.set_item(
                pipeline.getattr("pipeline")?.getattr("name")?,
                tuple(py, keys)?,
            )?;
        }
    }
    let selected: Bound<'py, PySet> = PySet::new(py, sql_changed)?;
    let mut selections: Vec<Value<'py>> = Vec::new();
    for drifted in items(&native(
        py,
        "drifted_managed_sources",
        &[
            ("realized_project", analysis.getattr("realized_project")?),
            ("catalog", snapshot.getattr("catalog")?),
        ],
    )?)? {
        let owner_names: Value<'py> = drifted.getattr("owner_pipeline_names")?;
        let mut owners: Vec<((bool, String), Value<'py>)> = Vec::new();
        for name in items(&owner_names)? {
            let Some(keys) = direct_models.get_item(&name)? else {
                continue;
            };
            let protected: bool = build_protection_requirements(
                py,
                &compiled_project,
                &PyFrozenSet::new(py, items(&keys)?)?.into_any(),
            )?
            .is_truthy()?;
            owners.push(((protected, name.extract()?), name));
        }
        // Stable, like Python's `sorted`.
        owners.sort_by(|left, right| left.0.cmp(&right.0));
        if owners.is_empty() {
            continue;
        }
        let mut pipeline_keys: Vec<Value<'py>> = Vec::new();
        for (_, name) in &owners {
            pipeline_keys.push(direct_models.as_any().get_item(name)?);
        }
        if rebuilds_any_pipeline(py, analysis, &selected, &pipeline_keys)? {
            continue;
        }
        let owner: &Value<'py> = &owners[0].1;
        let owner_keys: Value<'py> = direct_models.as_any().get_item(owner)?;
        let mut added: Vec<Value<'py>> = Vec::new();
        for key in items(&owner_keys)? {
            if !selected.contains(&key)? {
                added.push(key);
            }
        }
        selections.push(build(
            py,
            PLANNER_MODELS,
            "DirectSourceDriftSelection",
            &[
                ("source_key", drifted.getattr("source_key")?),
                ("pipeline_name", owner.clone()),
                ("reader_pipeline_names", owner_names.clone()),
                (
                    "drifted_setting_names",
                    drifted.getattr("drifted_setting_names")?,
                ),
                ("model_keys", tuple(py, added)?),
            ],
        )?);
        for key in items(&owner_keys)? {
            selected.add(key)?;
        }
    }
    Ok(selections)
}

/// Refuse a replay window that would discard history of pipelines only drift selected.
fn reject_source_drift_start_time(
    py: Python<'_>,
    selections: &[Value<'_>],
    start_time: &Value<'_>,
) -> PyResult<()> {
    if start_time.is_none() || selections.is_empty() {
        return Ok(());
    }
    let mut names: Vec<String> = Vec::new();
    let mut sources: Vec<String> = Vec::new();
    for selection in selections {
        names.push(format!(
            "pipeline:{}",
            selection.getattr("pipeline_name")?.str()?
        ));
        sources.push(format!(
            "'{}'",
            selection.getattr("source_key")?.getattr("name")?.str()?
        ));
    }
    Err(error(
        py,
        CLI_EXCEPTIONS,
        "CliUserError",
        format!(
            "--changed selects {} because the settings of source {} drifted from the project, \
             and --start-time would rebuild their models from that time only, discarding earlier \
             history. Run `sqt build --select {}` without --start-time first, then rerun with \
             --changed --start-time.",
            names.join(", "),
            sources.join(", "),
            names.join(" ")
        ),
    ))
}

fn reject_changed_replayless_start_time(
    py: Python<'_>,
    analysis: &Value<'_>,
    selected: &Bound<'_, PyFrozenSet>,
    start_time: &Value<'_>,
) -> PyResult<()> {
    if start_time.is_none() || selected.is_empty() {
        return Ok(());
    }
    let compiled_project: Value<'_> = analysis.getattr("compiled_project")?;
    let mut postgres_sources: Vec<Value<'_>> = Vec::new();
    for source in items(&compiled_project.getattr("sources")?)? {
        if is_a(
            &source.getattr("source")?,
            DISCOVERY_MODELS,
            "PostgresRefreshSourceStep",
        )? {
            postgres_sources.push(source.getattr("key")?.getattr("name")?);
        }
    }
    let postgres_sources: Bound<'_, PyFrozenSet> = PyFrozenSet::new(py, postgres_sources)?;
    let mut replayless: Vec<String> = Vec::new();
    for model in items(&compiled_project.getattr("models")?)? {
        if selected.contains(model.getattr("key")?)?
            && is_a(&model, COMPILE_MODELS, "CompiledTableModel")?
            && postgres_sources.contains(model.getattr("transform")?.getattr("source")?)?
        {
            replayless.push(model.getattr("key")?.getattr("name")?.extract()?);
        }
    }
    replayless.sort();
    if !replayless.is_empty() && replayless.len() == selected.len() {
        return Err(error(
            py,
            CLI_EXCEPTIONS,
            "CliUserError",
            format!(
                "--start-time is not available for scheduled postgres sources, which the \
                 warehouse refreshes in full: {}",
                replayless.join(", ")
            ),
        ));
    }
    Ok(())
}

/// Plan the selected direct closure from one shared compile analysis.
#[pyfunction]
#[pyo3(signature = (*, options, client, analysis, effective_start_time=None))]
fn build_direct_build_preview<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    analysis: &Value<'py>,
    effective_start_time: Option<Value<'py>>,
) -> PyResult<Value<'py>> {
    let start_time: Value<'py> = effective_start_time.unwrap_or_else(|| none(py));
    let database: Value<'py> =
        resolve_default_database(py, analysis, options.getattr("database")?)?;
    let metadata_database: Value<'py> = or(options.getattr("metadata_database")?, &database)?;
    let realized_project: Value<'py> = analysis.getattr("realized_project")?;
    let mut identities: Vec<Value<'py>> = Vec::new();
    for model in items(&realized_project.getattr("project")?.getattr("models")?)? {
        identities.push(text(
            py,
            &format!(
                "{}.{}",
                database.str()?,
                model.getattr("key")?.getattr("name")?.str()?
            ),
        ));
    }
    let snapshot: Value<'py> = native(
        py,
        "load_direct_warehouse_snapshot",
        &[
            ("client", client.clone()),
            ("database", database.clone()),
            ("metadata_database", metadata_database.clone()),
            ("logical_model_identities", tuple(py, identities)?),
        ],
    )?;
    let catalog: Value<'py> = snapshot.getattr("catalog")?;
    validate_declared_external_sources(py, analysis, &catalog, &database)?;
    let modes: Value<'py> = class(py, PLANNER_TYPES, "DirectSelectionMode")?;
    let changed: bool = options.getattr("changed")?.is_truthy()?;
    let (selection_mode, selected, drift): (Value<'py>, Bound<'py, PyFrozenSet>, Vec<Value<'py>>) =
        if changed {
            let sql_changed: Vec<Value<'py>> =
                changed_direct_model_keys(py, analysis, &snapshot, &database)?;
            let drift: Vec<Value<'py>> =
                source_drift_selections(py, analysis, &snapshot, &sql_changed)?;
            reject_source_drift_start_time(py, &drift, &options.getattr("start_time")?)?;
            let selected: Bound<'py, PySet> = PySet::new(py, &sql_changed)?;
            for selection in &drift {
                for key in items(&selection.getattr("model_keys")?)? {
                    selected.add(key)?;
                }
            }
            let selected: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, selected.iter())?;
            reject_changed_replayless_start_time(
                py,
                analysis,
                &selected,
                &options.getattr("start_time")?,
            )?;
            (modes.getattr("CHANGED")?, selected, drift)
        } else {
            let selection: Value<'py> = native(
                py,
                "resolve_selection",
                &[
                    ("realized_project", realized_project.clone()),
                    ("graph", analysis.getattr("graph")?),
                    ("selectors", options.getattr("selectors")?),
                ],
            )?;
            let mode: &str = if options.getattr("selectors")?.is_truthy()? {
                "EXPLICIT"
            } else {
                "ALL_MODELS"
            };
            (
                modes.getattr(mode)?,
                selection
                    .getattr("selected_logical_model_keys")?
                    .downcast_into::<PyFrozenSet>()?,
                Vec::new(),
            )
        };
    let plan: Value<'py> = native(
        py,
        "plan_direct_build",
        &[
            ("graph", analysis.getattr("graph")?),
            ("realized_project", realized_project),
            ("snapshot", snapshot.clone()),
            ("database", database.clone()),
            ("selected_model_keys", selected.into_any()),
            ("selection_mode", selection_mode),
            (
                "include_missing_upstream",
                options.getattr("include_missing_upstream")?,
            ),
            ("effective_start_time", start_time.clone()),
            ("source_drift_selections", tuple(py, drift)?),
        ],
    )?;
    let policy_changes: Value<'py> = if changed {
        native(
            py,
            "plan_row_error_policy_changes",
            &[
                ("analysis", analysis.clone()),
                ("plan", plan.clone()),
                ("relations", catalog.getattr("relations")?),
            ],
        )?
    } else {
        tuple(py, Vec::new())?
    };
    build(
        py,
        BUILD_MODELS,
        "DirectBuildPreviewContext",
        &[
            ("row_error_policy_changes", policy_changes),
            ("analysis", analysis.clone()),
            ("plan", plan),
            ("warehouse_snapshot", snapshot),
            ("database", database),
            ("metadata_database", metadata_database),
            (
                "adapter_name",
                client.getattr("adapter_identity")?.getattr("name")?,
            ),
            ("effective_start_time", start_time),
        ],
    )
}

/// Model keys in graph order that the plan prepares, and the upstream keys they read but do
/// not execute.
fn run_scopes<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    plan: &Value<'py>,
) -> PyResult<(Vec<Value<'py>>, Vec<Value<'py>>)> {
    let mut planned: Vec<Value<'py>> = Vec::new();
    for prepared in items(&plan.getattr("prepared_shadow_objects")?)? {
        planned.push(prepared.getattr("logical_model_name")?);
    }
    let planned: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, planned)?;
    let graph: Value<'py> = analysis.getattr("graph")?;
    let ordered: Vec<Value<'py>> = items(&graph.getattr("ordered_keys")?)?;
    let model_type: Value<'py> =
        class(py, COMPILE_TYPES, "LogicalResourceType")?.getattr("MODEL")?;
    let mut execution: Vec<Value<'py>> = Vec::new();
    for key in &ordered {
        if key.getattr("resource_type")?.eq(&model_type)?
            && planned.contains(key.getattr("name")?)?
        {
            execution.push(key.clone());
        }
    }
    let executed: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, &execution)?;
    let upstream: Value<'py> = graph.getattr("upstream_edges_by_key")?;
    let context_keys: Bound<'py, PySet> = PySet::empty(py)?;
    let empty: Value<'py> = tuple(py, Vec::new())?;
    for key in &execution {
        for edge in items(&upstream.call_method1("get", (key, &empty))?)? {
            let upstream_key: Value<'py> = edge.getattr("upstream_key")?;
            if !executed.contains(&upstream_key)? {
                context_keys.add(upstream_key)?;
            }
        }
    }
    let mut context: Vec<Value<'py>> = Vec::new();
    for key in ordered {
        if context_keys.contains(&key)? {
            context.push(key);
        }
    }
    Ok((execution, context))
}

/// Plan one virtual deployment and fix its identity before confirmation.
#[pyfunction]
#[pyo3(signature = (*, options, start_time_utc, client, analysis))]
fn build_virtual_build_preview<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    start_time_utc: &Value<'py>,
    client: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let database: Value<'py> =
        resolve_default_database(py, analysis, options.getattr("database")?)?;
    let snapshot: Value<'py> = native(
        py,
        "load_planning_warehouse_snapshot",
        &[("client", client.clone()), ("database", database.clone())],
    )?;
    let start_time: Value<'py> = start_time_utc.clone();
    let catalog: Value<'py> = snapshot.getattr("catalog")?;
    validate_declared_external_sources(py, analysis, &catalog, &database)?;
    let metadata_database: Value<'py> = or(options.getattr("metadata_database")?, &database)?;
    let metadata_catalog: Value<'py> = if metadata_database.eq(&database)? {
        catalog.clone()
    } else {
        client.call_method1("load_catalog", (&metadata_database,))?
    };
    let realized_project: Value<'py> = analysis.getattr("realized_project")?;
    let selection: Value<'py> = native(
        py,
        "resolve_selection",
        &[
            ("realized_project", realized_project),
            ("graph", analysis.getattr("graph")?),
            ("selectors", options.getattr("selectors")?),
        ],
    )?;
    let replay_lineage_mode: Value<'py> = or(
        selection.getattr("replay_lineage_mode")?,
        &class(py, DISCOVERY_TYPES, "ReplayLineageMode")?.getattr("OFFSETS")?,
    )?;
    let desired_state: Value<'py> = selection.getattr("desired_state")?;
    let actual_state: Value<'py> = native(
        py,
        "load_actual_state_from_snapshot",
        &[
            ("snapshot", snapshot.clone()),
            ("desired_state", desired_state.clone()),
            ("database", database.clone()),
        ],
    )?;
    let identity: Value<'py> = native(
        py,
        "build_backfill_deployment_identity",
        &[("deployment_id", options.getattr("deployment_id")?)],
    )?;
    let selected_model_keys: Value<'py> = selection.getattr("selected_model_keys")?;
    let full_refresh_keys: Value<'py> = if options.getattr("full_refresh")?.is_truthy()? {
        selected_model_keys.clone()
    } else {
        empty_frozenset(py)?
    };
    let start_time_keys: Value<'py> = if start_time.is_none() {
        empty_frozenset(py)?
    } else {
        selected_model_keys.clone()
    };
    let plan: Value<'py> = native(
        py,
        "plan_deployment",
        &[
            ("desired_state", desired_state.clone()),
            ("actual_state", actual_state),
            ("default_database", database.clone()),
            ("render_resource", client.getattr("render_resource")?),
            ("deployment_id", identity.getattr("deployment_id")?),
            ("full_refresh_keys", full_refresh_keys.clone()),
            ("start_time_keys", start_time_keys.clone()),
            ("start_time", start_time.clone()),
        ],
    )?;
    let plan: Value<'py> = native(
        py,
        "add_empty_replay_source_warnings",
        &[
            ("client", client.clone()),
            ("catalog", catalog.clone()),
            ("database", database.clone()),
            ("desired_state", desired_state.clone()),
            ("plan", plan),
        ],
    )?;
    let plan: Value<'py> = native(
        py,
        "resolve_unsupported_bounded_replay_behavior",
        &[
            ("catalog", catalog.clone()),
            ("deployment_plan", plan),
            ("desired_state", desired_state.clone()),
            ("default_database", database.clone()),
            ("replay_lineage_mode", replay_lineage_mode.clone()),
        ],
    )?;
    let (execution, context) = run_scopes(py, analysis, &plan)?;
    let root_reports: Value<'py> = native(
        py,
        "build_root_backfill_reports",
        &[
            ("catalog", catalog.clone()),
            ("desired_state", desired_state.clone()),
        ],
    )?;
    if !start_time.is_none() {
        let active: Value<'py> =
            class(py, PLANNER_TYPES, "RootDeploymentStateKind")?.getattr("ACTIVE_VIEW_PRESENT")?;
        let mut invalid_names: Vec<String> = Vec::new();
        for report in items(&root_reports)? {
            if report.getattr("state_kind")?.ne(&active)? {
                let name: String = string(&report.getattr("root_key")?, "name")?;
                invalid_names.push(name);
            }
        }
        if !invalid_names.is_empty() {
            // `sorted` keeps duplicates.
            invalid_names.sort();
            return Err(error(
                py,
                CLI_EXCEPTIONS,
                "CliUserError",
                format!(
                    "--start-time requires an active published root for every selected target; \
                     found unsupported state for {}",
                    invalid_names.join(", ")
                ),
            ));
        }
    }
    let mut planned_names: Vec<Value<'py>> = Vec::new();
    for prepared in items(&plan.getattr("prepared_shadow_objects")?)? {
        planned_names.push(prepared.getattr("logical_model_name")?);
    }
    let policy_changes: Value<'py> = native(
        py,
        "plan_virtual_row_error_policy_changes",
        &[
            ("analysis", analysis.clone()),
            (
                "planned_model_names",
                PyFrozenSet::new(py, planned_names)?.into_any(),
            ),
            (
                "selected_model_keys",
                selection.getattr("selected_logical_model_keys")?,
            ),
            ("relations", catalog.getattr("relations")?),
        ],
    )?;
    build(
        py,
        BUILD_MODELS,
        "VirtualBuildPreviewContext",
        &[
            ("database", database),
            ("metadata_database", metadata_database),
            ("desired_state", desired_state),
            ("plan", plan),
            ("replay_lineage_mode", replay_lineage_mode),
            ("deployment_id", identity.getattr("deployment_id")?),
            ("created_at", identity.getattr("created_at")?),
            ("root_reports", root_reports),
            (
                "existing_relation_names",
                catalog.call_method0("relation_names")?,
            ),
            ("target_catalog", catalog),
            ("metadata_catalog", metadata_catalog),
            ("full_refresh_keys", full_refresh_keys),
            ("start_time_keys", start_time_keys),
            ("start_time", start_time),
            (
                "execution_logical_model_keys",
                selection.getattr("execution_logical_model_keys")?,
            ),
            ("run_execution_scope", tuple(py, execution)?),
            ("run_context_scope", tuple(py, context)?),
            ("row_error_policy_changes", policy_changes),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_direct_build_preview, module)?)?;
    module.add_function(wrap_pyfunction!(build_virtual_build_preview, module)?)
}
