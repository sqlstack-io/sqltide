//! Resolve one invocation: project directory, pipelines root, project configuration, adapter,
//! connection options and the warehouse connections the command owns or borrows.

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyTuple};

use crate::cli::python::{getattr_none, getattr_or, raised, raised_from, user_error};
use crate::dev_server::support::attr;

const NATIVE: &str = "sqltide._native";
const DEV_CLI_VARIABLES_ENV_VAR: &str = "SQLTIDE_INTERNAL_CLI_VARS";
const PROJECT_FILE: &str = "sqltide_project.toml";
const LEGACY_PROJECT_FILE: &str = "streambuild_project.toml";
const PIPELINES_DIRECTORY: &str = "pipelines";
const COMMANDS_REQUIRING_PIPELINES_ROOT: [&str; 13] = [
    "dev",
    "discover",
    "compile",
    "lint",
    "format",
    "test",
    "plan",
    "build",
    "destroy",
    "reset-target",
    "deployment",
    "reconcile",
    "audit",
];
const COMMANDS_WITHOUT_ADAPTER_CONNECTION: [&str; 4] = ["discover", "compile", "lint", "format"];
const VIRTUAL_ENVIRONMENT_ONLY_COMMANDS: [&str; 5] =
    ["deployment", "reconcile", "janitor", "doctor", "repair"];
const OBSERVED_COMMANDS: [&str; 3] = ["build", "destroy", "reset-target"];
/// `--<name>` options that fall back to these environment variables, in resolution order.
const CONNECTION_OPTIONS: [(&str, &str); 4] = [
    ("host", "SQLTIDE_CLICKHOUSE_HOST"),
    ("port", "SQLTIDE_CLICKHOUSE_PORT"),
    ("username", "SQLTIDE_CLICKHOUSE_USERNAME"),
    ("password", "SQLTIDE_CLICKHOUSE_PASSWORD"),
];

/// The connection settings an invocation resolved from options, environment and project.
pub(crate) struct ConnectionOptions<'py> {
    /// `host`, `port`, `username` and `password`, each a value or `None`.
    pub(crate) values: [Bound<'py, PyAny>; 4],
    pub(crate) raw_project_connection: Bound<'py, PyAny>,
    pub(crate) variables: Bound<'py, PyAny>,
    pub(crate) environment: Bound<'py, PyAny>,
}

impl<'py> ConnectionOptions<'py> {
    pub(crate) fn value(&self, name: &str) -> &Bound<'py, PyAny> {
        let index: usize = CONNECTION_OPTIONS
            .iter()
            .position(|(option, _)| *option == name)
            .unwrap_or_default();
        &self.values[index]
    }
}

/// One resolved invocation.
pub(crate) struct Invocation<'py> {
    pub(crate) args: Bound<'py, PyAny>,
    pub(crate) command: String,
    pub(crate) project_dir: Option<Bound<'py, PyAny>>,
    pub(crate) pipelines_root: Option<Bound<'py, PyAny>>,
    /// `--database`, else the project's database: a `str` or `None`.
    pub(crate) database: Bound<'py, PyAny>,
    pub(crate) adapter: Bound<'py, PyAny>,
    /// The loaded project, or `None`.
    pub(crate) loaded_project: Bound<'py, PyAny>,
    pub(crate) connection: ConnectionOptions<'py>,
    /// `tuple(cli_variables.items())`.
    pub(crate) cli_variables: Bound<'py, PyAny>,
}

/// The primary connection, then the dev server's primary and observation connection factories.
pub(crate) type PrimaryConnection<'py> = (
    OwnedConnection<'py>,
    Option<Bound<'py, PyAny>>,
    Option<Bound<'py, PyAny>>,
);

/// A connection for the command and whether the command line closes it afterwards.
pub(crate) struct OwnedConnection<'py> {
    pub(crate) connection: Option<Bound<'py, PyAny>>,
    pub(crate) close_after_command: bool,
}

fn borrowed(connection: Option<Bound<'_, PyAny>>) -> OwnedConnection<'_> {
    OwnedConnection {
        connection,
        close_after_command: false,
    }
}

fn some<'py>(value: &Bound<'py, PyAny>) -> Option<Bound<'py, PyAny>> {
    (!value.is_none()).then(|| value.clone())
}

