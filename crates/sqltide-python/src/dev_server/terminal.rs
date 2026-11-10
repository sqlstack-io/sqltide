//! `sqt dev`: the command handler that serves the dev UI and API, and the terminal voice that
//! narrates the session with a startup banner and one aligned activity line per server event.

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyString, PyTuple};

use super::audits::{fstring, literal};
use super::support::attr;
use crate::py_kwargs;

const NATIVE: &str = "sqltide._native";
const DEV_TYPES: &str = "sqltide.dev_server.types";
const FACT_LABEL_WIDTH: usize = 9;
const FACT_VALUE_INDENT: &str = "             ";
const CATEGORY_COLUMN_WIDTH: usize = 6;
const STATUS_COLUMN_WIDTH: usize = 9;
const SEPARATOR: &str = " · ";

type Any<'py> = Bound<'py, PyAny>;

fn tone<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, DEV_TYPES, "ActivityTone")?.getattr(name)
}

fn compile_ok(outcome: &Any<'_>) -> PyResult<bool> {
    let py: Python<'_> = outcome.py();
    outcome
        .getattr("state")?
        .eq(attr(py, DEV_TYPES, "CompileStateKind")?.getattr("OK")?)
}

fn styled<'py>(style: &Any<'py>, method: &str, text: Any<'py>) -> PyResult<Any<'py>> {
    style.call_method1(method, (text,))
}

fn ljust<'py>(text: &Any<'py>, width: usize) -> PyResult<Any<'py>> {
    text.call_method1("ljust", (width,))
}

/// `f"  {style.label(label.ljust(9))}  {value}"`, with `value` already evaluated.
fn fact_line<'py>(style: &Any<'py>, label: &str, value: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = style.py();
    let label: Any<'py> = styled(
        style,
        "label",
        ljust(&literal(py, label), FACT_LABEL_WIDTH)?,
    )?;
    fstring(py, &[literal(py, "  "), label, literal(py, "  "), value])
}

/// `f"{count} {noun}"`, pluralised unless `count == 1`.
fn counted(count: usize, noun: &str) -> String {
    if count == 1 {
        format!("{count} {noun}")
    } else {
        format!("{count} {noun}s")
    }
}

/// Model and source counts plus total compile time, or `""` without an analysis.
fn compile_counts<'py>(outcome: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = outcome.py();
    let analysis: Any<'py> = outcome.getattr("analysis")?;
    if analysis.is_none() {
        return Ok(literal(py, ""));
    }
    let project: Any<'py> = analysis.getattr("compiled_project")?;
    let models: String = counted(project.getattr("models")?.len()?, "model");
    let sources: String = counted(project.getattr("sources")?.len()?, "source");
    let counts: Any<'py> = literal(py, &format!("{models}{SEPARATOR}{sources}"));
    let timings: Any<'py> = outcome.getattr("timings")?;
    if timings.is_none() {
        return Ok(counts);
    }
    let mut total: Any<'py> = timings.getattr("discovery_ms")?;
    for field in [
        "compile_inputs_ms",
        "assembly_ms",
        "graph_ms",
        "realization_ms",
    ] {
        total = total.add(timings.getattr(field)?)?;
    }
    fstring(
        py,
        &[counts, literal(py, SEPARATOR), total, literal(py, "ms")],
    )
}

fn compile_value<'py>(style: &Any<'py>, outcome: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = style.py();
    let outcome_word = |text: &str, passed: bool| {
        style.call_method(
            "outcome",
            (),
            Some(&py_kwargs!(py; text = text, passed = passed)?),
        )
    };
    if !compile_ok(outcome)? {
        return outcome_word("failing", false);
    }
    let ok_text: Any<'py> = outcome_word("ok", true)?;
    let counts: Any<'py> = compile_counts(outcome)?;
    if counts.is_truthy()? {
        fstring(py, &[ok_text, literal(py, SEPARATOR), counts])
    } else {
        Ok(ok_text)
    }
}

fn compile_error_lines<'py>(style: &Any<'py>, outcome: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = style.py();
    let error: Any<'py> = outcome.getattr("error")?;
    if error.is_none() {
        return Ok(Vec::new());
    }
    let mut location: Any<'py> = literal(py, "");
    let path: Any<'py> = error.getattr("path")?;
    if !path.is_none() {
        location = fstring(
            py,
            &[
                path,
                literal(py, ":"),
                error.getattr("line")?,
                literal(py, ":"),
                error.getattr("column")?,
            ],
        )?;
    }
    let prefix: Any<'py> = if location.is_truthy()? {
        fstring(
            py,
            &[
                literal(py, FACT_VALUE_INDENT),
                styled(style, "muted", location)?,
                literal(py, "  "),
            ],
        )?
    } else {
        literal(py, FACT_VALUE_INDENT)
    };
    Ok(vec![fstring(py, &[prefix, error.getattr("message")?])?])
}

