//! Operator-facing error text: CLI user errors, structured diagnostics and expected warehouse
//! failures.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyMapping};
use pyo3::wrap_pyfunction;

use pyo3::exceptions::PyValueError;
use sqltide_json::{JsonDialect, JsonValue, StdlibJsonOptions, dumps};

use crate::cli::output::{json_value, object};
use crate::cli::python::getattr_none;
use crate::dev_server::support::attr;

const DIAGNOSTIC_MODELS: &str = "sqltide.diagnostics.models";
const DIAGNOSTIC_TYPES: &str = "sqltide.diagnostics.types";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const METADATA_TABLE_NAME_PREFIX: &str = "sqltide_";
const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";
const TRUTHY_ENV_VALUES: [&str; 3] = ["1", "true", "yes"];

/// Whether human output is coloured: `NO_COLOR` wins, then `CLICOLOR_FORCE`/`FORCE_COLOR`, then
/// whether stdout is a terminal.
pub(crate) fn use_color(py: Python<'_>) -> PyResult<bool> {
    let getenv: Bound<'_, PyAny> = attr(py, "os", "getenv")?;
    if getenv.call1(("NO_COLOR",))?.is_truthy()? {
        return Ok(false);
    }
    for name in ["CLICOLOR_FORCE", "FORCE_COLOR"] {
        let value: Bound<'_, PyAny> = getenv.call1((name,))?;
        if !value.is_none() && TRUTHY_ENV_VALUES.contains(&value.extract::<String>()?.as_str()) {
            return Ok(true);
        }
    }
    py.import("sys")?
        .getattr("stdout")?
        .call_method0("isatty")?
        .is_truthy()
}

fn styled(color: bool, prefix: &str, text: &str) -> String {
    if !color || text.is_empty() {
        return text.to_owned();
    }
    format!("{prefix}{text}{RESET}")
}

/// A `CliUserError` as a red `Error` heading, its message and an optional yellow hint.
pub(crate) fn render_cli_user_error(py: Python<'_>, error: &PyErr) -> PyResult<String> {
    let color: bool = use_color(py)?;
    let value: &Bound<'_, PyAny> = error.value(py).as_any();
    let mut lines: Vec<String> = vec![
        styled(color, &format!("{RED}{BOLD}"), "Error"),
        value.str()?.to_string(),
    ];
    let hint: Bound<'_, PyAny> = value.getattr("hint")?;
    if !hint.is_none() {
        lines.push(String::new());
        lines.push(format!(
            "{}: {}",
            styled(color, YELLOW, "Hint"),
            hint.str()?
        ));
    }
    Ok(lines.join("\n"))
}

/// An exception's structured diagnostic text, or its ordinary message.
pub(crate) fn render_error(py: Python<'_>, error: &Bound<'_, PyAny>) -> PyResult<String> {
    let diagnostic: Bound<'_, PyAny> = getattr_none(error, "diagnostic")?;
    if diagnostic.is_instance(&attr(py, DIAGNOSTIC_MODELS, "CompilerDiagnostic")?)? {
        return render_diagnostic_text(&diagnostic, None);
    }
    Ok(error.str()?.to_string())
}

