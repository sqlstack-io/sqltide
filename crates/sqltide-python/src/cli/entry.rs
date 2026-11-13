//! `sqt`: parse the command line, resolve the project and connections, dispatch one command and
//! report expected failures on stderr with exit status 1.

use pyo3::exceptions::{PyKeyboardInterrupt, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyString, PyTuple};
use pyo3::wrap_pyfunction;

use crate::cli::admin::run_admin_command;
use crate::cli::argv;
use crate::cli::dispatch::dispatch_cli_command;
use crate::cli::invocation::{
    Invocation, OwnedConnection, resolve_cli_invocation, resolve_observation_connection,
    resolve_primary_connection, validate_cli_command_mode,
};
use crate::cli::playground::{is_query, playground_project, run_playground};
use crate::cli::python::{getattr_none, print_to, raised, raised_during, user_error};
use crate::cli::render::{
    attach_error_diagnostic, render_cli_user_error, render_error, render_expected_warehouse_error,
};
use crate::dev_server::support::attr;

const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";
const UNKNOWN_DATABASE: &str = "<unknown>";
const RUNTIME_ERROR_CODE: &str = "SQT-RUNTIME-001";
const INTERACTIVE_COMMANDS: [&str; 2] = ["destroy", "reset-target"];
/// Commands named differently in operator messages than on the command line.
const DISPLAY_NAME_BY_COMMAND: [(&str, &str); 4] = [
    ("audit", "audit"),
    ("destroy", "destroy"),
    ("repair", "repair active-view"),
    ("reset-target", "reset-target"),
];

/// Run `sqt` with `argv` (`sys.argv` when `None`, its first item the program); the exit status.
/// SIGTERM cancels like Ctrl-C while the command runs. The keyword collaborators replace the
/// default command handlers, environment, working directory and warehouse connections.
#[pyfunction]
#[pyo3(signature = (
    argv=None,
    *,
    handlers=None,
    environment=None,
    working_directory=None,
    adapter_connection=None,
    observation_adapter_connection=None,
))]
#[allow(clippy::too_many_arguments)]
fn run_cli<'py>(
    py: Python<'py>,
    argv: Option<Bound<'py, PyAny>>,
    handlers: Option<Bound<'py, PyAny>>,
    environment: Option<Bound<'py, PyAny>>,
    working_directory: Option<Bound<'py, PyAny>>,
    adapter_connection: Option<Bound<'py, PyAny>>,
    observation_adapter_connection: Option<Bound<'py, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let signal: Bound<'py, PyModule> = py.import("signal")?;
    let sigterm: Bound<'py, PyAny> = signal.getattr("SIGTERM")?;
    let previous: Bound<'py, PyAny> = signal.call_method1("getsignal", (&sigterm,))?;
    let interrupt: Bound<'py, PyCFunction> = PyCFunction::new_closure(
        py,
        Some(c"raise_keyboard_interrupt_from_signal"),
        Some(c"Map a process-manager termination signal onto the CLI cancellation path."),
        |_: &Bound<'_, PyTuple>, _: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
            Err(PyKeyboardInterrupt::new_err(()))
        },
    )?;
    signal.call_method1("signal", (&sigterm, interrupt))?;
    let outcome: PyResult<Py<PyAny>> = main_with_dependencies(
        py,
        argv,
        &Collaborators {
            handlers,
            environment,
            working_directory,
            adapter_connection,
            observation_adapter_connection,
        },
    );
    match signal.call_method1("signal", (&sigterm, previous)) {
        Ok(_) => outcome,
        Err(error) => Err(match outcome {
            Err(original) => raised_during(py, error, original),
            Ok(_) => error,
        }),
    }
}

/// What a caller supplies in place of the defaults.
pub(crate) struct Collaborators<'py> {
    pub(crate) handlers: Option<Bound<'py, PyAny>>,
    pub(crate) environment: Option<Bound<'py, PyAny>>,
    pub(crate) working_directory: Option<Bound<'py, PyAny>>,
    pub(crate) adapter_connection: Option<Bound<'py, PyAny>>,
    pub(crate) observation_adapter_connection: Option<Bound<'py, PyAny>>,
}