/// `environment.get(name)` or the default.
fn environment_get<'py>(
    environment: &Bound<'py, PyAny>,
    name: &str,
    default: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    match default {
        Some(default) => environment.call_method1("get", (name, default)),
        None => environment.call_method1("get", (name,)),
    }
}

fn join<'py>(path: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    path.call_method1("__truediv__", (name,))
}

fn exists(path: &Bound<'_, PyAny>) -> PyResult<bool> {
    path.call_method0("exists")?.is_truthy()
}

/// `working_directory / project_dir` unless it is absolute; `None` without one.
pub(crate) fn resolve_project_dir<'py>(
    project_dir: Option<&Bound<'py, PyAny>>,
    working_directory: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Some(project_dir) = project_dir else {
        return Ok(None);
    };
    if project_dir.call_method0("is_absolute")?.is_truthy()? {
        return Ok(Some(project_dir.clone()));
    }
    Ok(Some(
        working_directory.call_method1("__truediv__", (project_dir,))?,
    ))
}

/// The pipelines directory: `--project-dir`'s, or the nearest project's above the working
/// directory.
fn resolve_pipelines_root<'py>(
    py: Python<'py>,
    project_dir: Option<&Bound<'py, PyAny>>,
    working_directory: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let resolved: Option<Bound<'py, PyAny>> = resolve_project_dir(project_dir, working_directory)?;
    let mut current: Bound<'py, PyAny> = resolved
        .unwrap_or_else(|| working_directory.clone())
        .call_method0("resolve")?;
    if project_dir.is_some() {
        let direct: Bound<'py, PyAny> = join(&current, PIPELINES_DIRECTORY)?;
        if exists(&direct)? {
            return Ok(direct);
        }
        if exists(&current)? {
            return Ok(current);
        }
    }
    loop {
        let project_file: Bound<'py, PyAny> = join(&current, PROJECT_FILE)?;
        let legacy_project_file: Bound<'py, PyAny> = join(&current, LEGACY_PROJECT_FILE)?;
        let pipelines_root: Bound<'py, PyAny> = join(&current, PIPELINES_DIRECTORY)?;
        if exists(&project_file)? || exists(&legacy_project_file)? {
            if !exists(&pipelines_root)? {
                return Err(user_error(
                    py,
                    &format!(
                        "Found SQLTide project at '{}', but '{}' does not exist",
                        current.str()?,
                        pipelines_root.str()?
                    ),
                    None,
                ));
            }
            return Ok(pipelines_root);
        }
        if project_dir.is_some() {
            break;
        }
        let parent: Bound<'py, PyAny> = current.getattr("parent")?;
        if parent.eq(&current)? {
            break;
        }
        current = parent;
    }
    Err(user_error(
        py,
        "No SQLTide project found from the current directory.",
        Some("Run this command inside a project or pass --project-dir."),
    ))
}

/// The inherited `sqt dev` variables overlaid with `--vars`.
fn resolved_cli_variables<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let inherited_raw: Bound<'py, PyAny> =
        environment_get(environment, DEV_CLI_VARIABLES_ENV_VAR, None)?;
    let inherited: Bound<'py, PyAny> = if inherited_raw.is_none() {
        PyDict::new(py).into_any()
    } else {
        attr(py, "json", "loads")?.call1((inherited_raw,))?
    };
    let Ok(inherited) = inherited.downcast_into::<PyDict>() else {
        return Err(user_error(
            py,
            &format!("{DEV_CLI_VARIABLES_ENV_VAR} must contain a JSON object"),
            None,
        ));
    };
    let variables: Bound<'py, PyDict> = PyDict::new(py);
    for (key, value) in inherited.iter() {
        variables.set_item(key.str()?, value)?;
    }
    let overrides: Bound<'py, PyAny> = getattr_or(args, "vars", PyDict::new(py).into_any())?;
    variables.call_method1("update", (overrides,))?;
    Ok(variables)
}

