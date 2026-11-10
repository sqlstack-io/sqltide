//! The `sqt build` and `sqt plan` command entries: analyze the project, initialize metadata and
//! publish the manifest, prepare the connected workflow, then run the direct, virtual or mixed
//! build command, or publish and print the plan. Failures the commands report are recorded as
//! terminal invocations and printed through Python's `sys.stderr`.

use std::time::Instant;

use pyo3::exceptions::{PyKeyboardInterrupt, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::build_command::{int, invocation_record, print, quietly};
use crate::direct_build::{Value, build, call, class, is_a, items, none, or, text, tuple};
use crate::handling::while_handling;

const NATIVE: &str = "sqltide._native";
const BUILD_MODELS: &str = "sqltide.cli.build.models";
const BUILD_CONSTANTS: &str = "sqltide.cli.build.constants";
const BUILD_EXCEPTIONS: &str = "sqltide.cli.build.exceptions";
const OBSERVABILITY_MODELS: &str = "sqltide.executor.observability.models";
const OBSERVABILITY_CONSTANTS: &str = "sqltide.executor.observability.constants";
const START_INVOCATION: &str = "sqltide.executor.observability.main.start_invocation";
const INITIALIZE_OBSERVABILITY: &str = "sqltide._native";
const PUBLISH_MANIFEST: &str = "sqltide._native";
const TERMINAL_OBSERVATIONS: &str =
    "sqltide.executor.observability.main.persist_terminal_observations";
const PUBLISH_PLAN: &str = "sqltide.cli.workflow_artifacts.main._publish_plan_workflow";
const SIGINT_EXIT_CODE: i64 = 130;

/// The exceptions a build or plan reports as a failed command, as `(module, class)`.
const REPORTED: [(&str, &str); 6] = [
    (
        "sqltide.compiler.compile.exceptions",
        "TransformSqlContractError",
    ),
    ("sqltide.cli.entry.exceptions", "CliUserError"),
    ("sqltide.compiler.planner.exceptions", "DirectPlanError"),
    ("sqltide.executor.direct.exceptions", "DirectBuildError"),
    (
        "sqltide.executor.backfill.exceptions",
        "BackfillExecutionError",
    ),
    ("sqltide.adapter.exceptions", "AdapterError"),
];
const WORKFLOW_ERROR: (&str, &str) = (
    "sqltide.executor.workflow.exceptions",
    "WorkflowExecutionError",
);

fn native<'py>(py: Python<'py>, name: &str, pairs: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
    call(py, NATIVE, name, pairs)
}

fn caught(py: Python<'_>, error: &PyErr, (module, name): (&str, &str)) -> PyResult<bool> {
    Ok(error.is_instance(py, &class(py, module, name)?))
}

fn reported(py: Python<'_>, error: &PyErr) -> PyResult<bool> {
    for exception in REPORTED {
        if caught(py, error, exception)? {
            return Ok(true);
        }
    }
    Ok(error.is_instance_of::<PyValueError>(py))
}

/// `os.environ.get(<constant>)`.
fn environment<'py>(py: Python<'py>, module: &str, constant: &str) -> PyResult<Value<'py>> {
    py.import("os")?
        .getattr("environ")?
        .call_method1("get", (py.import(module)?.getattr(constant)?,))
}

fn elapsed_ms(py: Python<'_>, started: Instant) -> PyResult<Value<'_>> {
    int(py, (started.elapsed().as_nanos() / 1_000_000) as i64)
}

fn analyze<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    native(
        py,
        "analyze_project",
        &[
            ("pipelines_root", options.getattr("pipelines_root")?),
            ("loaded_project", loaded_project.clone()),
            ("adapter_profile", adapter_profile.clone()),
        ],
    )
}

fn preparation_options<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    metadata_database: Value<'py>,
) -> PyResult<Value<'py>> {
    let field = |name: &str| options.getattr(name);
    build(
        py,
        BUILD_MODELS,
        "WorkflowPreparationOptions",
        &[
            ("database", field("database")?),
            ("metadata_database", metadata_database),
            ("selectors", field("selectors")?),
            ("deployment_id", field("deployment_id")?),
            ("full_refresh", field("full_refresh")?),
            ("start_time", field("start_time")?),
            ("verbose", field("verbose")?),
            ("changed", field("changed")?),
            (
                "include_missing_upstream",
                field("include_missing_upstream")?,
            ),
        ],
    )
}

