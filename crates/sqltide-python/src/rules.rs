//! Python bindings for configurable SQL Rules. The compiler's Python keeps only the orchestration
//! that needs compiled models; configuration, evaluation, policy and formatting run here.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_rules::bodies::{LintBody, Span, lint_bodies};
use sqltide_rules::config::{RulesConfig as Config, rules_config as load_rules_config};
use sqltide_rules::evaluation::{RuleFinding as Finding, evaluate_rules as evaluate};
use sqltide_rules::formatting::formatted_file as format_file;
use sqltide_rules::{RuleFile, RulesFailure};

use crate::sql_scan::raise;
use crate::toml::decode_error;

const RULES_EXCEPTIONS: &str = "sqltide.compiler.rules.exceptions";
const DIAGNOSTIC_MODELS: &str = "sqltide.diagnostics.models";
const DIAGNOSTIC_TYPES: &str = "sqltide.diagnostics.types";
const KEYWORDS: [(&str, &str); 3] = [
    ("model_files", "MODEL"),
    ("test_files", "TEST"),
    ("audit_files", "AUDIT"),
];

fn construct(py: Python<'_>, module: &str, class: &str, message: String) -> PyErr {
    match py
        .import(module)
        .and_then(|module| module.getattr(class))
        .and_then(|class| class.call1((message,)))
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(error) => error,
    }
}

fn failure(py: Python<'_>, error: RulesFailure) -> PyErr {
    match error {
        RulesFailure::Rules(message) => construct(py, RULES_EXCEPTIONS, "SqlRulesError", message),
        RulesFailure::Value(message) => PyValueError::new_err(message),
        RulesFailure::Scan(error) => raise(error),
        RulesFailure::Toml(error) => decode_error(py, &error),
        RulesFailure::Io { errno, path } => match py
            .import("os")
            .and_then(|os| os.getattr("strerror")?.call1((errno,)))
            .and_then(|strerror| {
                py.import("builtins")?
                    .getattr("OSError")?
                    .call1((errno, strerror, path))
            }) {
            Ok(exception) => PyErr::from_value(exception),
            Err(error) => error,
        },
    }
}

/// A Rules failure, or a Python error from reading the compiler's objects.
enum Failure {
    Rules(RulesFailure),
    Python(PyErr),
}

impl From<RulesFailure> for Failure {
    fn from(error: RulesFailure) -> Self {
        Self::Rules(error)
    }
}

impl From<PyErr> for Failure {
    fn from(error: PyErr) -> Self {
        Self::Python(error)
    }
}

impl Failure {
    fn into_py(self, py: Python<'_>) -> PyErr {
        match self {
            Self::Rules(error) => failure(py, error),
            Self::Python(error) => error,
        }
    }
}

/// The Rules configuration of one invocation.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct RulesConfig {
    config: Config,
}

#[pymethods]
impl RulesConfig {
    /// The explicit selection, or `None` when SQLClean's defaults apply.
    #[getter]
    fn select(&self) -> Option<Vec<String>> {
        self.config.select.clone()
    }

    /// The Rule codes this configuration evaluates, in catalogue order.
    fn selected_rules(&self) -> Vec<String> {
        self.config.selected_rules()
    }

    /// Each rule ignore's model selectors, empty for a path-only ignore.
    fn ignore_selectors(&self) -> Vec<Vec<String>> {
        self.config
            .rule_ignores
            .iter()
            .map(|entry| entry.selectors.clone())
            .collect()
    }

    /// The configuration with each selector ignore replaced by the sorted union of its paths and
    /// the authored files its selectors resolved to, one entry per ignore.
    fn with_selector_paths(&self, resolved: Vec<Vec<String>>) -> PyResult<Self> {
        let mut config = self.config.clone();
        if resolved.len() != config.rule_ignores.len() {
            return Err(PyValueError::new_err(
                "Resolved selector paths must cover every rule ignore",
            ));
        }
        for (entry, paths) in config.rule_ignores.iter_mut().zip(resolved) {
            if entry.selectors.is_empty() {
                continue;
            }
            entry.paths.extend(paths);
            entry.paths.sort_unstable();
            entry.paths.dedup();
            entry.selectors.clear();
        }
        Ok(Self { config })
    }
}