/// `argv[1:]` as parser input; `None` keeps `sys.argv`.
fn parser_arguments<'py>(
    argv: Option<Bound<'py, PyAny>>,
) -> PyResult<Option<Vec<Bound<'py, PyString>>>> {
    let Some(argv) = argv else {
        return Ok(None);
    };
    let tail: Bound<'py, PyAny> =
        argv.get_item(pyo3::types::PySlice::new(argv.py(), 1, isize::MAX, 1))?;
    let mut strings: Vec<Bound<'py, PyString>> = Vec::new();
    for item in tail.try_iter()? {
        strings.push(item?.downcast_into::<PyString>()?);
    }
    Ok(Some(strings))
}

fn main_with_dependencies<'py>(
    py: Python<'py>,
    argv: Option<Bound<'py, PyAny>>,
    collaborators: &Collaborators<'py>,
) -> PyResult<Py<PyAny>> {
    let args: Bound<'py, PyAny> = argv::parse(py, parser_arguments(argv)?)?;
    let mut resolved_database: Option<Bound<'py, PyAny>> = None;
    match run_command(py, &args, collaborators, &mut resolved_database) {
        Ok(code) => Ok(code.unbind()),
        Err(error) => Ok(report(py, error, &args, resolved_database)?
            .into_pyobject(py)?
            .into_any()
            .unbind()),
    }
}

/// The body of the entry point's `try`: everything after parsing.
fn run_command<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    collaborators: &Collaborators<'py>,
    resolved_database: &mut Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    require_interactive_destruction(py, args)?;
    let command: String = args.getattr("command")?.str()?.to_string();
    let working_directory = || -> PyResult<Bound<'py, PyAny>> {
        match &collaborators.working_directory {
            Some(directory) if !directory.is_none() => Ok(directory.clone()),
            _ => attr(py, "pathlib", "Path")?.call_method0("cwd"),
        }
    };
    if command == "playground" {
        if !is_query(args)? {
            let code: i64 = run_playground(py, args, &working_directory()?)?;
            return Ok(code.into_pyobject(py)?.into_any());
        }
        // `query` runs every statement in a file, so it only reaches a playground's own services.
        // It then reads the project's connection like other warehouse commands.
        playground_project(py, args, &working_directory()?)?;
    }
    if command == "admin" {
        let environment: Bound<'py, PyAny> = match &collaborators.environment {
            Some(environment) if !environment.is_none() => environment.clone(),
            _ => attr(py, "os", "environ")?,
        };
        let code: i64 = run_admin_command(py, args, &environment, &working_directory()?)?;
        return Ok(code.into_pyobject(py)?.into_any());
    }
    let invocation: Invocation<'py> = resolve_cli_invocation(
        py,
        args,
        collaborators.environment.as_ref(),
        collaborators.working_directory.as_ref(),
    )?;
    validate_cli_command_mode(py, &invocation)?;
    *resolved_database = Some(invocation.database.clone());
    let (primary, connection_factory, observation_factory) =
        resolve_primary_connection(py, &invocation, collaborators.adapter_connection.as_ref())?;
    let observation: PyResult<Option<OwnedConnection<'py>>> = resolve_observation_connection(
        py,
        &invocation,
        collaborators.adapter_connection.as_ref(),
        collaborators.observation_adapter_connection.as_ref(),
    );
    let outcome: PyResult<Bound<'py, PyAny>> = match &observation {
        Ok(observation) => dispatch_cli_command(
            py,
            &invocation,
            collaborators.handlers.as_ref(),
            primary.connection.as_ref(),
            observation
                .as_ref()
                .and_then(|connection| connection.connection.as_ref()),
            connection_factory.as_ref(),
            observation_factory.as_ref(),
        ),
        Err(error) => Err(error.clone_ref(py)),
    };
    let mut outcome: PyResult<Bound<'py, PyAny>> = outcome;
    if let Ok(Some(observation)) = &observation {
        outcome = close_after(py, observation, outcome);
    }
    close_after(py, &primary, outcome)
}