/// `--<name>`, else a non-empty environment value (`int()` of it for the port), else `None`.
fn connection_value<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>,
    (name, variable): (&str, &str),
) -> PyResult<Bound<'py, PyAny>> {
    let value: Bound<'py, PyAny> = getattr_none(args, name)?;
    if !value.is_none() {
        return Ok(value);
    }
    let environment_value: Bound<'py, PyAny> = environment_get(environment, variable, None)?;
    if !environment_value.is_truthy()? {
        return Ok(py.None().into_bound(py));
    }
    if name == "port" {
        return py
            .get_type::<pyo3::types::PyInt>()
            .call1((environment_value,));
    }
    Ok(environment_value)
}

/// Resolve the project, adapter and connection options of a parsed command.
pub(crate) fn resolve_cli_invocation<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    environment: Option<&Bound<'py, PyAny>>,
    working_directory: Option<&Bound<'py, PyAny>>,
) -> PyResult<Invocation<'py>> {
    let environment: Bound<'py, PyAny> = match environment {
        Some(environment) if !environment.is_none() => environment.clone(),
        _ => attr(py, "os", "environ")?,
    };
    let cli_variables: Bound<'py, PyDict> = resolved_cli_variables(py, args, &environment)?;
    let working_directory: Bound<'py, PyAny> = match working_directory {
        Some(directory) if !directory.is_none() => directory.clone(),
        _ => attr(py, "pathlib", "Path")?.call_method0("cwd")?,
    };
    let command: String = args.getattr("command")?.str()?.to_string();
    let project_dir_arg: Option<Bound<'py, PyAny>> = some(&getattr_none(args, "project_dir")?);
    let project_dir: Option<Bound<'py, PyAny>> =
        resolve_project_dir(project_dir_arg.as_ref(), &working_directory)?;
    let pipelines_root: Option<Bound<'py, PyAny>> =
        if COMMANDS_REQUIRING_PIPELINES_ROOT.contains(&command.as_str()) {
            Some(resolve_pipelines_root(
                py,
                project_dir_arg.as_ref(),
                &working_directory,
            )?)
        } else {
            None
        };
    let path: &Bound<'py, PyAny> = pipelines_root
        .as_ref()
        .or(project_dir.as_ref())
        .unwrap_or(&working_directory);
    let load_kwargs: Bound<'py, PyDict> = PyDict::new(py);
    load_kwargs.set_item("path", path)?;
    load_kwargs.set_item("selected_target", getattr_none(args, "target")?)?;
    load_kwargs.set_item("cli_variables", &cli_variables)?;
    load_kwargs.set_item("environment", &environment)?;
    let loaded_project: Bound<'py, PyAny> =
        attr(py, NATIVE, "load_project_input_for_path")?.call((), Some(&load_kwargs))?;
    let effective: Bound<'py, PyAny> = if loaded_project.is_none() {
        py.None().into_bound(py)
    } else {
        loaded_project.getattr("effective_configuration")?
    };
    let none: Bound<'py, PyAny> = py.None().into_bound(py);
    let (default_database, adapter_name, raw_connection, variables) = if effective.is_none() {
        (
            none.clone(),
            attr(py, NATIVE, "DEFAULT_ADAPTER_NAME")?,
            none.clone(),
            PyTuple::empty(py).into_any(),
        )
    } else {
        (
            effective.getattr("database")?,
            effective.getattr("adapter")?,
            effective.getattr("connection")?,
            effective.getattr("variables")?,
        )
    };
    let adapter: Bound<'py, PyAny> =
        attr(py, "sqltide._native", "resolve_adapter")?.call1((adapter_name,))?;
    let database_arg: Bound<'py, PyAny> = getattr_none(args, "database")?;
    let database: Bound<'py, PyAny> = if database_arg.is_truthy()? {
        database_arg
    } else {
        default_database
    };
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(CONNECTION_OPTIONS.len());
    for option in CONNECTION_OPTIONS {
        values.push(connection_value(py, args, &environment, option)?);
    }
    let [host, port, username, password]: [Bound<'py, PyAny>; 4] = values
        .try_into()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("connection options"))?;
    Ok(Invocation {
        args: args.clone(),
        command,
        project_dir,
        pipelines_root,
        database,
        adapter,
        loaded_project,
        connection: ConnectionOptions {
            values: [host, port, username, password],
            raw_project_connection: raw_connection,
            variables,
            environment,
        },
        cli_variables: PyTuple::new(py, cli_variables.items())?.into_any(),
    })
}