fn warehouse_value<'py>(
    style: &Any<'py>,
    database: &Any<'py>,
    connected: bool,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = style.py();
    if database.is_none() {
        return styled(style, "warning", literal(py, "not connected"));
    }
    let state: Any<'py> = if connected {
        styled(style, "passed", literal(py, "connected"))?
    } else {
        styled(style, "warning", literal(py, "connecting"))?
    };
    fstring(py, &[state, literal(py, SEPARATOR), database.clone()])
}

/// Build the startup banner: dim labels, coloured state words, plain facts.
#[pyfunction]
#[pyo3(signature = (*, style, outcome, project_dir, database, warehouse_connected = true, host, port, tool_version))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn startup_lines<'py>(
    style: &Any<'py>,
    outcome: &Any<'py>,
    project_dir: &Any<'py>,
    database: &Any<'py>,
    warehouse_connected: bool,
    host: &Any<'py>,
    port: &Any<'py>,
    tool_version: &Any<'py>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = style.py();
    let base_url: Any<'py> = fstring(
        py,
        &[
            literal(py, "http://"),
            host.clone(),
            literal(py, ":"),
            port.clone(),
        ],
    )?;
    let title: Any<'py> = styled(style, "title", literal(py, "SQLTide dev server"))?;
    let version: Any<'py> = styled(style, "muted", literal(py, "v").add(tool_version)?)?;
    let mut lines: Vec<Any<'py>> = vec![
        fstring(py, &[title, literal(py, " "), version])?,
        literal(py, ""),
    ];
    lines.push(fact_line(style, "project", project_dir.getattr("name")?)?);
    let value: Any<'py> = compile_value(style, outcome)?;
    lines.push(fact_line(style, "compile", value)?);
    lines.extend(compile_error_lines(style, outcome)?);
    let value: Any<'py> = warehouse_value(style, database, warehouse_connected)?;
    lines.push(fact_line(style, "warehouse", value)?);
    lines.push(literal(py, ""));
    lines.push(fact_line(style, "ui", base_url.clone())?);
    lines.push(fact_line(
        style,
        "api",
        fstring(py, &[base_url, literal(py, "/api")])?,
    )?);
    lines.push(literal(py, ""));
    let stop: Any<'py> = styled(style, "muted", literal(py, "ctrl+c to stop"))?;
    lines.push(fstring(py, &[literal(py, "  "), stop])?);
    lines.push(literal(py, ""));
    PyTuple::new(py, lines)
}

/// Reduce one reload outcome to an activity `(status, tone, detail)` triple.
#[pyfunction]
#[pyo3(signature = (*, outcome))]
pub(crate) fn reload_summary<'py>(outcome: &Any<'py>) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = outcome.py();
    if compile_ok(outcome)? {
        return PyTuple::new(
            py,
            [
                literal(py, "ok"),
                tone(py, "GOOD")?,
                compile_counts(outcome)?,
            ],
        );
    }
    let error: Any<'py> = outcome.getattr("error")?;
    let message: Any<'py> = if error.is_none() {
        literal(py, "compile failed")
    } else {
        error.getattr("message")?
    };
    PyTuple::new(py, [literal(py, "failing"), tone(py, "BAD")?, message])
}

/// Build one aligned activity line: dim time, bold category, toned status.
#[pyfunction]
#[pyo3(signature = (*, style, timestamp, category, status, tone, detail))]
pub(crate) fn activity_line<'py>(
    style: &Any<'py>,
    timestamp: &Any<'py>,
    category: &Any<'py>,
    status: &Any<'py>,
    tone: &Any<'py>,
    detail: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = style.py();
    let category_text: Any<'py> =
        styled(style, "subsection", ljust(category, CATEGORY_COLUMN_WIDTH)?)?;
    let padded: Any<'py> = ljust(status, STATUS_COLUMN_WIDTH)?;
    let tones = attr(py, DEV_TYPES, "ActivityTone")?;
    let status_text: Any<'py> = if tone.eq(tones.getattr("GOOD")?)? {
        styled(style, "passed", padded)?
    } else if tone.eq(tones.getattr("BAD")?)? {
        styled(style, "failed", padded)?
    } else if tone.eq(tones.getattr("CAUTION")?)? {
        styled(style, "warning", padded)?
    } else {
        padded
    };
    let separator: Any<'py> = literal(py, "  ");
    fstring(
        py,
        &[
            styled(style, "muted", timestamp.clone())?,
            separator.clone(),
            category_text,
            separator.clone(),
            status_text,
            separator,
            detail.clone(),
        ],
    )?
    .call_method0("rstrip")
}