/// Record a build that ended before any mode ran; the observation warning is discarded.
fn persist_unrouted_build<'py>(
    py: Python<'py>,
    started: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    (target_database, target_identity): (Value<'py>, Value<'py>),
    (outcome, exit_code, error_message): (&str, i64, Value<'py>),
) -> PyResult<()> {
    let invocation: Value<'py> = invocation_record(
        py,
        started,
        &[
            (
                "project_dir",
                options.getattr("pipelines_root")?.getattr("parent")?,
            ),
            ("target_identity", target_identity),
            ("command", text(py, "build")),
            ("mode", none(py)),
            ("outcome", text(py, outcome)),
            ("exit_code", int(py, exit_code)?),
            ("materialized_outcome", none(py)),
            ("deployment_id", options.getattr("deployment_id")?),
            ("workflow_id", none(py)),
            ("selected_node_count", int(py, 0)?),
            ("error_message", error_message),
            ("summary", PyDict::new(py).into_any()),
        ],
    )?;
    call(
        py,
        TERMINAL_OBSERVATIONS,
        "persist_terminal_observations",
        &[
            ("client", client.clone()),
            ("database", target_database),
            ("invocation", invocation),
            ("node_results", tuple(py, Vec::new())?),
        ],
    )?;
    Ok(())
}

fn persist_failed_build<'py>(
    py: Python<'py>,
    started: &Value<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    error_message: Value<'py>,
    database: Value<'py>,
) -> PyResult<()> {
    let empty: Value<'py> = text(py, "");
    let requested: Value<'py> = options.getattr("database")?;
    let target_database: Value<'py> = or(
        options.getattr("metadata_database")?,
        &or(database.clone(), &or(requested.clone(), &empty)?)?,
    )?;
    let target_identity: Value<'py> = or(database, &or(requested, &empty)?)?;
    persist_unrouted_build(
        py,
        started,
        options,
        client,
        (target_database, target_identity),
        ("failed", 1, error_message),
    )
}

/// Plan and execute one build through the effective project mode.
#[pyfunction]
#[pyo3(signature = (*, options, client, observation_client, loaded_project, adapter_profile))]
fn run_build<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    let invocation_id: Value<'py> =
        environment(py, OBSERVABILITY_CONSTANTS, "RUN_INVOCATION_ID_ENV_VAR")?;
    let started: Value<'py> = call(
        py,
        START_INVOCATION,
        "start_invocation",
        &[("invocation_id", invocation_id)],
    )?;
    let mut resolved_database: Value<'py> = or(options.getattr("database")?, &text(py, ""))?;
    let mut attempt = || -> PyResult<Value<'py>> {
        for flag in ["json", "events"] {
            if options.getattr(format!("{flag}_output"))?.is_truthy()?
                && !options.getattr("auto_approve")?.is_truthy()?
            {
                let message: String = format!("--{flag} requires --auto-approve for build");
                persist_failed_build(py, &started, options, client, text(py, &message), none(py))?;
                print(py, &text(py, &message), true)?;
                return int(py, 1);
            }
        }
        let compile_started: Instant = Instant::now();
        let analysis: Value<'py> = analyze(py, options, loaded_project, adapter_profile)?;
        let compile_ms: Value<'py> = elapsed_ms(py, compile_started)?;
        let pipelines: Value<'py> = PyList::new(
            py,
            items(&analysis.getattr("compile_inputs")?.getattr("pipelines")?)?,
        )?
        .into_any();
        let database: Value<'py> = crate::cli::invocation::resolve_default_database(
            py,
            &pipelines,
            options.getattr("database")?,
        )?;
        resolved_database = database.clone();
        let observability_started: Instant = Instant::now();
        let metadata_database: Value<'py> = or(options.getattr("metadata_database")?, &database)?;
        call(
            py,
            INITIALIZE_OBSERVABILITY,
            "initialize_observability",
            &[
                ("connection", observation_client.clone()),
                ("database", metadata_database.clone()),
            ],
        )?;
        let project_identity: Value<'py> = native(
            py,
            "resolve_manifest_project_identity",
            &[("analysis", analysis.clone())],
        )?;
        let manifest: Value<'py> = native(
            py,
            "build_manifest",
            &[
                ("analysis", analysis.clone()),
                ("invocation_id", started.get_item(0)?),
                ("project_identity", project_identity),
                ("target_database", database.clone()),
                (
                    "tool_version",
                    py.import(BUILD_CONSTANTS)?
                        .getattr("SQLTIDE_TOOL_VERSION")?,
                ),
                (
                    "project_revision",
                    environment(py, OBSERVABILITY_CONSTANTS, "PROJECT_REVISION_ENV_VAR")?,
                ),
            ],
        )?;
        call(
            py,
            PUBLISH_MANIFEST,
            "publish_manifest",
            &[
                ("connection", observation_client.clone()),
                ("database", metadata_database),
                ("manifest", manifest),
            ],
        )?;
        let observability_ms: Value<'py> = elapsed_ms(py, observability_started)?;
        let preparation_options: Value<'py> =
            preparation_options(py, options, options.getattr("metadata_database")?)?;
        let planning_started: Instant = Instant::now();
        let preparation: Value<'py> = native(
            py,
            "prepare_validated_build",
            &[
                ("analysis", analysis),
                ("options", preparation_options),
                ("client", client.clone()),
                ("adapter_profile", adapter_profile.clone()),
            ],
        )?;
        let planning_ms: Value<'py> = elapsed_ms(py, planning_started)?;
        let startup_timings: Value<'py> = build(
            py,
            OBSERVABILITY_MODELS,
            "RunStartupTimings",
            &[
                ("compile_ms", compile_ms),
                ("observability_ms", observability_ms),
                ("planning_ms", planning_ms),
            ],
        )?;
        let command: &str = if is_a(&preparation, BUILD_MODELS, "MixedWorkflowPreparation")? {
            "execute_mixed_build_command"
        } else if is_a(&preparation, BUILD_MODELS, "VirtualWorkflowPreparation")? {
            "execute_virtual_build_command"
        } else {
            "execute_direct_build_command"
        };
        native(
            py,
            command,
            &[
                ("preparation", preparation),
                ("options", options.clone()),
                ("client", client.clone()),
                ("observation_client", observation_client.clone()),
                ("started", started.clone()),
                ("startup_timings", startup_timings),
            ],
        )
    };
    let error: PyErr = match attempt() {
        Ok(exit_code) => return Ok(exit_code),
        Err(error) => error,
    };
    let handle = || -> PyResult<Value<'py>> {
        if caught(py, &error, (BUILD_EXCEPTIONS, "BuildPipelineLimitError"))? {
            print(py, error.value(py).str()?.as_any(), true)?;
            return int(py, 1);
        }
        if reported(py, &error)?
            || caught(py, &error, WORKFLOW_ERROR)?
            || error.is_instance_of::<PyOSError>(py)
        {
            let message: Value<'py> = error.value(py).str()?.into_any();
            persist_failed_build(
                py,
                &started,
                options,
                client,
                message.clone(),
                resolved_database.clone(),
            )?;
            print(py, &message, true)?;
            return int(py, 1);
        }
        if error.is_instance_of::<PyKeyboardInterrupt>(py) {
            let target_database: Value<'py> =
                or(options.getattr("metadata_database")?, &resolved_database)?;
            persist_unrouted_build(
                py,
                &started,
                options,
                client,
                (target_database, resolved_database.clone()),
                ("cancelled", SIGINT_EXIT_CODE, none(py)),
            )?;
            quietly(py, || {
                let message: Value<'py> = native(py, "render_interrupted_build_message", &[])?;
                print(py, &message, true)
            })?;
            return int(py, SIGINT_EXIT_CODE);
        }
        Err(error.clone_ref(py))
    };
    handle().map_err(|raised| while_handling(py, raised, &error))
}

