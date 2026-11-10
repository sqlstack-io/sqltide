//! The dev server's single-flight `sqt build` child: launch, live event feed, graceful
//! cancellation with warehouse query termination, and force kill. The server is a pure spectator
//! of the child: it reads the child's event stream and never fabricates terminal facts.

use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};

use super::audits::{fstring, literal};
use super::routes::DEV_EXCEPTIONS;
use super::support::{attr, is_instance};
use crate::handling::{raise_from, while_handling, within};
use crate::py_kwargs;

const RUN_STARTED_KIND: &str = "run_started";
const STATEMENT_STARTED_KIND: &str = "statement_started";
const STATEMENT_COMPLETED_KIND: &str = "statement_completed";
const STDERR_TAIL_LINES: usize = 50;
const DEV_CONSTANTS: &str = "sqltide.dev_server.constants";
const BUILD_CONSTANTS: &str = "sqltide.cli.build.constants";
const OBSERVABILITY_CONSTANTS: &str = "sqltide.executor.observability.constants";
const SCOPE_ENV_VARS: [&str; 3] = [
    "EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR",
    "EXPECTED_BUILD_READ_SCOPE_ENV_VAR",
    "EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR",
];
const CONNECTION_OVERRIDES: [(&str, &str); 4] = [
    ("SQLTIDE_CLICKHOUSE_HOST", "connection_host"),
    ("SQLTIDE_CLICKHOUSE_PORT", "connection_port"),
    ("SQLTIDE_CLICKHOUSE_USERNAME", "connection_username"),
    ("SQLTIDE_CLICKHOUSE_PASSWORD", "connection_password"),
];

type Any<'py> = Bound<'py, PyAny>;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The held object, or Python `None`.
fn held<'py>(py: Python<'py>, value: &Option<Py<PyAny>>) -> Any<'py> {
    match value {
        Some(value) => value.bind(py).clone(),
        None => py.None().into_bound(py),
    }
}

fn some(value: Any<'_>) -> Option<Py<PyAny>> {
    if value.is_none() { None } else { Some(value.unbind()) }
}

fn build_error(py: Python<'_>, class: &str, message: &Any<'_>) -> PyResult<PyErr> {
    Ok(PyErr::from_value(attr(py, DEV_EXCEPTIONS, class)?.call1((message,))?))
}

fn not_owned(py: Python<'_>) -> PyResult<PyErr> {
    build_error(
        py,
        "BuildInProgressError",
        &literal(py, "this server does not own that running process; it cannot deliver a cancellation signal"),
    )
}

/// Whether `process` is set and `process.poll()` is `None`.
fn running(py: Python<'_>, process: &Option<Py<PyAny>>) -> PyResult<bool> {
    match process {
        Some(process) => Ok(process.bind(py).call_method0("poll")?.is_none()),
        None => Ok(false),
    }
}

fn monotonic(py: Python<'_>) -> PyResult<f64> {
    attr(py, "time", "monotonic")?.call0()?.extract()
}