/// One Rule diagnostic and an optional authored-model edit.
#[pyclass(module = "sqltide._native", frozen, get_all)]
pub(crate) struct RuleFinding {
    diagnostic: Py<PyAny>,
    path: Py<PyAny>,
    start: i64,
    end: i64,
    replacement: Option<String>,
    fix_start: Option<i64>,
    fix_end: Option<i64>,
}

/// The authored files of a discovery, with their `Path` objects.
struct Files<'py> {
    files: Vec<RuleFile>,
    paths: Vec<Bound<'py, PyAny>>,
}

impl<'py> Files<'py> {
    fn new() -> Self {
        Self {
            files: Vec::new(),
            paths: Vec::new(),
        }
    }

    fn push(
        &mut self,
        source: &Bound<'py, PyAny>,
        project_dir: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<usize> {
        let path = source.getattr("file_path")?;
        let contents: String = source.getattr("contents")?.extract()?;
        let relative_path = match project_dir {
            None => Err(String::new()),
            Some(project_dir) => match path.call_method1("relative_to", (project_dir,)) {
                Ok(relative) => Ok(relative.call_method0("as_posix")?.extract()?),
                Err(error) if error.is_instance_of::<PyValueError>(source.py()) => {
                    Err(error.value(source.py()).str()?.extract()?)
                }
                Err(error) => return Err(error),
            },
        };
        self.files.push(RuleFile {
            path: path.str()?.extract()?,
            contents: contents.chars().collect(),
            relative_path,
        });
        self.paths.push(path);
        Ok(self.files.len() - 1)
    }
}

fn config_of(config: &Bound<'_, RulesConfig>) -> Config {
    config.get().config.clone()
}

/// The invocation's Rules configuration from the project's retained `sqltide_project.toml` text,
/// with `selection` overriding `rules.select`.
#[pyfunction]
#[pyo3(signature = (*, loaded_project, selection=None))]
fn rules_config(
    py: Python<'_>,
    loaded_project: &Bound<'_, PyAny>,
    selection: Option<Vec<String>>,
) -> PyResult<RulesConfig> {
    let (contents, project_dir): (Option<String>, String) = if loaded_project.is_none() {
        (
            None,
            py.import("pathlib")?
                .getattr("Path")?
                .call_method0("cwd")?
                .str()?
                .extract()?,
        )
    } else {
        let source_file = loaded_project.getattr("source_file")?;
        (
            Some(source_file.getattr("contents")?.extract()?),
            source_file
                .getattr("file_path")?
                .getattr("parent")?
                .str()?
                .extract()?,
        )
    };
    load_rules_config(contents.as_deref(), &project_dir, selection.as_deref())
        .map(|config| RulesConfig { config })
        .map_err(|error| failure(py, error))
}

fn rule_finding<'py>(
    py: Python<'py>,
    files: &Files<'py>,
    finding: Finding,
) -> PyResult<RuleFinding> {
    let path = &files.paths[finding.file];
    let models = py.import(DIAGNOSTIC_MODELS)?;
    let types = py.import(DIAGNOSTIC_TYPES)?;
    let location_arguments = PyDict::new(py);
    location_arguments.set_item("path", path)?;
    location_arguments.set_item("line", finding.line)?;
    location_arguments.set_item("column", finding.column)?;
    location_arguments.set_item("end_line", finding.end_line)?;
    location_arguments.set_item("end_column", finding.end_column)?;
    let location = models
        .getattr("SourceLocation")?
        .call((), Some(&location_arguments))?;
    let arguments = PyDict::new(py);
    arguments.set_item(
        "phase",
        types.getattr("DiagnosticPhase")?.getattr("COMPILATION")?,
    )?;
    arguments.set_item(
        "severity",
        types.getattr("DiagnosticSeverity")?.getattr("ERROR")?,
    )?;
    arguments.set_item("code", finding.code)?;
    arguments.set_item("message", finding.message)?;
    arguments.set_item("resource_name", path.getattr("stem")?)?;
    arguments.set_item("location", location)?;
    arguments.set_item("help", finding.remediation)?;
    let diagnostic = models
        .getattr("CompilerDiagnostic")?
        .call((), Some(&arguments))?;
    Ok(RuleFinding {
        diagnostic: diagnostic.unbind(),
        path: path.clone().unbind(),
        start: finding.start,
        end: finding.end,
        replacement: finding.replacement,
        fix_start: finding.fix_start,
        fix_end: finding.fix_end,
    })
}