/// Reject mode-specific commands before credentials or warehouse IO are resolved.
pub(crate) fn validate_cli_command_mode(
    py: Python<'_>,
    invocation: &Invocation<'_>,
) -> PyResult<()> {
    let virtual_environments: bool = virtual_environments_enabled(py, invocation)?;
    if VIRTUAL_ENVIRONMENT_ONLY_COMMANDS.contains(&invocation.command.as_str())
        && !virtual_environments
    {
        let display: String = match invocation.command.as_str() {
            "deployment" => format!(
                "deployment {}",
                invocation.args.getattr("deployment_command")?.str()?
            ),
            "repair" => "repair active-view".to_owned(),
            command => command.to_owned(),
        };
        return Err(user_error(
            py,
            &format!(
                "sqt {display} requires virtual environments to be enabled. Set \
                 defaults.pipeline_mode = 'virtual' in sqltide_project.toml or set mode = \
                 'virtual' in a pipeline.toml."
            ),
            None,
        ));
    }
    Ok(())
}

fn virtual_environments_enabled(py: Python<'_>, invocation: &Invocation<'_>) -> PyResult<bool> {
    let loaded_project: &Bound<'_, PyAny> = &invocation.loaded_project;
    if !loaded_project.is_none() {
        let effective: Bound<'_, PyAny> = loaded_project.getattr("effective_configuration")?;
        if !effective.is_none()
            && effective
                .getattr("defaults")?
                .getattr("pipeline_mode")?
                .eq(
                    attr(py, "sqltide.compiler.discovery.types", "PipelineMode")?
                        .getattr("VIRTUAL")?,
                )?
        {
            return Ok(true);
        }
    }
    let pipelines_root: Option<Bound<'_, PyAny>> =
        match (&invocation.pipelines_root, &invocation.project_dir) {
            (Some(root), _) => Some(root.clone()),
            (None, Some(project_dir)) => Some(join(project_dir, PIPELINES_DIRECTORY)?),
            (None, None) => None,
        };
    let Some(pipelines_root) = pipelines_root else {
        return Ok(false);
    };
    attr(py, NATIVE, "declares_virtual_pipeline")?
        .call1((pipelines_root,))?
        .is_truthy()
}