/// Python's `str.splitlines()` line boundaries.
fn is_line_break(character: char) -> bool {
    matches!(character, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// `str.splitlines()`: lines without their breaks, `\r\n` counting as one break.
fn split_lines(text: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = Vec::new();
    let mut start: usize = 0;
    let mut characters = text.char_indices().peekable();
    while let Some((index, character)) = characters.next() {
        if !is_line_break(character) {
            continue;
        }
        lines.push(&text[start..index]);
        let mut end: usize = index + character.len_utf8();
        if character == '\r'
            && let Some(&(_, '\n')) = characters.peek()
        {
            characters.next();
            end += 1;
        }
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// The last lines of the child's stderr capture, decoded with replacement characters.
fn stderr_tail(path: &Option<PathBuf>) -> PyResult<String> {
    let Some(path) = path else {
        return Ok(String::new());
    };
    let bytes: Vec<u8> = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(String::new()),
        Err(error) => return Err(error.into()),
    };
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = split_lines(&text);
    Ok(lines[lines.len().saturating_sub(STDERR_TAIL_LINES)..].join("\n"))
}

fn remove_file(path: Option<PathBuf>) -> PyResult<()> {
    match path.map(std::fs::remove_file) {
        Some(Err(error)) if error.kind() != std::io::ErrorKind::NotFound => Err(error.into()),
        _ => Ok(()),
    }
}

/// `shlex.quote` over the `surrogatepass` UTF-8 bytes of one argument.
fn shell_quoted(argument: &[u8]) -> Vec<u8> {
    if argument.is_empty() {
        return b"''".to_vec();
    }
    let safe = |byte: &u8| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(byte);
    if argument.iter().all(safe) {
        return argument.to_vec();
    }
    let mut quoted: Vec<u8> = vec![b'\''];
    for &byte in argument {
        if byte == b'\'' {
            quoted.extend_from_slice(b"'\"'\"'");
        } else {
            quoted.push(byte);
        }
    }
    quoted.push(b'\'');
    quoted
}

/// `shlex.join(arguments)`, keeping lone surrogates as Python does.
fn shell_joined<'py>(py: Python<'py>, arguments: &[Any<'py>]) -> PyResult<Any<'py>> {
    let mut joined: Vec<u8> = Vec::new();
    for (index, argument) in arguments.iter().enumerate() {
        if index > 0 {
            joined.push(b' ');
        }
        let encoded: Any<'py> = argument.downcast::<PyString>()?.call_method1("encode", ("utf-8", "surrogatepass"))?;
        joined.extend(shell_quoted(encoded.downcast::<PyBytes>()?.as_bytes()));
    }
    PyBytes::new(py, &joined).call_method1("decode", ("utf-8", "surrogatepass"))
}

/// Build the executable argv and the safe user-facing command from one source.
#[pyfunction]
#[pyo3(signature = (
    *,
    selectors,
    start_time,
    execution_context,
    changed = false,
    include_missing_upstream = false,
    deployment_id = None,
    confirmations = None,
))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_invocation<'py>(
    py: Python<'py>,
    selectors: &Any<'py>,
    start_time: &Any<'py>,
    execution_context: &Any<'py>,
    changed: bool,
    include_missing_upstream: bool,
    deployment_id: Option<Any<'py>>,
    confirmations: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let executable: Any<'py> = attr(py, "sys", "executable")?;
    let sqt_path: Any<'py> = attr(py, "pathlib", "Path")?.call1((executable,))?.getattr("parent")?.div("sqt")?;
    let mut argv: Vec<Any<'py>> = vec![sqt_path.str()?.into_any(), literal(py, "build")];
    let mut display: Vec<Any<'py>> = vec![literal(py, "sqt"), literal(py, "build")];
    if !execution_context.is_none() {
        for (flag, field) in [("--target", "selected_target"), ("--database", "database")] {
            let value: Any<'py> = execution_context.getattr(field)?;
            if !value.is_none() {
                argv.extend([literal(py, flag), value]);
            }
        }
    }
    let mut both = |items: Vec<Any<'py>>| {
        display.extend(items.iter().cloned());
        argv.extend(items);
    };
    if selectors.is_truthy()? {
        let mut items: Vec<Any<'py>> = vec![literal(py, "--select")];
        for selector in selectors.try_iter()? {
            items.push(selector?);
        }
        both(items);
    }
    if changed {
        both(vec![literal(py, "--changed")]);
    }
    if include_missing_upstream {
        both(vec![literal(py, "--include-missing-upstream")]);
    }
    if !start_time.is_none() {
        both(vec![literal(py, "--start-time"), start_time.clone()]);
    }
    if let Some(deployment_id) = deployment_id.filter(|value| !value.is_none()) {
        both(vec![literal(py, "--deployment-id"), deployment_id]);
    }
    if let Some(confirmations) = confirmations {
        for confirmation in confirmations.try_iter()? {
            both(vec![literal(py, "--confirm"), confirmation?]);
        }
    }
    let command: Any<'py> = shell_joined(py, &display)?;
    argv.extend([literal(py, "--auto-approve"), literal(py, "--events")]);
    PyTuple::new(py, [PyList::new(py, argv)?.into_any(), command])
}

/// `json.dumps(sorted(scope))`.
fn scope_json<'py>(scope: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = scope.py();
    attr(py, "json", "dumps")?.call1((attr(py, "builtins", "sorted")?.call1((scope,))?,))
}

