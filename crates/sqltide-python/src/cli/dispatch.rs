//! Dispatch one resolved invocation to its command handler with the options it takes.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyTuple};
use pyo3::wrap_pyfunction;

use crate::cli::invocation::Invocation;
use crate::cli::python::{flag, getattr_none, getattr_or, user_error};
use crate::cli::sql::run_sql;
use crate::dev_server::support::attr;

const NATIVE: &str = "sqltide._native";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const AUTH_TYPES: &str = "sqltide.auth.types";
const DEFAULT_DEV_SERVER_HOST: &str = "127.0.0.1";
const DEFAULT_DEV_SERVER_PORT: i64 = 8000;
const DEFAULT_SESSION_TTL_SECONDS: i64 = 43200;
const TRUE_ENV_VALUES: [&str; 3] = ["1", "true", "yes"];
/// Each handler name and where its default implementation lives.
const DEFAULT_HANDLERS: [(&str, &str, &str); 18] = [
    ("run_discover", NATIVE, "run_discover"),
    ("run_compile", NATIVE, "run_compile"),
    ("run_test", NATIVE, "run_test"),
    ("run_audit", NATIVE, "run_audit"),
    ("run_plan", NATIVE, "run_plan"),
    ("run_build", NATIVE, "run_build"),
    ("run_destruction", NATIVE, "run_destruction"),
    ("run_deployment_diff", NATIVE, "run_deployment_diff"),
    ("run_deployment_list", NATIVE, "run_deployment_list"),
    ("run_deployment_show", NATIVE, "run_deployment_show"),
    ("run_deployment_audit", NATIVE, "run_deployment_audit"),
    ("run_deployment_promote", NATIVE, "run_deployment_promotion"),
    ("run_deployment_rollback", NATIVE, "run_deployment_rollback"),
    ("run_reconcile", NATIVE, "run_reconcile"),
    ("run_janitor", NATIVE, "run_janitor"),
    ("run_doctor", NATIVE, "run_doctor"),
    ("run_repair_active_view", NATIVE, "run_repair_active_view"),
    ("run_dev", NATIVE, "run_dev"),
];

type Entries<'py> = Vec<(&'static str, Bound<'py, PyAny>)>;

/// The handler `name`: the supplied handlers' attribute, else the default.
fn handler<'py>(
    py: Python<'py>,
    handlers: Option<&Bound<'py, PyAny>>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    if let Some(handlers) = handlers.filter(|handlers| !handlers.is_none()) {
        return handlers.getattr(name);
    }
    let (_, module, attribute) = DEFAULT_HANDLERS
        .iter()
        .find(|(handler, _, _)| *handler == name)
        .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err(name.to_owned()))?;
    attr(py, module, attribute)
}

fn kwargs<'py>(py: Python<'py>, entries: Entries<'py>) -> PyResult<Bound<'py, PyDict>> {
    let dict: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in entries {
        dict.set_item(name, value)?;
    }
    Ok(dict)
}

/// `handlers.<name>(**entries)` as an exit status.
fn call_handler<'py>(
    py: Python<'py>,
    handlers: Option<&Bound<'py, PyAny>>,
    name: &str,
    entries: Entries<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    handler(py, handlers, name)?.call((), Some(&kwargs(py, entries)?))
}

/// `model(**entries)` from `module`.
fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    entries: Entries<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    attr(py, module, name)?.call((), Some(&kwargs(py, entries)?))
}

fn boolean(py: Python<'_>, value: bool) -> Bound<'_, PyAny> {
    PyBool::new(py, value).to_owned().into_any()
}

/// `tuple(getattr(args, name, default))`.
fn tuple_of<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let value: Bound<'py, PyAny> = getattr_or(args, name, PyTuple::empty(py).into_any())?;
    py.get_type::<PyTuple>().call1((value,))
}