/// Prints the startup banner and one aligned activity line per server event.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct DevTerminalReporter {
    style: Py<PyAny>,
    lock: Py<PyAny>,
}

impl DevTerminalReporter {
    fn print(&self, py: Python<'_>, text: Any<'_>) -> PyResult<()> {
        crate::handling::within(self.lock.bind(py), || {
            attr(py, "builtins", "print")?.call((text,), Some(&py_kwargs!(py; flush = true)?))?;
            Ok(())
        })
    }
}

#[pymethods]
impl DevTerminalReporter {
    #[new]
    #[pyo3(signature = (*, style))]
    fn new(py: Python<'_>, style: Any<'_>) -> PyResult<Self> {
        Ok(Self {
            style: style.unbind(),
            lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
        })
    }

    /// Print the startup banner for one compiled (or failing) project.
    #[pyo3(signature = (*, outcome, project_dir, database, warehouse_connected, host, port))]
    #[allow(clippy::too_many_arguments)]
    fn report_startup(
        &self,
        py: Python<'_>,
        outcome: Any<'_>,
        project_dir: Any<'_>,
        database: Any<'_>,
        warehouse_connected: bool,
        host: Any<'_>,
        port: Any<'_>,
    ) -> PyResult<()> {
        let tool_version: Any<'_> =
            attr(py, "importlib.metadata", "version")?.call1(("sqltide",))?;
        let lines: Bound<'_, PyTuple> = startup_lines(
            self.style.bind(py),
            &outcome,
            &project_dir,
            &database,
            warehouse_connected,
            &host,
            &port,
            &tool_version,
        )?;
        self.print(py, PyString::new(py, "\n").call_method1("join", (lines,))?)
    }

    /// Print one activity line for a project reload outcome.
    #[pyo3(signature = (*, outcome))]
    fn report_reload(&self, py: Python<'_>, outcome: Any<'_>) -> PyResult<()> {
        let summary: Bound<'_, PyTuple> = reload_summary(&outcome)?;
        self.report_activity(
            py,
            literal(py, "reload"),
            summary.get_item(0)?,
            summary.get_item(1)?,
            summary.get_item(2)?,
        )
    }

    /// Print one timestamped activity line.
    #[pyo3(signature = (*, category, status, tone, detail))]
    fn report_activity(
        &self,
        py: Python<'_>,
        category: Any<'_>,
        status: Any<'_>,
        tone: Any<'_>,
        detail: Any<'_>,
    ) -> PyResult<()> {
        let timestamp: Any<'_> = attr(py, "datetime", "datetime")?
            .call_method0("now")?
            .call_method1("strftime", ("%H:%M:%S",))?;
        let line: Any<'_> = activity_line(
            self.style.bind(py),
            &timestamp,
            &category,
            &status,
            &tone,
            &detail,
        )?;
        self.print(py, line)
    }

    /// Print the shutdown line after the server stops.
    fn report_shutdown(&self, py: Python<'_>) -> PyResult<()> {
        let line: Any<'_> = styled(
            self.style.bind(py),
            "muted",
            literal(py, "dev server stopped"),
        )?;
        self.print(py, line)
    }
}