/// The `finally` clause that closes a connection the command line opened.
fn close_after<'py>(
    py: Python<'py>,
    connection: &OwnedConnection<'py>,
    outcome: PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let Some(opened) = connection
        .connection
        .as_ref()
        .filter(|_| connection.close_after_command)
    else {
        return outcome;
    };
    match opened.call_method0("close") {
        Ok(_) => outcome,
        Err(error) => Err(match outcome {
            Err(original) => raised_during(py, error, original),
            Ok(_) => error,
        }),
    }
}

/// Reject destructive commands before project or connection resolution.
fn require_interactive_destruction(py: Python<'_>, args: &Bound<'_, PyAny>) -> PyResult<()> {
    let command: Bound<'_, PyAny> = args.getattr("command")?;
    let destructive: bool = INTERACTIVE_COMMANDS
        .iter()
        .any(|name| command.eq(*name).unwrap_or(false));
    if destructive
        && !py
            .import("sys")?
            .getattr("stdin")?
            .call_method0("isatty")?
            .is_truthy()?
    {
        return Err(user_error(
            py,
            &format!(
                "sqt {} requires an interactive terminal; stdin is not a TTY",
                command.str()?
            ),
            None,
        ));
    }
    Ok(())
}

/// The `except` clauses of the entry point, in order.
fn report<'py>(
    py: Python<'py>,
    error: PyErr,
    args: &Bound<'py, PyAny>,
    resolved_database: Option<Bound<'py, PyAny>>,
) -> PyResult<i64> {
    if raised(py, &error, CLI_EXCEPTIONS, "CliUserError")? {
        print_to(py, "stderr", &render_cli_user_error(py, &error)?)?;
        return Ok(1);
    }
    if raised(
        py,
        &error,
        "sqltide.executor.destruction.exceptions",
        "DestructionError",
    )? || raised(
        py,
        &error,
        "sqltide.compiler.compile.exceptions",
        "TransformSqlContractError",
    )? || error.is_instance_of::<PyValueError>(py)
    {
        print_to(py, "stderr", &render_error(py, error.value(py).as_any())?)?;
        return Ok(1);
    }
    if raised(py, &error, ADAPTER_EXCEPTIONS, "AdapterWarehouseError")? {
        let database: String = match resolved_database {
            Some(database) if database.is_truthy()? => database.str()?.to_string(),
            _ => UNKNOWN_DATABASE.to_owned(),
        };
        let rendered: Option<String> = render_expected_warehouse_error(
            py,
            &command_name(args)?,
            &database,
            error.value(py).as_any(),
        )?;
        let Some(rendered) = rendered else {
            return Err(error);
        };
        print_to(py, "stderr", &rendered)?;
        return Ok(1);
    }
    if raised(py, &error, ADAPTER_EXCEPTIONS, "AdapterError")? {
        attach_error_diagnostic(
            py,
            error.value(py).as_any(),
            pyo3::types::PyString::new(py, "runtime").into_any(),
            RUNTIME_ERROR_CODE,
            py.None().into_bound(py),
            py.None().into_bound(py),
        )?;
        print_to(py, "stderr", &render_error(py, error.value(py).as_any())?)?;
        return Ok(1);
    }
    Err(error)
}

/// The operator-facing command name for error messages.
fn command_name(args: &Bound<'_, PyAny>) -> PyResult<String> {
    let command: String = args.getattr("command")?.str()?.to_string();
    if command == "deployment" {
        return Ok(format!(
            "deployment {}",
            getattr_none(args, "deployment_command")?.str()?
        ));
    }
    Ok(DISPLAY_NAME_BY_COMMAND
        .iter()
        .find(|(name, _)| *name == command)
        .map_or(command.clone(), |(_, display)| (*display).to_owned()))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_cli, module)?)
}