/// `AdapterConnectionConfig` from the options and project connection, without connecting.
pub(crate) fn resolve_invocation_connection_config<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let connection: &ConnectionOptions<'py> = &invocation.connection;
    if connection.raw_project_connection.is_none() {
        let kwargs: Bound<'py, PyDict> = PyDict::new(py);
        for (name, variable) in CONNECTION_OPTIONS {
            let value: &Bound<'py, PyAny> = connection.value(name);
            if value.is_none() {
                return Err(user_error(
                    py,
                    &format!("Missing {name}. Pass --{name} or set {variable}."),
                    None,
                ));
            }
            kwargs.set_item(name, value)?;
        }
        kwargs.set_item("settings", PyTuple::empty(py))?;
        return attr(py, "sqltide.adapter.models", "AdapterConnectionConfig")?
            .call((), Some(&kwargs));
    }
    let raw_values: Bound<'py, PyDict> = PyDict::new(py);
    raw_values.call_method1(
        "update",
        (connection.raw_project_connection.getattr("values")?,),
    )?;
    for (name, _) in CONNECTION_OPTIONS {
        let value: &Bound<'py, PyAny> = connection.value(name);
        if !value.is_none() {
            raw_values.set_item(name, value)?;
        }
    }
    let environment: Bound<'py, PyAny> = if connection.environment.is_truthy()? {
        connection.environment.clone()
    } else {
        PyDict::new(py).into_any()
    };
    let variables: Bound<'py, PyDict> = PyDict::new(py);
    variables.call_method1("update", (&connection.variables,))?;
    let interpolate: Bound<'py, PyAny> = attr(py, NATIVE, "interpolate_config_value")?;
    let expanded: Bound<'py, PyDict> = PyDict::new(py);
    for (name, raw_value) in raw_values.iter() {
        let kwargs: Bound<'py, PyDict> = PyDict::new(py);
        kwargs.set_item("value", raw_value)?;
        kwargs.set_item("variables", &variables)?;
        kwargs.set_item("environment", &environment)?;
        kwargs.set_item("field_path", format!("connection.{}", name.str()?))?;
        expanded.set_item(&name, interpolate.call((), Some(&kwargs))?)?;
    }
    let kwargs: Bound<'py, PyDict> = PyDict::new(py);
    kwargs.set_item("values", &expanded)?;
    kwargs.set_item(
        "database",
        if invocation.command == "build" {
            py.None().into_bound(py)
        } else {
            invocation.database.clone()
        },
    )?;
    match invocation
        .adapter
        .call_method("build_connection_config", (), Some(&kwargs))
    {
        Ok(config) => Ok(config),
        Err(error) => {
            if !raised(
                py,
                &error,
                "sqltide.adapter.exceptions",
                "AdapterConfigurationError",
            )? {
                return Err(error);
            }
            let loaded_project: &Bound<'py, PyAny> = &invocation.loaded_project;
            if loaded_project.is_none() || loaded_project.getattr("configuration")?.is_none() {
                return Err(error);
            }
            let configuration: Bound<'py, PyAny> = loaded_project.getattr("configuration")?;
            let mut sources: Vec<String> = vec![
                configuration
                    .getattr("project_source")?
                    .getattr("file_path")?
                    .str()?
                    .to_string(),
            ];
            let local_source: Bound<'py, PyAny> = configuration.getattr("local_source")?;
            if !local_source.is_none() {
                sources.push(local_source.getattr("file_path")?.str()?.to_string());
            }
            let message: String = format!(
                "Effective connection from {} is invalid: {}",
                sources.join(", "),
                error.value(py).str()?
            );
            let replacement: PyErr = PyErr::from_value(
                attr(
                    py,
                    "sqltide.adapter.exceptions",
                    "AdapterConfigurationError",
                )?
                .call1((message,))?,
            );
            Err(raised_from(py, replacement, error))
        }
    }
}

/// Open a connection and rename pre-rename metadata in each metadata database.
fn connect<'py>(
    py: Python<'py>,
    adapter: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    databases: &[String],
) -> PyResult<Bound<'py, PyAny>> {
    let connection: Bound<'py, PyAny> = adapter.call_method1("connect", (config,))?;
    for database in databases {
        if let Err(error) = connection.call_method1("rename_streambuild_metadata", (database,)) {
            return Err(match connection.call_method0("close") {
                Ok(_) => error,
                Err(close_error) => crate::cli::python::raised_during(py, close_error, error),
            });
        }
    }
    Ok(connection)
}

/// The invocation database and `--metadata-database`, deduplicated and sorted.
fn metadata_databases(invocation: &Invocation<'_>) -> PyResult<Vec<String>> {
    let mut databases: Vec<String> = Vec::new();
    for value in [
        invocation.database.clone(),
        getattr_none(&invocation.args, "metadata_database")?,
    ] {
        if value.is_none() {
            continue;
        }
        let name: String = value.extract()?;
        if !databases.contains(&name) {
            databases.push(name);
        }
    }
    databases.sort();
    Ok(databases)
}

/// A borrowed or command-line-owned connection for one invocation.
fn resolve_invocation_connection<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    provided: Option<&Bound<'py, PyAny>>,
) -> PyResult<OwnedConnection<'py>> {
    if COMMANDS_WITHOUT_ADAPTER_CONNECTION.contains(&invocation.command.as_str()) {
        return Ok(borrowed(None));
    }
    if let Some(provided) = provided.filter(|connection| !connection.is_none()) {
        return Ok(borrowed(Some(provided.clone())));
    }
    let config: Bound<'py, PyAny> = resolve_invocation_connection_config(py, invocation)?;
    Ok(OwnedConnection {
        connection: Some(connect(
            py,
            &invocation.adapter,
            &config,
            &metadata_databases(invocation)?,
        )?),
        close_after_command: true,
    })
}