/// The child's environment: the launch identity, the authorized scopes, the dev session's
/// variables and its connection overrides.
#[pyfunction]
#[pyo3(signature = (
    *,
    execution_context,
    display_command = None,
    invocation_id = None,
    expected_write_scope = None,
    expected_read_scope = None,
    expected_pipeline_scope = None,
))]
pub(crate) fn build_child_environment<'py>(
    py: Python<'py>,
    execution_context: &Any<'py>,
    display_command: Option<Any<'py>>,
    invocation_id: Option<Any<'py>>,
    expected_write_scope: Option<Any<'py>>,
    expected_read_scope: Option<Any<'py>>,
    expected_pipeline_scope: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyDict>> {
    let context_environment: Option<Any<'py>> = if execution_context.is_none() {
        None
    } else {
        Some(execution_context.getattr("environment")?).filter(|environment| !environment.is_none())
    };
    let source: Any<'py> = match context_environment {
        Some(environment) => environment,
        None => attr(py, "os", "environ")?,
    };
    let environment: Bound<'py, PyDict> = attr(py, "builtins", "dict")?.call1((source,))?.downcast_into()?;
    if let Some(command) = display_command {
        environment.set_item(attr(py, OBSERVABILITY_CONSTANTS, "RUN_DISPLAY_COMMAND_ENV_VAR")?, command)?;
    }
    if let Some(invocation_id) = invocation_id {
        environment.set_item(attr(py, OBSERVABILITY_CONSTANTS, "RUN_INVOCATION_ID_ENV_VAR")?, invocation_id)?;
    }
    let scope_names: Vec<Any<'py>> =
        SCOPE_ENV_VARS.iter().map(|name| attr(py, BUILD_CONSTANTS, name)).collect::<PyResult<_>>()?;
    for name in &scope_names {
        environment.call_method1("pop", (name, py.None()))?;
    }
    for (name, scope) in scope_names.iter().zip([expected_write_scope, expected_read_scope, expected_pipeline_scope]) {
        if let Some(scope) = scope {
            environment.set_item(name, scope_json(&scope)?)?;
        }
    }
    if execution_context.is_none() {
        return Ok(environment);
    }
    let cli_variables: Any<'py> = execution_context.getattr("cli_variables")?;
    if cli_variables.is_truthy()? {
        let variables: Any<'py> = attr(py, "builtins", "dict")?.call1((cli_variables,))?;
        let separators: Bound<'py, PyTuple> = PyTuple::new(py, [",", ":"])?;
        environment.set_item(
            attr(py, "sqltide.cli.entry.constants", "DEV_CLI_VARIABLES_ENV_VAR")?,
            attr(py, "json", "dumps")?.call((variables,), Some(&py_kwargs!(py; sort_keys = true, separators = separators)?))?,
        )?;
    }
    for (name, field) in CONNECTION_OVERRIDES {
        let value: Any<'py> = execution_context.getattr(field)?;
        if !value.is_none() {
            environment.set_item(name, value.str()?)?;
        }
    }
    Ok(environment)
}

#[derive(Default)]
struct State {
    process: Option<Py<PyAny>>,
    statement_count: u64,
    stderr_path: Option<PathBuf>,
    invocation_id: Option<Py<PyAny>>,
    exit_code: Option<Py<PyAny>>,
    command: Option<Py<PyAny>>,
    started_monotonic: f64,
    current_invocation_id: Option<Py<PyAny>>,
    cancelling_invocation_id: Option<Py<PyAny>>,
    force_available: bool,
    active_query_id: Option<Py<PyAny>>,
    cancellation_query_id: Option<Py<PyAny>>,
    cancellation_status: Option<Py<PyAny>>,
    cancellation_error: Option<Py<PyAny>>,
    cancellation_thread: Option<Py<PyAny>>,
}

/// Owns at most one running `sqt build` child and its live event feed. Every state change
/// happens under one Python lock, so the stdout reader, the cancellation worker and request
/// handlers observe one consistent run.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct BuildProcessManager {
    reporter: Py<PyAny>,
    execution_context: Option<Py<PyAny>>,
    query_canceller: Option<Py<PyAny>>,
    cancellation_reporter: Option<Py<PyAny>>,
    lock: Py<PyAny>,
    state: Mutex<State>,
}

/// The value of `py_value` cloned out of the state without holding the guard across Python.
macro_rules! read {
    ($self:ident, $py:ident, $field:ident) => {
        held($py, &guard(&$self.state).$field)
    };
}