/// Serve the dev UI and API over this project until interrupted.
#[pyfunction]
#[pyo3(signature = (
    *,
    options,
    client,
    observation_client = None,
    connection_factory = None,
    observation_connection_factory = None,
    loaded_project,
    adapter_profile,
))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_dev<'py>(
    py: Python<'py>,
    options: Any<'py>,
    client: Any<'py>,
    observation_client: Option<Any<'py>>,
    connection_factory: Option<Any<'py>>,
    observation_connection_factory: Option<Any<'py>>,
    loaded_project: Any<'py>,
    adapter_profile: Any<'py>,
) -> PyResult<Any<'py>> {
    let project_dir: Any<'py> = options.getattr("pipelines_root")?.getattr("parent")?;
    let configuration: Any<'py> = attr(py, "builtins", "getattr")?.call1((
        &loaded_project,
        "effective_configuration",
        py.None(),
    ))?;
    let defaults: Any<'py> = if configuration.is_none() {
        attr(py, "sqltide.compiler.discovery.models", "ProjectDefaults")?.call0()?
    } else {
        configuration.getattr("defaults")?
    };
    let context: Any<'py> = attr(py, "sqltide.dev_server.models", "DevExecutionContext")?.call(
        (),
        Some(&py_kwargs!(py;
            database = options.getattr("database")?,
            selected_target = options.getattr("selected_target")?,
            cli_variables = options.getattr("cli_variables")?,
            environment = options.getattr("environment")?,
            connection_host = options.getattr("connection_host")?,
            connection_port = options.getattr("connection_port")?,
            connection_username = options.getattr("connection_username")?,
            connection_password = options.getattr("connection_password")?,
            run_presumed_failed_after_seconds = defaults.getattr("run_presumed_failed_after_seconds")?,
            connection_factory = connection_factory,
            observation_connection_factory = observation_connection_factory,
        )?),
    )?;
    let compile_inputs: [Py<PyAny>; 5] = [
        project_dir.clone().unbind(),
        context.clone().unbind(),
        loaded_project.unbind(),
        options.getattr("pipelines_root")?.unbind(),
        adapter_profile.unbind(),
    ];
    let run_compile = move |arguments: &Bound<'_, PyTuple>,
                            _keywords: Option<&Bound<'_, PyDict>>|
          -> PyResult<Py<PyAny>> {
        let py: Python<'_> = arguments.py();
        let [
            project_dir,
            context,
            loaded_project,
            pipelines_root,
            adapter_profile,
        ] = &compile_inputs;
        let context: &Any<'_> = context.bind(py);
        let reloaded: Any<'_> = if loaded_project.is_none(py) {
            py.None().into_bound(py)
        } else {
            let cli_variables: Any<'_> =
                attr(py, "builtins", "dict")?.call1((context.getattr("cli_variables")?,))?;
            attr(py, NATIVE, "load_project_input_for_path")?.call(
                (),
                Some(&py_kwargs!(py;
                    path = project_dir.bind(py),
                    selected_target = context.getattr("selected_target")?,
                    cli_variables = cli_variables,
                    environment = context.getattr("environment")?,
                )?),
            )?
        };
        Ok(attr(py, NATIVE, "analyze_project")?
            .call(
                (),
                Some(&py_kwargs!(py;
                    pipelines_root = pipelines_root.bind(py),
                    loaded_project = reloaded,
                    adapter_profile = adapter_profile.bind(py),
                )?),
            )?
            .unbind())
    };
    let run_compile: Any<'py> = PyCFunction::new_closure(py, None, None, run_compile)?.into_any();
    let style: Any<'py> = attr(py, NATIVE, "cli_style")?.call0()?;
    let reporter: Any<'py> =
        attr(py, NATIVE, "DevTerminalReporter")?.call((), Some(&py_kwargs!(py; style = style)?))?;
    let configured: Any<'py> = options.getattr("auth_settings")?;
    let auth_settings: Any<'py> = if configured.is_truthy()? {
        configured
    } else {
        let control_store_url: Any<'py> = attr(
            py,
            "sqltide.auth.main.default_control_store_url",
            "default_control_store_url",
        )?
        .call((), Some(&py_kwargs!(py; project_dir = &project_dir)?))?;
        attr(py, "sqltide.auth.models", "AuthSettings")?.call(
            (),
            Some(&py_kwargs!(py;
                mode = attr(py, "sqltide.auth.types", "AuthenticationMode")?.getattr("DISABLED")?,
                control_store_url = control_store_url,
            )?),
        )?
    };
    attr(py, NATIVE, "run_dev_server")?.call(
        (),
        Some(&py_kwargs!(py;
            run_compile = run_compile,
            connection = client,
            observation_connection = observation_client,
            database = options.getattr("database")?,
            project_dir = &project_dir,
            host = options.getattr("host")?,
            port = options.getattr("port")?,
            reporter = reporter,
            execution_context = context,
            auth_settings = auth_settings,
        )?),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<DevTerminalReporter>()?;
    module.add_function(wrap_pyfunction!(startup_lines, module)?)?;
    module.add_function(wrap_pyfunction!(reload_summary, module)?)?;
    module.add_function(wrap_pyfunction!(activity_line, module)?)?;
    module.add_function(wrap_pyfunction!(run_dev, module)?)
}