fn expansion(
    expansions: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
    body: &LintBody,
    line: i64,
    column: i64,
) -> PyResult<Option<(Vec<char>, Vec<Span>)>> {
    let sql: String = body.sql.iter().collect();
    let key = PyTuple::new(
        expansions.py(),
        [
            path.clone(),
            sql.into_pyobject(expansions.py())?.into_any(),
            line.into_pyobject(expansions.py())?.into_any(),
            column.into_pyobject(expansions.py())?.into_any(),
        ],
    )?;
    let found = expansions.call_method1("get", (key,))?;
    if found.is_none() {
        return Ok(None);
    }
    let (expanded, spans): (String, Vec<Span>) = found.extract()?;
    Ok(Some((expanded.chars().collect(), spans)))
}

/// Every Rule finding for the compile's authored bodies, or only those in `only_path`. Bodies
/// reuse this compile's macro expansions; macros are never called again.
#[pyfunction]
#[pyo3(signature = (*, inputs, config, dialect, project_dir, only_path=None, check_stale=true))]
fn evaluate_rules<'py>(
    py: Python<'py>,
    inputs: &Bound<'py, PyAny>,
    config: &Bound<'py, RulesConfig>,
    dialect: &str,
    project_dir: &Bound<'py, PyAny>,
    only_path: Option<&Bound<'py, PyAny>>,
    check_stale: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let discovered = inputs.getattr("discovered_inputs")?;
    let expansions = inputs.getattr("macro_registry")?.getattr("expansions")?;
    let mut files = Files::new();
    let mut order: Vec<(usize, &str)> = Vec::new();
    for (attribute, keyword) in KEYWORDS {
        for source in discovered.getattr(attribute)?.try_iter()? {
            order.push((files.push(&source?, Some(project_dir))?, keyword));
        }
    }
    let bodies = lint_bodies(
        &files.files,
        &order,
        |body, line, column| -> Result<_, Failure> {
            Ok(expansion(
                &expansions,
                &files.paths[body.file],
                body,
                line,
                column,
            )?)
        },
    )
    .map_err(|error| error.into_py(py))?;
    let bodies: Vec<LintBody> = match only_path {
        None => bodies,
        Some(only) => {
            let mut kept = Vec::new();
            for body in bodies {
                if files.paths[body.file].eq(only)? {
                    kept.push(body);
                }
            }
            kept
        }
    };
    let findings = evaluate(
        &files.files,
        &bodies,
        &config_of(config),
        dialect,
        check_stale,
    )
    .map_err(|error| failure(py, error))?;
    let findings = findings
        .into_iter()
        .map(|finding| rule_finding(py, &files, finding))
        .collect::<PyResult<Vec<_>>>()?;
    PyTuple::new(py, findings)
}

/// The source with only its SQL bodies formatted; headers and surrounding whitespace keep their
/// exact text.
#[pyfunction]
#[pyo3(signature = (*, source, keyword, dialect))]
fn formatted_file(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    keyword: &str,
    dialect: &str,
) -> PyResult<String> {
    let mut files = Files::new();
    let file = files.push(source, None)?;
    format_file(&files.files, file, keyword, dialect).map_err(|error| failure(py, error))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RulesConfig>()?;
    module.add_class::<RuleFinding>()?;
    module.add_function(wrap_pyfunction!(rules_config, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_rules, module)?)?;
    module.add_function(wrap_pyfunction!(formatted_file, module)?)
}