impl BuildProcessManager {
    fn locked<'py, T>(&self, py: Python<'py>, body: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
        within(self.lock.bind(py), body)
    }

    /// Whether `process` is still the current child launched as `invocation_id`.
    fn current(&self, py: Python<'_>, process: &Any<'_>, invocation_id: &Any<'_>) -> PyResult<bool> {
        let (same_process, owned_id) = {
            let state = guard(&self.state);
            (state.process.as_ref().is_some_and(|current| current.bind(py).is(process)), held(py, &state.invocation_id))
        };
        Ok(same_process && owned_id.eq(invocation_id)?)
    }

    fn owned_process_locked<'py>(&self, py: Python<'py>, invocation_id: &Any<'py>) -> PyResult<Any<'py>> {
        let (owned_id, process) = {
            let state = guard(&self.state);
            (held(py, &state.invocation_id), state.process.as_ref().map(|process| process.clone_ref(py)))
        };
        if owned_id.ne(invocation_id)? || process.is_none() || !running(py, &process)? {
            return Err(not_owned(py)?);
        }
        Ok(process.expect("checked above").into_bound(py))
    }

    fn cancellation_payload_locked<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let state = guard(&self.state);
        let status: Any<'py> = held(py, &state.cancellation_status);
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("status", if status.is_truthy()? { status } else { literal(py, "cancelling") })?;
        payload.set_item("forceAvailable", state.force_available)?;
        payload.set_item("queryId", held(py, &state.cancellation_query_id))?;
        payload.set_item("errorMessage", held(py, &state.cancellation_error))?;
        if let Some(exit_code) = &state.exit_code {
            payload.set_item("exitCode", exit_code.bind(py))?;
        }
        Ok(payload)
    }

    fn report_finished(&self, py: Python<'_>, exit_code: &Any<'_>, elapsed_seconds: f64, statement_count: u64) -> PyResult<()> {
        let succeeded: bool = exit_code.eq(0)?;
        let elapsed: Any<'_> = attr(py, "builtins", "format")?.call1((elapsed_seconds, ".1f"))?;
        let detail: Any<'_> = if succeeded {
            fstring(py, &[statement_count.into_pyobject(py)?.into_any(), literal(py, " statements in "), elapsed, literal(py, "s")])?
        } else {
            fstring(py, &[literal(py, "exit "), exit_code.clone(), literal(py, " after "), elapsed, literal(py, "s")])?
        };
        let tone: Any<'_> = attr(py, "sqltide.dev_server.types", "ActivityTone")?.getattr(if succeeded { "GOOD" } else { "BAD" })?;
        self.reporter.bind(py).call_method(
            "report_activity",
            (),
            Some(&py_kwargs!(py;
                category = "build",
                status = if succeeded { "succeeded" } else { "failed" },
                tone = tone,
                detail = detail,
            )?),
        )?;
        Ok(())
    }
}

/// Whether a cancellation leaves the warehouse query unconfirmed.
fn warehouse_unconfirmed(cancellation: &Option<Any<'_>>, cancellation_error: &Option<Any<'_>>) -> PyResult<bool> {
    if cancellation_error.is_some() {
        return Ok(true);
    }
    let Some(cancellation) = cancellation else {
        return Ok(true);
    };
    Ok(!cancellation.getattr("supported")?.is_truthy()? || !cancellation.getattr("termination_confirmed")?.is_truthy()?)
}

/// `process.wait(timeout=...)`, or `None` when the wait times out.
fn wait_within<'py>(process: &Any<'py>, timeout: &Any<'py>) -> PyResult<Result<Any<'py>, PyErr>> {
    let py: Python<'py> = process.py();
    match process.call_method("wait", (), Some(&py_kwargs!(py; timeout = timeout)?)) {
        Ok(exit_code) => Ok(Ok(exit_code)),
        Err(error) if is_instance(py, &error, "subprocess", "TimeoutExpired")? => Ok(Err(error)),
        Err(error) => Err(error),
    }
}

#[pymethods]
impl BuildProcessManager {
    #[new]
    #[pyo3(signature = (*, reporter, execution_context = None, query_canceller = None, cancellation_reporter = None))]
    fn new(
        py: Python<'_>,
        reporter: Any<'_>,
        execution_context: Option<Any<'_>>,
        query_canceller: Option<Any<'_>>,
        cancellation_reporter: Option<Any<'_>>,
    ) -> PyResult<Self> {
        Ok(Self {
            reporter: reporter.unbind(),
            execution_context: execution_context.and_then(some),
            query_canceller: query_canceller.and_then(some),
            cancellation_reporter: cancellation_reporter.and_then(some),
            lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
            state: Mutex::new(State::default()),
        })
    }