/// Plan the effective mode's execution against live warehouse state.
#[pyfunction]
#[pyo3(signature = (*, options, client, loaded_project, adapter_profile))]
fn run_plan<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<i64> {
    let attempt = || -> PyResult<()> {
        let analysis: Value<'py> = analyze(py, options, loaded_project, adapter_profile)?;
        let preparation_options: Value<'py> = preparation_options(py, options, none(py))?;
        let preparation: Value<'py> = native(
            py,
            "prepare_build_workflow",
            &[
                ("analysis", analysis),
                ("options", preparation_options),
                ("client", client.clone()),
                ("adapter_profile", adapter_profile.clone()),
            ],
        )?;
        let mixed: bool = is_a(&preparation, BUILD_MODELS, "MixedWorkflowPreparation")?;
        if !mixed {
            let direct: bool = is_a(&preparation, BUILD_MODELS, "DirectWorkflowPreparation")?;
            let workflow: Value<'py> = preparation.getattr("workflow")?;
            call(
                py,
                PUBLISH_PLAN,
                "publish_plan_workflow",
                &[
                    (
                        "target_dir",
                        options
                            .getattr("pipelines_root")?
                            .getattr("parent")?
                            .call_method1("__truediv__", ("target",))?,
                    ),
                    (
                        "workflow",
                        if direct {
                            workflow.getattr("template")?
                        } else {
                            workflow
                        },
                    ),
                    (
                        "is_template",
                        direct.into_pyobject(py)?.to_owned().into_any(),
                    ),
                ],
            )?;
        }
        let rendered: Value<'py> = if options.getattr("json_output")?.is_truthy()? {
            if mixed {
                preparation.getattr("plan_json")?
            } else {
                preparation.getattr("workflow")?.getattr("plan_json")?
            }
        } else {
            preparation.getattr("plan_text")?.add("\n")?
        };
        let arguments: Bound<'py, PyDict> = PyDict::new(py);
        arguments.set_item("end", "")?;
        py.import("builtins")?
            .getattr("print")?
            .call((rendered,), Some(&arguments))?;
        Ok(())
    };
    match attempt() {
        Ok(()) => Ok(0),
        Err(error) if reported(py, &error)? => {
            (|| print(py, error.value(py).str()?.as_any(), true))()
                .map_err(|raised| while_handling(py, raised, &error))?;
            Ok(1)
        }
        Err(error) => Err(error),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_build, module)?)?;
    module.add_function(wrap_pyfunction!(run_plan, module)?)
}