/// The primary connection; `sqt dev` without a supplied connection gets a connection factory
/// for its primary and observation connections instead.
pub(crate) fn resolve_primary_connection<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    provided: Option<&Bound<'py, PyAny>>,
) -> PyResult<PrimaryConnection<'py>> {
    let provided: Option<&Bound<'py, PyAny>> = provided.filter(|value| !value.is_none());
    if invocation.command != "dev" || provided.is_some() {
        return Ok((
            resolve_invocation_connection(py, invocation, provided)?,
            None,
            None,
        ));
    }
    let config: Py<PyAny> = resolve_invocation_connection_config(py, invocation)?.unbind();
    let adapter: Py<PyAny> = invocation.adapter.clone().unbind();
    let databases: Vec<String> = metadata_databases(invocation)?;
    let factory: Bound<'py, PyAny> = PyCFunction::new_closure(
        py,
        Some(c"connect"),
        Some(c"Open one warehouse connection for the dev server."),
        move |arguments: &Bound<'_, PyTuple>,
              _: Option<&Bound<'_, PyDict>>|
              -> PyResult<Py<PyAny>> {
            let py: Python<'_> = arguments.py();
            Ok(connect(py, adapter.bind(py), config.bind(py), &databases)?.unbind())
        },
    )?
    .into_any();
    Ok((borrowed(None), Some(factory.clone()), Some(factory)))
}

/// The separate observation connection that observed mutations require.
pub(crate) fn resolve_observation_connection<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    provided_primary: Option<&Bound<'py, PyAny>>,
    provided_observation: Option<&Bound<'py, PyAny>>,
) -> PyResult<Option<OwnedConnection<'py>>> {
    let observed: bool = OBSERVED_COMMANDS.contains(&invocation.command.as_str());
    let primary: Option<&Bound<'py, PyAny>> = provided_primary.filter(|value| !value.is_none());
    let observation: Option<Bound<'py, PyAny>> = provided_observation
        .filter(|value| !value.is_none())
        .cloned();
    if primary.is_none() && observed {
        return resolve_invocation_connection(py, invocation, None).map(Some);
    }
    if primary.is_some() && observed {
        if observation.is_none() {
            let display: &str = if invocation.command == "build" {
                "Build"
            } else {
                "Destruction"
            };
            return Err(user_error(
                py,
                &format!("{display} requires a dedicated observation connection"),
                None,
            ));
        }
        return Ok(Some(borrowed(observation)));
    }
    if primary.is_some() && invocation.command == "dev" {
        return Ok(Some(borrowed(observation)));
    }
    Ok(None)
}

/// `override`, else the one `default_database` every discovered pipeline's project declares.
pub(crate) fn resolve_default_database<'py>(
    py: Python<'py>,
    loaded_pipelines: &Bound<'py, PyAny>,
    override_: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !override_.is_none() {
        return Ok(override_);
    }
    let mut declared: Option<Bound<'py, PyAny>> = None;
    let mut disagree: bool = false;
    for pipeline in loaded_pipelines.try_iter()? {
        let project: Bound<'py, PyAny> = pipeline?.getattr("project")?;
        if project.is_none() {
            continue;
        }
        let database: Bound<'py, PyAny> = project.getattr("default_database")?;
        if database.is_none() {
            continue;
        }
        match &declared {
            None => declared = Some(database),
            Some(first) => disagree |= !first.eq(&database)?,
        }
    }
    if disagree {
        return Err(user_error(
            py,
            "Discovered pipelines disagree on project default_database",
            None,
        ));
    }
    declared.ok_or_else(|| {
        user_error(
            py,
            "No database was provided. Pass --database or define database on the selected \
             target in sqltide_project.toml",
            None,
        )
    })
}

/// The database a command targets: `--database`, else the project's declared default.
#[pyfunction]
#[pyo3(name = "resolve_default_database", signature = (*, loaded_pipelines, r#override))]
fn resolve_default_database_py<'py>(
    py: Python<'py>,
    loaded_pipelines: &Bound<'py, PyAny>,
    r#override: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    resolve_default_database(py, loaded_pipelines, r#override)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(resolve_default_database_py, module)?)
}