/// The compiler's connection-free view of an adapter.
pub(crate) fn compiler_adapter_profile<'py>(
    py: Python<'py>,
    adapter: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let dialect: Bound<'py, PyAny> = adapter.getattr("sql_analysis_dialect")?;
    build(
        py,
        COMPILE_MODELS,
        "CompilerAdapterProfile",
        vec![
            ("identity", adapter.getattr("identity")?),
            ("sql_analysis_dialect", dialect.clone()),
            (
                "type_inference_profile",
                build(
                    py,
                    COMPILE_MODELS,
                    "CompilerExpressionInferenceProfile",
                    vec![("sql_analysis_dialect", dialect)],
                )?,
            ),
            (
                "target_metadata",
                build(
                    py,
                    COMPILE_MODELS,
                    "CompilerTargetMetadata",
                    vec![
                        ("default_database", adapter.getattr("default_database")?),
                        ("default_schema", adapter.getattr("default_schema")?),
                    ],
                )?,
            ),
            ("realize_source", adapter.getattr("realize_source")?),
            (
                "model_relation_name",
                adapter.getattr("model_relation_name")?,
            ),
            ("realize_model", adapter.getattr("realize_model")?),
            ("render_resource", adapter.getattr("render_resource")?),
            (
                "render_set_difference_comparison",
                adapter.getattr("render_set_difference_comparison")?,
            ),
        ],
    )
}

/// Capture compiler-facing adapter behaviour without opening a warehouse connection.
#[pyfunction]
fn build_compiler_adapter_profile<'py>(
    py: Python<'py>,
    adapter: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    compiler_adapter_profile(py, adapter)
}

fn require_pipelines_root<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    invocation
        .pipelines_root
        .clone()
        .ok_or_else(|| user_error(py, "Command requires a resolved pipelines root", None))
}

/// `invocation.project_dir or pipelines_root.parent`.
fn project_dir<'py>(py: Python<'py>, invocation: &Invocation<'py>) -> PyResult<Bound<'py, PyAny>> {
    match &invocation.project_dir {
        Some(project_dir) => Ok(project_dir.clone()),
        None => require_pipelines_root(py, invocation)?.getattr("parent"),
    }
}

fn default_control_store_url<'py>(
    py: Python<'py>,
    project_dir: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    build(
        py,
        "sqltide._native",
        "default_control_store_url",
        vec![("project_dir", project_dir.clone())],
    )
}

/// `first or second`.
fn or<'py>(
    first: Bound<'py, PyAny>,
    second: impl FnOnce() -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    if first.is_truthy()? {
        Ok(first)
    } else {
        second()
    }
}