    /// Spawn one build and return its stable launch identity immediately.
    #[pyo3(signature = (
        *,
        project_dir,
        selectors,
        start_time,
        changed = false,
        include_missing_upstream = false,
        deployment_id = None,
        confirmations = None,
        expected_write_scope = None,
        expected_read_scope = None,
        expected_pipeline_scope = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn start<'py>(
        slf: &Bound<'py, Self>,
        project_dir: Any<'py>,
        selectors: Any<'py>,
        start_time: Any<'py>,
        changed: bool,
        include_missing_upstream: bool,
        deployment_id: Option<Any<'py>>,
        confirmations: Option<Any<'py>>,
        expected_write_scope: Option<Any<'py>>,
        expected_read_scope: Option<Any<'py>>,
        expected_pipeline_scope: Option<Any<'py>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let py: Python<'py> = slf.py();
        let this: &Self = slf.get();
        let context: Any<'py> = held(py, &this.execution_context);
        let invocation: Bound<'py, PyTuple> = build_invocation(
            py,
            &selectors,
            &start_time,
            &context,
            changed,
            include_missing_upstream,
            deployment_id,
            confirmations,
        )?;
        let argv: Any<'py> = invocation.get_item(0)?;
        let command: Any<'py> = invocation.get_item(1)?;
        let launch_invocation_id: Any<'py> = attr(py, "uuid", "uuid4")?.call0()?.str()?.into_any();
        let process: Any<'py> = this.locked(py, || {
            let thread: Any<'py> = read!(this, py, cancellation_thread);
            let cancellation_running: bool = !thread.is_none() && thread.call_method0("is_alive")?.is_truthy()?;
            let process = guard(&this.state).process.as_ref().map(|process| process.clone_ref(py));
            if cancellation_running || running(py, &process)? {
                return Err(build_error(py, "BuildInProgressError", &literal(py, "a build is already running"))?);
            }
            let stale_path: Option<PathBuf> = {
                let mut state = guard(&this.state);
                state.statement_count = 0;
                state.stderr_path.take()
            };
            remove_file(stale_path)?;
            {
                let mut state = guard(&this.state);
                state.invocation_id = Some(launch_invocation_id.clone().unbind());
                state.current_invocation_id = None;
                state.cancelling_invocation_id = None;
                state.force_available = false;
                state.active_query_id = None;
                state.cancellation_query_id = None;
                state.cancellation_status = None;
                state.cancellation_error = None;
                state.exit_code = None;
                state.command = Some(command.clone().unbind());
            }
            let started: f64 = monotonic(py)?;
            guard(&this.state).started_monotonic = started;
            let stderr_file: Any<'py> = attr(py, "tempfile", "NamedTemporaryFile")?.call(
                (),
                Some(&py_kwargs!(py; mode = "w+", prefix = "sqltide-run-", suffix = ".stderr", delete = false)?),
            )?;
            let stderr_path: PathBuf = stderr_file.getattr("name")?.extract()?;
            guard(&this.state).stderr_path = Some(stderr_path);
            let environment: Bound<'py, PyDict> = build_child_environment(
                py,
                &context,
                Some(command.clone()),
                Some(launch_invocation_id.clone()),
                expected_write_scope,
                expected_read_scope,
                expected_pipeline_scope,
            )?;
            let spawned: PyResult<Any<'py>> = attr(py, "subprocess", "Popen")?.call(
                (&argv,),
                Some(&py_kwargs!(py;
                    cwd = &project_dir,
                    stdout = attr(py, "subprocess", "PIPE")?,
                    stderr = &stderr_file,
                    text = true,
                    env = environment,
                )?),
            );
            let process: Any<'py> = match spawned {
                Ok(process) => process,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyOSError>(py) => {
                    let failed: PyResult<PyErr> = (|| {
                        stderr_file.call_method0("close")?;
                        let path: Option<PathBuf> = guard(&this.state).stderr_path.take();
                        remove_file(path)?;
                        {
                            let mut state = guard(&this.state);
                            state.invocation_id = None;
                            state.command = None;
                        }
                        let raised: Any<'py> = error.value(py).clone().into_any();
                        let start_error: Any<'py> =
                            attr(py, DEV_EXCEPTIONS, "BuildStartError")?.call1((raised.str()?,))?;
                        raise_from(&start_error, &raised, Some(&raised))
                    })();
                    return Err(match failed {
                        Ok(start_error) => start_error,
                        Err(raised) => while_handling(py, raised, &error),
                    });
                }
                Err(error) => return Err(error),
            };
            guard(&this.state).process = Some(process.clone().unbind());
            stderr_file.call_method0("close")?;
            Ok(process)
        })?;
        let kwargs: Bound<'py, PyDict> = py_kwargs!(py; process = &process, launch_invocation_id = &launch_invocation_id)?;
        attr(py, "threading", "Thread")?
            .call((), Some(&py_kwargs!(py; target = slf.getattr("_consume_stdout")?, kwargs = kwargs, daemon = true)?))?
            .call_method0("start")?;
        let neutral: Any<'py> = attr(py, "sqltide.dev_server.types", "ActivityTone")?.getattr("NEUTRAL")?;
        this.reporter.bind(py).call_method(
            "report_activity",
            (),
            Some(&py_kwargs!(py; category = "build", status = "starting", tone = neutral, detail = &command)?),
        )?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("invocationId", &launch_invocation_id)?;
        payload.set_item("command", &command)?;
        payload.set_item("status", "starting")?;
        Ok(payload)
    }

    /// A cursor read of the live feed: events past `after`, plus run state.
    #[pyo3(signature = (*, after))]
    fn feed<'py>(&self, py: Python<'py>, after: Any<'py>) -> PyResult<Bound<'py, PyDict>> {
        drop(after);
        self.locked(py, || {
            let process = guard(&self.state).process.as_ref().map(|process| process.clone_ref(py));
            let is_running: bool = running(py, &process)?;
            let stderr_path: Option<PathBuf> = guard(&self.state).stderr_path.clone();
            let state = guard(&self.state);
            let payload: Bound<'py, PyDict> = PyDict::new(py);
            payload.set_item("running", is_running)?;
            payload.set_item("invocationId", held(py, &state.invocation_id))?;
            payload.set_item("currentInvocationId", held(py, &state.current_invocation_id))?;
            payload.set_item("command", match &state.command {
                Some(command) => command.bind(py).clone(),
                None => literal(py, ""),
            })?;
            payload.set_item("exitCode", held(py, &state.exit_code))?;
            payload.set_item("events", PyList::empty(py))?;
            payload.set_item("stderr", stderr_tail(&stderr_path)?)?;
            payload.set_item("forceAvailable", state.force_available)?;
            payload.set_item("activeQueryId", held(py, &state.active_query_id))?;
            payload.set_item("cancellationStatus", held(py, &state.cancellation_status))?;
            payload.set_item("cancellationError", held(py, &state.cancellation_error))?;
            payload.set_item("cancellationQueryId", held(py, &state.cancellation_query_id))?;
            Ok(payload)
        })
    }

    /// Request graceful cancellation of the server-owned child.
    #[pyo3(signature = (*, invocation_id))]
    fn cancel<'py>(slf: &Bound<'py, Self>, invocation_id: Any<'py>) -> PyResult<Bound<'py, PyDict>> {
        let py: Python<'py> = slf.py();
        let this: &Self = slf.get();
        let requested = this.locked(py, || {
            if read!(this, py, cancelling_invocation_id).eq(&invocation_id)? {
                return Ok(Err(this.cancellation_payload_locked(py)?));
            }
            let process: Any<'py> = this.owned_process_locked(py, &invocation_id)?;
            let query_id: Any<'py> = read!(this, py, active_query_id);
            {
                let mut state = guard(&this.state);
                state.cancelling_invocation_id = Some(invocation_id.clone().unbind());
                state.cancellation_status = Some(literal(py, "cancelling").unbind());
                state.cancellation_error = None;
                state.cancellation_query_id = some(query_id.clone());
            }
            let now: Any<'py> = attr(py, "datetime", "datetime")?
                .call_method("now", (), Some(&py_kwargs!(py; tz = attr(py, "datetime", "UTC")?)?))?;
            let requested_at: Any<'py> = now.call_method("isoformat", (), Some(&py_kwargs!(py; timespec = "milliseconds")?))?;
            Ok(Ok((process, query_id, requested_at)))
        })?;
        let (process, query_id, requested_at) = match requested {
            Ok(requested) => requested,
            Err(payload) => return Ok(payload),
        };
        process.call_method1("send_signal", (attr(py, "signal", "SIGINT")?,))?;
        let kwargs: Bound<'py, PyDict> = py_kwargs!(py;
            process = &process,
            invocation_id = &invocation_id,
            query_id = query_id,
            requested_at = requested_at,
        )?;
        let thread: Any<'py> = attr(py, "threading", "Thread")?
            .call((), Some(&py_kwargs!(py; target = slf.getattr("_complete_cancellation")?, kwargs = kwargs, daemon = true)?))?;
        this.locked(py, || {
            guard(&this.state).cancellation_thread = Some(thread.clone().unbind());
            Ok(())
        })?;
        thread.call_method0("start")?;
        this.locked(py, || this.cancellation_payload_locked(py))
    }

    /// The cancellation worker: terminate the warehouse query, wait out the grace periods and
    /// record the terminal cancellation outcome.
    #[pyo3(signature = (*, process, invocation_id, query_id, requested_at))]
    fn _complete_cancellation(
        &self,
        py: Python<'_>,
        process: Any<'_>,
        invocation_id: Any<'_>,
        query_id: Any<'_>,
        requested_at: Any<'_>,
    ) -> PyResult<()> {
        let mut cancellation: Option<Any<'_>> = None;
        let mut cancellation_error: Option<Any<'_>> = None;
        if !query_id.is_none() {
            match &self.query_canceller {
                None => cancellation_error = Some(literal(py, "Warehouse query cancellation is unavailable")),
                Some(canceller) => match canceller.bind(py).call1((&query_id,)) {
                    Ok(result) => cancellation = Some(result),
                    Err(error) if error.is_instance_of::<PyException>(py) => {
                        cancellation_error =
                            Some(error.value(py).str().map_err(|raised| while_handling(py, raised, &error))?.into_any());
                    }
                    Err(error) => return Err(error),
                },
            }
        }
        let has_query: bool = !query_id.is_none();
        let exit_code: Any<'_> = match wait_within(&process, &attr(py, DEV_CONSTANTS, "CANCEL_GRACE_SECONDS")?)? {
            Ok(exit_code) => exit_code,
            Err(timeout) => {
                let terminated: PyResult<Any<'_>> = (|| {
                    process.call_method0("terminate")?;
                    match wait_within(&process, &attr(py, DEV_CONSTANTS, "TERMINATE_GRACE_SECONDS")?)? {
                        Ok(exit_code) => Ok(Some(exit_code)),
                        Err(second_timeout) => {
                            let waited: PyResult<Option<Any<'_>>> = (|| {
                                let interim_status: &str =
                                    if has_query && warehouse_unconfirmed(&cancellation, &cancellation_error)? {
                                        "cancellation_failed"
                                    } else {
                                        "cancelling"
                                    };
                                let current: bool = self.locked(py, || {
                                    if !self.current(py, &process, &invocation_id)? {
                                        return Ok(false);
                                    }
                                    let mut state = guard(&self.state);
                                    state.force_available = true;
                                    state.cancellation_status = Some(literal(py, interim_status).unbind());
                                    state.cancellation_error = cancellation_error.clone().map(Bound::unbind);
                                    Ok(true)
                                })?;
                                if !current {
                                    return Ok(None);
                                }
                                Ok(Some(process.call_method0("wait")?))
                            })();
                            waited.map_err(|raised| while_handling(py, raised, &second_timeout))
                        }
                    }
                    .map(|exit_code| exit_code.unwrap_or_else(|| py.None().into_bound(py)))
                })();
                let exit_code: Any<'_> = terminated.map_err(|raised| while_handling(py, raised, &timeout))?;
                if exit_code.is_none() {
                    return Ok(());
                }
                exit_code
            }
        };
        let warehouse_failed: bool = has_query && warehouse_unconfirmed(&cancellation, &cancellation_error)?;
        if warehouse_failed
            && cancellation_error.is_none()
            && let Some(cancellation) = &cancellation
        {
            let detail: Any<'_> = cancellation.getattr("detail")?;
            cancellation_error = Some(if detail.is_truthy()? {
                detail
            } else {
                literal(py, "Warehouse query cancellation was not confirmed")
            });
        }
        let status: &str = if warehouse_failed { "cancellation_failed" } else { "cancelled" };
        let current: bool = self.locked(py, || {
            if !self.current(py, &process, &invocation_id)? {
                return Ok(false);
            }
            let mut state = guard(&self.state);
            state.force_available = false;
            state.cancellation_status = Some(literal(py, status).unbind());
            state.cancellation_error = cancellation_error.clone().map(Bound::unbind);
            state.exit_code = Some(exit_code.clone().unbind());
            Ok(true)
        })?;
        if !current {
            return Ok(());
        }
        let reported = |field: &str| -> PyResult<Any<'_>> {
            match &cancellation {
                None => Ok(py.None().into_bound(py)),
                Some(cancellation) => cancellation.getattr(field),
            }
        };
        let outcome: Any<'_> = attr(py, "sqltide.dev_server.models", "BuildCancellationOutcome")?.call(
            (),
            Some(&py_kwargs!(py;
                invocation_id = &invocation_id,
                query_id = &query_id,
                requested_at = &requested_at,
                status = status,
                process_exit_code = &exit_code,
                warehouse_supported = reported("supported")?,
                query_found = reported("query_found")?,
                warehouse_termination_confirmed = reported("termination_confirmed")?,
                error_message = cancellation_error.clone(),
            )?),
        )?;
        if let Some(reporter) = &self.cancellation_reporter {
            match reporter.bind(py).call1((&outcome,)) {
                Ok(_) => {}
                Err(error) if error.is_instance_of::<PyException>(py) => {
                    let recorded: PyResult<()> = (|| {
                        let message: Any<'_> = fstring(
                            py,
                            &[literal(py, "Cancellation outcome persistence failed: "), error.value(py).clone().into_any()],
                        )?;
                        self.locked(py, || {
                            let mut state = guard(&self.state);
                            state.cancellation_status = Some(literal(py, "cancellation_failed").unbind());
                            state.cancellation_error = Some(message.unbind());
                            Ok(())
                        })
                    })();
                    recorded.map_err(|raised| while_handling(py, raised, &error))?;
                }
                Err(error) => return Err(error),
            }
        }
        let current_thread: Any<'_> = attr(py, "threading", "current_thread")?.call0()?;
        self.locked(py, || {
            let mut state = guard(&self.state);
            if state.cancellation_thread.as_ref().is_some_and(|thread| thread.bind(py).is(&current_thread)) {
                state.cancellation_thread = None;
            }
            Ok(())
        })
    }

    /// Force-kill the server-owned child without fabricating terminal facts.
    #[pyo3(signature = (*, invocation_id))]
    fn kill<'py>(&self, py: Python<'py>, invocation_id: Any<'py>) -> PyResult<Bound<'py, PyDict>> {
        let process: Any<'py> = self.locked(py, || self.owned_process_locked(py, &invocation_id))?;
        process.call_method0("kill")?;
        self.locked(py, || {
            guard(&self.state).force_available = false;
            Ok(())
        })?;
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("status", "killed")?;
        Ok(payload)
    }

    /// The stdout reader: follow the child's event stream, then record its exit.
    #[pyo3(signature = (*, process, launch_invocation_id))]
    fn _consume_stdout(&self, py: Python<'_>, process: Any<'_>, launch_invocation_id: Any<'_>) -> PyResult<()> {
        let stdout: Any<'_> = process.getattr("stdout")?;
        if stdout.is_none() {
            return Ok(());
        }
        let loads: Any<'_> = attr(py, "json", "loads")?;
        for line in stdout.try_iter()? {
            let event: Any<'_> = match loads.call1((line?,)) {
                Ok(event) if event.is_instance_of::<PyDict>() => event,
                Ok(_) => continue,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => continue,
                Err(error) => return Err(error),
            };
            self.locked(py, || {
                if !self.current(py, &process, &launch_invocation_id)? {
                    return Ok(());
                }
                if event.call_method1("get", ("event",))?.eq(RUN_STARTED_KIND)? {
                    let invocation_id: Any<'_> = event.call_method1("get", ("invocationId",))?.str()?.into_any();
                    guard(&self.state).current_invocation_id = Some(invocation_id.unbind());
                }
                if event.call_method1("get", ("event",))?.eq(STATEMENT_STARTED_KIND)? {
                    let query_id: Any<'_> = event.call_method1("get", ("queryId",))?;
                    let active: Option<Py<PyAny>> =
                        if query_id.is_none() { None } else { Some(query_id.str()?.into_any().unbind()) };
                    guard(&self.state).active_query_id = active;
                }
                if event.call_method1("get", ("event",))?.eq(STATEMENT_COMPLETED_KIND)? {
                    let mut state = guard(&self.state);
                    state.statement_count += 1;
                    state.active_query_id = None;
                }
                Ok(())
            })?;
        }
        let exit_code: Any<'_> = process.call_method0("wait")?;
        let finished: Option<(f64, u64)> = self.locked(py, || {
            if !self.current(py, &process, &launch_invocation_id)? {
                return Ok(None);
            }
            let now: f64 = monotonic(py)?;
            let mut state = guard(&self.state);
            state.exit_code = Some(exit_code.clone().unbind());
            state.force_available = false;
            Ok(Some((now - state.started_monotonic, state.statement_count)))
        })?;
        match finished {
            Some((elapsed_seconds, statement_count)) => self.report_finished(py, &exit_code, elapsed_seconds, statement_count),
            None => Ok(()),
        }
    }

    /// Release server-owned temporary evidence without signalling the child.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.locked(py, || {
            let path: Option<PathBuf> = guard(&self.state).stderr_path.take();
            remove_file(path)
        })
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<BuildProcessManager>()?;
    module.add_function(wrap_pyfunction!(build_invocation, module)?)?;
    module.add_function(wrap_pyfunction!(build_child_environment, module)?)
}

#[cfg(test)]
mod tests {
    use super::{shell_quoted, split_lines};

    #[test]
    fn split_lines_matches_python_boundaries() {
        assert_eq!(split_lines("a\r\nb\rc\nd\u{2028}e\x0cf\n"), vec!["a", "b", "c", "d", "e", "f"]);
        assert_eq!(split_lines("\n\n"), vec!["", ""]);
        assert_eq!(split_lines(""), Vec::<&str>::new());
    }

    #[test]
    fn shell_quoting_matches_shlex() {
        assert_eq!(shell_quoted(b""), b"''".to_vec());
        assert_eq!(shell_quoted(b"orders+"), b"orders+".to_vec());
        assert_eq!(shell_quoted(b"it's here"), b"'it'\"'\"'s here'".to_vec());
    }
}