/// Python's `str.splitlines()` without line ends.
fn split_lines(text: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = Vec::new();
    let mut start: usize = 0;
    let mut characters = text.char_indices().peekable();
    while let Some((index, character)) = characters.next() {
        let breaks: bool = matches!(
            character,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !breaks {
            continue;
        }
        lines.push(&text[start..index]);
        start = index + character.len_utf8();
        if character == '\r' && characters.peek().is_some_and(|(_, next)| *next == '\n') {
            characters.next();
            start += 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// One location's `-->` line and, when its source line exists, the caret excerpt.
fn render_location(
    location: &Bound<'_, PyAny>,
    source: Option<String>,
    label: Option<String>,
) -> PyResult<Vec<String>> {
    let line: i64 = location.getattr("line")?.extract()?;
    let column: i64 = location.getattr("column")?.extract()?;
    let prefix: String = label.map_or_else(String::new, |label| format!("{label}: "));
    let mut lines: Vec<String> = vec![format!(
        "{prefix}--> {}:{line}:{column}",
        location.getattr("path")?.str()?
    )];
    let source: String = source.unwrap_or_default();
    let source_lines: Vec<&str> = split_lines(&source);
    if line < 1 || line > source_lines.len() as i64 {
        return Ok(lines);
    }
    let source_line: &str = source_lines[(line - 1) as usize];
    let start: i64 = column.max(1);
    let end_line: Bound<'_, PyAny> = location.getattr("end_line")?;
    let end_column: Bound<'_, PyAny> = location.getattr("end_column")?;
    let end: i64 = if end_column.is_none() || !end_line.eq(line)? {
        start
    } else {
        end_column.extract::<i64>()?.max(start)
    };
    lines.push("  |".to_owned());
    lines.push(format!("{line:>3} | {source_line}"));
    lines.push(format!(
        "  | {}{}",
        " ".repeat((start - 1) as usize),
        "^".repeat((end - start + 1).max(1) as usize)
    ));
    Ok(lines)
}

/// `source_by_path.get(path)`.
fn source_for(
    sources: Option<&Bound<'_, PyAny>>,
    location: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    let Some(sources) = sources else {
        return Ok(None);
    };
    let source: Bound<'_, PyAny> = sources.call_method1("get", (location.getattr("path")?,))?;
    if source.is_none() {
        return Ok(None);
    }
    Ok(Some(source.extract()?))
}

/// One structured diagnostic with stable source context when available.
pub(crate) fn render_diagnostic_text(
    diagnostic: &Bound<'_, PyAny>,
    sources: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    let mut lines: Vec<String> = vec![
        format!(
            "{} [{}] {}",
            diagnostic.getattr("severity")?.str()?,
            diagnostic.getattr("code")?.str()?,
            diagnostic.getattr("message")?.str()?
        ),
        format!("phase: {}", diagnostic.getattr("phase")?.str()?),
    ];
    let resource_name: Bound<'_, PyAny> = diagnostic.getattr("resource_name")?;
    if !resource_name.is_none() {
        lines.push(format!("resource: {}", resource_name.str()?));
    }
    let location: Bound<'_, PyAny> = diagnostic.getattr("location")?;
    if !location.is_none() {
        lines.extend(render_location(
            &location,
            source_for(sources, &location)?,
            None,
        )?);
    }
    for related in diagnostic.getattr("related_locations")?.try_iter()? {
        let related: Bound<'_, PyAny> = related?;
        let related_location: Bound<'_, PyAny> = related.getattr("location")?;
        lines.extend(render_location(
            &related_location,
            source_for(sources, &related_location)?,
            Some(related.getattr("label")?.str()?.to_string()),
        )?);
        let message: Bound<'_, PyAny> = related.getattr("message")?;
        if !message.is_none() {
            lines.push(message.str()?.to_string());
        }
    }
    let help: Bound<'_, PyAny> = diagnostic.getattr("help")?;
    if !help.is_none() {
        lines.push(format!("help: {}", help.str()?));
    }
    Ok(lines.join("\n"))
}

/// Give `error` a diagnostic unless its owner already supplied one; a `SourceLocation` the error
/// owns wins over `location`.
pub(crate) fn attach_error_diagnostic<'py>(
    py: Python<'py>,
    error: &Bound<'py, PyAny>,
    phase: Bound<'py, PyAny>,
    code: &str,
    resource_name: Bound<'py, PyAny>,
    location: Bound<'py, PyAny>,
) -> PyResult<()> {
    let model: Bound<'_, PyAny> = attr(py, DIAGNOSTIC_MODELS, "CompilerDiagnostic")?;
    if getattr_none(error, "diagnostic")?.is_instance(&model)? {
        return Ok(());
    }
    let owned_location: Bound<'_, PyAny> = getattr_none(error, "location")?;
    let location: Bound<'_, PyAny> =
        if owned_location.is_instance(&attr(py, DIAGNOSTIC_MODELS, "SourceLocation")?)? {
            owned_location
        } else {
            location
        };
    let kwargs: Bound<'_, PyDict> = PyDict::new(py);
    kwargs.set_item("phase", phase)?;
    kwargs.set_item(
        "severity",
        attr(py, DIAGNOSTIC_TYPES, "DiagnosticSeverity")?.getattr("ERROR")?,
    )?;
    kwargs.set_item("code", code)?;
    kwargs.set_item("message", error.str()?)?;
    kwargs.set_item("resource_name", resource_name)?;
    kwargs.set_item("location", location)?;
    error
        .getattr("__dict__")?
        .set_item("diagnostic", model.call((), Some(&kwargs))?)
}

fn location_payload(location: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if location.is_none() {
        return Ok(JsonValue::Null);
    }
    Ok(object(vec![
        ("column", json_value(&location.getattr("column")?)?),
        ("end_column", json_value(&location.getattr("end_column")?)?),
        ("end_line", json_value(&location.getattr("end_line")?)?),
        ("line", json_value(&location.getattr("line")?)?),
        (
            "path",
            JsonValue::String(location.getattr("path")?.str()?.to_string()),
        ),
    ]))
}

/// `json.dumps(payload, indent=2, sort_keys=True) + "\n"` of one structured diagnostic.
fn render_diagnostic_json_text(diagnostic: &Bound<'_, PyAny>) -> PyResult<String> {
    let value = |name: &str| -> PyResult<JsonValue> { json_value(&diagnostic.getattr(name)?) };
    let mut related: Vec<JsonValue> = Vec::new();
    for item in diagnostic.getattr("related_locations")?.try_iter()? {
        let item: Bound<'_, PyAny> = item?;
        related.push(object(vec![
            ("label", json_value(&item.getattr("label")?)?),
            ("location", location_payload(&item.getattr("location")?)?),
            ("message", json_value(&item.getattr("message")?)?),
        ]));
    }
    let payload: JsonValue = object(vec![
        ("code", value("code")?),
        ("help", value("help")?),
        (
            "location",
            location_payload(&diagnostic.getattr("location")?)?,
        ),
        ("message", value("message")?),
        ("phase", value("phase")?),
        ("related_locations", JsonValue::Array(related)),
        ("resource_name", value("resource_name")?),
        ("severity", value("severity")?),
    ]);
    let options: StdlibJsonOptions = StdlibJsonOptions {
        sort_keys: true,
        ..StdlibJsonOptions::new(Some(2))
    };
    let text: String = dumps(&payload, &JsonDialect::Stdlib(options))
        .map_err(|error| PyValueError::new_err(format!("{error:?}")))?;
    Ok(text + "\n")
}

/// Deterministic JSON for one structured diagnostic.
#[pyfunction]
#[pyo3(signature = (*, diagnostic))]
fn render_diagnostic_json(diagnostic: &Bound<'_, PyAny>) -> PyResult<String> {
    render_diagnostic_json_text(diagnostic)
}

/// Return the same exception with a diagnostic unless its owner already supplied one.
#[pyfunction]
#[pyo3(name = "attach_error_diagnostic", signature = (*, error, phase, code, resource_name=None, location=None))]
fn attach_error_diagnostic_py<'py>(
    py: Python<'py>,
    error: Bound<'py, PyAny>,
    phase: Bound<'py, PyAny>,
    code: &str,
    resource_name: Option<Bound<'py, PyAny>>,
    location: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let none = || py.None().into_bound(py);
    attach_error_diagnostic(
        py,
        &error,
        phase,
        code,
        resource_name.unwrap_or_else(none),
        location.unwrap_or_else(none),
    )?;
    Ok(error)
}

/// Python's `str.title()` for the command names messages use.
pub(crate) fn title(text: &str) -> String {
    let mut titled: String = String::with_capacity(text.len());
    let mut previous_cased: bool = false;
    for character in text.chars() {
        if previous_cased {
            titled.extend(character.to_lowercase());
        } else {
            titled.extend(character.to_uppercase());
        }
        previous_cased = character.is_lowercase() || character.is_uppercase();
    }
    titled
}

/// An operator-facing message for an expected warehouse failure, or `None` for any other.
pub(crate) fn render_expected_warehouse_error(
    py: Python<'_>,
    command_name: &str,
    database: &str,
    error: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    let message: String = error.str()?.to_string();
    let command: String = title(command_name);
    let is =
        |name: &str| -> PyResult<bool> { error.is_instance(&attr(py, ADAPTER_EXCEPTIONS, name)?) };
    let lines: Vec<String> = if is("AdapterTimeoutError")? {
        vec![
            format!("{command} could not complete"),
            format!("Database: {database}"),
            "Reason: the ClickHouse operation timed out.".to_owned(),
            String::new(),
            "Retry the command after checking warehouse availability.".to_owned(),
        ]
    } else if is("AdapterAuthenticationError")? {
        vec![
            format!("{command} could not start"),
            format!("Database: {database}"),
            "Reason: ClickHouse rejected the supplied credentials.".to_owned(),
            String::new(),
            "Check:".to_owned(),
            "- your ClickHouse username and password".to_owned(),
            "- any project or environment defaults used for this command".to_owned(),
        ]
    } else if is("AdapterDatabaseNotFoundError")? {
        vec![
            format!("{command} could not start"),
            format!("Database: {database}"),
            "Reason: the target ClickHouse database does not exist.".to_owned(),
            String::new(),
            "If this is a fresh environment:".to_owned(),
            "- run sqt build first".to_owned(),
            String::new(),
            "If this database should already exist:".to_owned(),
            "- verify your ClickHouse connection and database name".to_owned(),
        ]
    } else if !is("AdapterRelationNotFoundError")? {
        return Ok(None);
    } else if message.contains(METADATA_TABLE_NAME_PREFIX) {
        vec![
            format!("{command} could not start"),
            format!("Database: {database}"),
            "Reason: SQLTide metadata tables do not exist in this database yet.".to_owned(),
            String::new(),
            "If this is a fresh environment:".to_owned(),
            "- run sqt build first".to_owned(),
        ]
    } else {
        vec![
            format!("{command} could not start"),
            format!("Database: {database}"),
            format!("Reason: {message}"),
        ]
    };
    Ok(Some(lines.join("\n")))
}

/// Render one structured diagnostic for human-readable compiler and CLI output.
#[pyfunction]
#[pyo3(name = "render_diagnostic", signature = (*, diagnostic, source_by_path=None))]
fn render_diagnostic_py(
    diagnostic: &Bound<'_, PyAny>,
    source_by_path: Option<Bound<'_, PyMapping>>,
) -> PyResult<String> {
    render_diagnostic_text(
        diagnostic,
        source_by_path.as_ref().map(|sources| sources.as_any()),
    )
}

/// Structured diagnostic text for an exception that carries one, else its message.
#[pyfunction]
#[pyo3(name = "render_error")]
fn render_error_py(py: Python<'_>, error: &Bound<'_, PyAny>) -> PyResult<String> {
    render_error(py, error)
}

/// The expected-warehouse-failure message a command prints, or `None`.
#[pyfunction]
#[pyo3(name = "render_expected_warehouse_error", signature = (*, command_name, database, error))]
fn render_expected_warehouse_error_py(
    py: Python<'_>,
    command_name: &str,
    database: &str,
    error: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    render_expected_warehouse_error(py, command_name, database, error)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_diagnostic_py, module)?)?;
    module.add_function(wrap_pyfunction!(render_error_py, module)?)?;
    module.add_function(wrap_pyfunction!(render_diagnostic_json, module)?)?;
    module.add_function(wrap_pyfunction!(attach_error_diagnostic_py, module)?)?;
    module.add_function(wrap_pyfunction!(
        render_expected_warehouse_error_py,
        module
    )?)
}