/// Dispatch one resolved invocation; the command's exit status.
#[allow(clippy::too_many_lines)]
pub(crate) fn dispatch_cli_command<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    handlers: Option<&Bound<'py, PyAny>>,
    adapter_connection: Option<&Bound<'py, PyAny>>,
    observation_connection: Option<&Bound<'py, PyAny>>,
    connection_factory: Option<&Bound<'py, PyAny>>,
    observation_connection_factory: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let args: &Bound<'py, PyAny> = &invocation.args;
    let none: Bound<'py, PyAny> = py.None().into_bound(py);
    let optional = |value: Option<&Bound<'py, PyAny>>| -> Bound<'py, PyAny> {
        value.cloned().unwrap_or_else(|| none.clone())
    };
    let mut adapter_profile: Bound<'py, PyAny> = compiler_adapter_profile(py, &invocation.adapter)?;
    if flag(args, "no_sql_analysis")? {
        let replace_kwargs: Bound<'py, PyDict> = PyDict::new(py);
        replace_kwargs.set_item("run_sql_rules", false)?;
        adapter_profile =
            attr(py, "dataclasses", "replace")?.call((adapter_profile,), Some(&replace_kwargs))?;
    }
    let command: &str = invocation.command.as_str();
    let loaded_project: Bound<'py, PyAny> = invocation.loaded_project.clone();
    let pipelines_root: Bound<'py, PyAny> = optional(invocation.pipelines_root.as_ref());
    let invocation_project_dir: Bound<'py, PyAny> = optional(invocation.project_dir.as_ref());
    let database: Bound<'py, PyAny> = invocation.database.clone();
    let json_output = || -> PyResult<Bound<'py, PyAny>> { Ok(boolean(py, flag(args, "json")?)) };
    let metadata_database = || getattr_none(args, "metadata_database");
    if command == "lint" || command == "format" {
        return Ok(run_sql(py, invocation, &adapter_profile)?
            .into_pyobject(py)?
            .into_any());
    }
    if command == "discover" {
        return call_handler(
            py,
            handlers,
            "run_discover",
            vec![
                ("pipelines_root", pipelines_root),
                ("loaded_project", loaded_project),
                ("adapter_profile", adapter_profile),
            ],
        );
    }
    if command == "compile" {
        let target_dir: Bound<'py, PyAny> = or(getattr_none(args, "target_dir")?, || {
            match &invocation.project_dir {
                Some(project_dir) => project_dir.call_method1("__truediv__", ("target",)),
                None => Ok(none.clone()),
            }
        })?;
        return call_handler(
            py,
            handlers,
            "run_compile",
            vec![
                ("pipelines_root", pipelines_root),
                ("loaded_project", loaded_project),
                ("adapter_profile", adapter_profile),
                ("target_dir", target_dir),
            ],
        );
    }
    if command == "dev" {
        return dispatch_dev(
            py,
            invocation,
            handlers,
            adapter_profile,
            [
                adapter_connection,
                observation_connection,
                connection_factory,
                observation_connection_factory,
            ],
        );
    }
    let client: Bound<'py, PyAny> = adapter_connection
        .cloned()
        .ok_or_else(|| user_error(py, "Command requires a resolved adapter connection", None))?;
    match command {
        "test" => call_handler(
            py,
            handlers,
            "run_test",
            vec![
                ("pipelines_root", pipelines_root),
                ("project_dir", invocation_project_dir),
                ("selectors", tuple_of(py, args, "select")?),
                ("paths", tuple_of(py, args, "paths")?),
                ("verbose", boolean(py, flag(args, "verbose")?)),
                ("client", client),
                ("loaded_project", loaded_project),
                ("adapter_profile", adapter_profile),
                ("target_dir", getattr_none(args, "target_dir")?),
                (
                    "database",
                    or(database, || {
                        Ok(pyo3::types::PyString::new(py, "").into_any())
                    })?,
                ),
            ],
        ),
        "build" => {
            let observation: Bound<'py, PyAny> =
                observation_connection.cloned().ok_or_else(|| {
                    user_error(
                        py,
                        "Build requires a dedicated observation connection",
                        None,
                    )
                })?;
            let options: Bound<'py, PyAny> = build(
                py,
                "sqltide.cli.build.models",
                "BuildCommandOptions",
                vec![
                    ("pipelines_root", require_pipelines_root(py, invocation)?),
                    ("database", database),
                    ("metadata_database", metadata_database()?),
                    ("selectors", tuple_of(py, args, "select")?),
                    ("deployment_id", getattr_none(args, "deployment_id")?),
                    ("full_refresh", boolean(py, flag(args, "full_refresh")?)),
                    ("start_time", getattr_none(args, "start_time")?),
                    ("json_output", json_output()?),
                    ("verbose", boolean(py, flag(args, "verbose")?)),
                    ("auto_approve", boolean(py, flag(args, "auto_approve")?)),
                    ("events_output", boolean(py, flag(args, "events")?)),
                    ("confirmations", tuple_of(py, args, "confirm")?),
                    ("changed", boolean(py, flag(args, "changed")?)),
                    (
                        "include_missing_upstream",
                        boolean(py, flag(args, "include_missing_upstream")?),
                    ),
                ],
            )?;
            call_handler(
                py,
                handlers,
                "run_build",
                vec![
                    ("options", options),
                    ("client", client),
                    ("observation_client", observation),
                    ("loaded_project", loaded_project),
                    ("adapter_profile", adapter_profile),
                ],
            )
        }
        "destroy" | "reset-target" => {
            let observation: Bound<'py, PyAny> =
                observation_connection.cloned().ok_or_else(|| {
                    user_error(
                        py,
                        "Destruction requires a dedicated observation connection",
                        None,
                    )
                })?;
            let project_dir: Bound<'py, PyAny> = project_dir(py, invocation)?;
            let operations: Bound<'py, PyAny> = attr(
                py,
                "sqltide.executor.destruction.types",
                "DestructionOperation",
            )?;
            let operation: Bound<'py, PyAny> = operations.getattr(if command == "destroy" {
                "DESTROY_PIPELINES"
            } else {
                "RESET_TARGET"
            })?;
            let control_store_url: Bound<'py, PyAny> =
                or(getattr_none(args, "control_store_url")?, || {
                    default_control_store_url(py, &project_dir)
                })?;
            let options: Bound<'py, PyAny> = build(
                py,
                "sqltide.cli.destruction.models",
                "DestructionCommandOptions",
                vec![
                    ("operation", operation),
                    ("pipelines_root", require_pipelines_root(py, invocation)?),
                    ("project_dir", project_dir),
                    ("selected_target", args.getattr("target")?.str()?.into_any()),
                    ("database", database),
                    ("selectors", tuple_of(py, args, "select")?),
                    (
                        "include_orphans",
                        boolean(py, flag(args, "include_orphans")?),
                    ),
                    ("control_store_url", control_store_url),
                    ("cli_variables", invocation.cli_variables.clone()),
                    ("environment", invocation.connection.environment.clone()),
                ],
            )?;
            call_handler(
                py,
                handlers,
                "run_destruction",
                vec![
                    ("options", options),
                    ("client", client),
                    ("observation_client", observation),
                    ("loaded_project", loaded_project),
                    ("adapter_profile", adapter_profile),
                ],
            )
        }
        "deployment" => {
            let deployment_command: String = args.getattr("deployment_command")?.str()?.to_string();
            match deployment_command.as_str() {
                "list" => call_handler(
                    py,
                    handlers,
                    "run_deployment_list",
                    vec![
                        ("database", database),
                        ("metadata_database", metadata_database()?),
                        ("json_output", json_output()?),
                        ("client", client),
                    ],
                ),
                "show" => call_handler(
                    py,
                    handlers,
                    "run_deployment_show",
                    vec![
                        ("deployment_id", args.getattr("deployment_id")?),
                        ("database", database),
                        ("metadata_database", metadata_database()?),
                        ("json_output", json_output()?),
                        ("client", client),
                    ],
                ),
                "diff" => call_handler(
                    py,
                    handlers,
                    "run_deployment_diff",
                    vec![
                        ("database", database),
                        ("metadata_database", metadata_database()?),
                        ("comparison", args.getattr("comparison")?),
                        ("json_output", json_output()?),
                        ("client", client),
                    ],
                ),
                "audit" => {
                    let audit_project_dir: Bound<'py, PyAny> =
                        match (&invocation.project_dir, &invocation.pipelines_root) {
                            (Some(project_dir), _) => project_dir.clone(),
                            (None, Some(root)) => root.getattr("parent")?,
                            (None, None) => none.clone(),
                        };
                    call_handler(
                        py,
                        handlers,
                        "run_deployment_audit",
                        vec![
                            ("pipelines_root", pipelines_root),
                            ("project_dir", audit_project_dir),
                            ("database", database),
                            ("metadata_database", metadata_database()?),
                            ("deployment_id", args.getattr("deployment_id")?),
                            ("json_output", json_output()?),
                            ("client", client),
                            ("loaded_project", loaded_project),
                            ("adapter_profile", adapter_profile),
                        ],
                    )
                }
                "rollback" => call_handler(
                    py,
                    handlers,
                    "run_deployment_rollback",
                    vec![
                        ("database", database),
                        ("metadata_database", metadata_database()?),
                        ("deployment_id", getattr_none(args, "deployment_id")?),
                        ("previous", boolean(py, flag(args, "previous")?)),
                        ("auto_approve", boolean(py, flag(args, "auto_approve")?)),
                        ("json_output", json_output()?),
                        ("client", client),
                    ],
                ),
                _ => call_handler(
                    py,
                    handlers,
                    "run_deployment_promote",
                    vec![
                        ("database", database),
                        ("metadata_database", metadata_database()?),
                        ("deployment_id", args.getattr("deployment_id")?),
                        ("json_output", json_output()?),
                        ("client", client),
                    ],
                ),
            }
        }
        "audit" => {
            let root: Bound<'py, PyAny> = invocation.pipelines_root.clone().ok_or_else(|| {
                user_error(py, "Audit command requires a resolved pipelines root", None)
            })?;
            let audit_project_dir: Bound<'py, PyAny> = match &invocation.project_dir {
                Some(project_dir) => project_dir.clone(),
                None => root.getattr("parent")?,
            };
            call_handler(
                py,
                handlers,
                "run_audit",
                vec![
                    ("pipelines_root", root),
                    ("project_dir", audit_project_dir),
                    ("database", database),
                    ("selectors", tuple_of(py, args, "select")?),
                    ("json_output", json_output()?),
                    ("client", client),
                    ("loaded_project", loaded_project),
                    ("adapter_profile", adapter_profile),
                    ("force", boolean(py, flag(args, "force")?)),
                ],
            )
        }
        "reconcile" => call_handler(
            py,
            handlers,
            "run_reconcile",
            vec![
                ("pipelines_root", pipelines_root),
                ("database", database),
                ("metadata_database", metadata_database()?),
                ("selectors", tuple_of(py, args, "select")?),
                ("json_output", json_output()?),
                ("apply", boolean(py, flag(args, "apply")?)),
                ("client", client),
                ("loaded_project", loaded_project),
                ("adapter_profile", adapter_profile),
            ],
        ),
        "rename-project" => {
            let project_dir: Bound<'py, PyAny> = project_dir(py, invocation)?;
            build(
                py,
                NATIVE,
                "run_rename_project",
                vec![
                    ("pipelines_root", require_pipelines_root(py, invocation)?),
                    ("project_dir", project_dir),
                    ("new_name", args.getattr("new_name")?),
                    ("from_name", getattr_none(args, "from_name")?),
                    ("environment", invocation.connection.environment.clone()),
                    ("database", database),
                    (
                        "control_store_url",
                        getattr_none(args, "control_store_url")?,
                    ),
                    ("apply", boolean(py, flag(args, "apply")?)),
                    ("client", client),
                    ("loaded_project", loaded_project),
                    ("adapter_profile", adapter_profile),
                ],
            )
        }
        "doctor" => call_handler(
            py,
            handlers,
            "run_doctor",
            vec![("database", database), ("client", client)],
        ),
        "janitor" => call_handler(
            py,
            handlers,
            "run_janitor",
            vec![
                ("database", database),
                ("retention_days", args.getattr("retention_days")?),
                (
                    "minimum_rollback_deployments",
                    args.getattr("minimum_rollback_deployments")?,
                ),
                ("apply", boolean(py, flag(args, "apply")?)),
                ("json_output", json_output()?),
                ("client", client),
            ],
        ),
        "repair" => call_handler(
            py,
            handlers,
            "run_repair_active_view",
            vec![
                ("database", database),
                ("table", args.getattr("table")?),
                ("deployment_id", args.getattr("deployment_id")?),
                ("client", client),
            ],
        ),
        _ => {
            let options: Bound<'py, PyAny> = build(
                py,
                "sqltide.cli.plan.models",
                "PlanCommandOptions",
                vec![
                    ("pipelines_root", require_pipelines_root(py, invocation)?),
                    ("database", database),
                    ("selectors", tuple_of(py, args, "select")?),
                    ("full_refresh", boolean(py, flag(args, "full_refresh")?)),
                    ("start_time", getattr_none(args, "start_time")?),
                    ("deployment_id", getattr_none(args, "deployment_id")?),
                    ("json_output", json_output()?),
                    ("verbose", boolean(py, flag(args, "verbose")?)),
                    ("changed", boolean(py, flag(args, "changed")?)),
                    (
                        "include_missing_upstream",
                        boolean(py, flag(args, "include_missing_upstream")?),
                    ),
                ],
            )?;
            call_handler(
                py,
                handlers,
                "run_plan",
                vec![
                    ("options", options),
                    ("client", client),
                    ("loaded_project", loaded_project),
                    ("adapter_profile", adapter_profile),
                ],
            )
        }
    }
}

/// `sqt dev`: authentication settings from options, then environment, then defaults.
fn dispatch_dev<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    handlers: Option<&Bound<'py, PyAny>>,
    adapter_profile: Bound<'py, PyAny>,
    [
        client,
        observation_client,
        connection_factory,
        observation_connection_factory,
    ]: [Option<&Bound<'py, PyAny>>; 4],
) -> PyResult<Bound<'py, PyAny>> {
    let args: &Bound<'py, PyAny> = &invocation.args;
    let none: Bound<'py, PyAny> = py.None().into_bound(py);
    let optional = |value: Option<&Bound<'py, PyAny>>| -> Bound<'py, PyAny> {
        value.cloned().unwrap_or_else(|| none.clone())
    };
    let loaded_project: &Bound<'py, PyAny> = &invocation.loaded_project;
    let effective_target: Bound<'py, PyAny> = if loaded_project.is_none()
        || loaded_project.getattr("effective_configuration")?.is_none()
    {
        none.clone()
    } else {
        loaded_project
            .getattr("effective_configuration")?
            .getattr("target_name")?
    };
    let project_dir: Bound<'py, PyAny> = project_dir(py, invocation)?;
    let environment: Bound<'py, PyAny> = if invocation.connection.environment.is_truthy()? {
        invocation.connection.environment.clone()
    } else {
        PyDict::new(py).into_any()
    };
    let setting = |option: &str, variable: &str, default: Option<Bound<'py, PyAny>>| {
        or(getattr_none(args, option)?, || match default {
            Some(default) => environment.call_method1("get", (variable, default)),
            None => environment.call_method1("get", (variable,)),
        })
    };
    let text = |value: &str| pyo3::types::PyString::new(py, value).into_any();
    let mode: Bound<'py, PyAny> = attr(py, AUTH_TYPES, "AuthenticationMode")?.call1((setting(
        "auth_mode",
        "SQLTIDE_AUTH_MODE",
        Some(attr(py, AUTH_TYPES, "AuthenticationMode")?.getattr("DISABLED")?),
    )?,))?;
    let control_store_url: Bound<'py, PyAny> = or(
        setting("control_store_url", "SQLTIDE_CONTROL_STORE_URL", None)?,
        || default_control_store_url(py, &project_dir),
    )?;
    let unknown_user_policy: Bound<'py, PyAny> =
        attr(py, AUTH_TYPES, "UnknownUserPolicy")?.call1((setting(
            "auth_unknown_user_policy",
            "SQLTIDE_AUTH_UNKNOWN_USER_POLICY",
            Some(attr(py, AUTH_TYPES, "UnknownUserPolicy")?.getattr("AUTO_PROVISION")?),
        )?,))?;
    let session_ttl_seconds: Bound<'py, PyAny> = py.get_type::<PyInt>().call1((setting(
        "auth_session_ttl_seconds",
        "SQLTIDE_AUTH_SESSION_TTL_SECONDS",
        Some(DEFAULT_SESSION_TTL_SECONDS.into_pyobject(py)?.into_any()),
    )?,))?;
    let insecure_value: String = environment
        .call_method1("get", ("SQLTIDE_AUTH_INSECURE_COOKIE", ""))?
        .call_method0("casefold")?
        .extract()?;
    let session_cookie_secure: bool =
        !flag(args, "auth_insecure_cookie")? && !TRUE_ENV_VALUES.contains(&insecure_value.as_str());
    let auth_settings: Bound<'py, PyAny> = build(
        py,
        "sqltide.auth.models",
        "AuthSettings",
        vec![
            ("mode", mode),
            ("control_store_url", control_store_url),
            (
                "username_header",
                setting(
                    "auth_username_header",
                    "SQLTIDE_AUTH_USERNAME_HEADER",
                    Some(text("X-SQLTide-User")),
                )?,
            ),
            (
                "display_name_header",
                setting(
                    "auth_display_name_header",
                    "SQLTIDE_AUTH_DISPLAY_NAME_HEADER",
                    None,
                )?,
            ),
            (
                "email_header",
                setting("auth_email_header", "SQLTIDE_AUTH_EMAIL_HEADER", None)?,
            ),
            ("unknown_user_policy", unknown_user_policy),
            (
                "default_role",
                setting(
                    "auth_default_role",
                    "SQLTIDE_AUTH_DEFAULT_ROLE",
                    Some(text("viewer")),
                )?,
            ),
            ("session_ttl_seconds", session_ttl_seconds),
            ("session_cookie_secure", boolean(py, session_cookie_secure)),
            (
                "proxy_logout_url",
                setting(
                    "auth_proxy_logout_url",
                    "SQLTIDE_AUTH_PROXY_LOGOUT_URL",
                    None,
                )?,
            ),
        ],
    )?;
    let selected_target: Bound<'py, PyAny> = or(effective_target, || getattr_none(args, "target"))?;
    let variables: Bound<'py, PyAny> = getattr_or(args, "vars", PyDict::new(py).into_any())?;
    let connection = &invocation.connection;
    let options: Bound<'py, PyAny> = build(
        py,
        "sqltide.cli.dev.models",
        "DevCommandOptions",
        vec![
            ("pipelines_root", require_pipelines_root(py, invocation)?),
            ("database", invocation.database.clone()),
            (
                "host",
                getattr_or(args, "ui_host", text(DEFAULT_DEV_SERVER_HOST))?
                    .str()?
                    .into_any(),
            ),
            (
                "port",
                py.get_type::<PyInt>().call1((getattr_or(
                    args,
                    "ui_port",
                    DEFAULT_DEV_SERVER_PORT.into_pyobject(py)?.into_any(),
                )?,))?,
            ),
            ("selected_target", selected_target),
            (
                "cli_variables",
                py.get_type::<PyTuple>()
                    .call1((variables.call_method0("items")?,))?,
            ),
            ("environment", connection.environment.clone()),
            ("connection_host", connection.value("host").clone()),
            ("connection_port", connection.value("port").clone()),
            ("connection_username", connection.value("username").clone()),
            ("connection_password", connection.value("password").clone()),
            ("auth_settings", auth_settings),
        ],
    )?;
    call_handler(
        py,
        handlers,
        "run_dev",
        vec![
            ("options", options),
            ("client", optional(client)),
            ("observation_client", optional(observation_client)),
            ("connection_factory", optional(connection_factory)),
            (
                "observation_connection_factory",
                optional(observation_connection_factory),
            ),
            ("loaded_project", loaded_project.clone()),
            ("adapter_profile", adapter_profile),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_compiler_adapter_profile, module)?)
}
