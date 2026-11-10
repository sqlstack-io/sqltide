//! Discovery of authored SQL-native tests: `tests/**.sql` files with one or more `TEST(...)`
//! blocks, each a `WITH` clause of mocks, expectations and assertions ending in `SELECT 1`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use sqltide_sqltext::python_text::{python_isspace, python_strip};
use sqltide_sqltext::sql_scan::{
    SqlTopLevelCtes, extract_top_level_ctes, skip_trivia, try_consume_keyword,
};

use crate::config::{sql_test_blocks, sql_test_header_fields};
use crate::discovery::classify_sql_test_ctes;
use crate::macros::{expand_sql_macros_with, find_macro_call_names};
use crate::model_analysis::extract_references;
use crate::model_sql::{call, chained, object};
use crate::project_discovery::parse_sql_header;
use crate::sql_scan::scan_failure;

const TEST_MODELS: &str = "sqltide.compiler.test_discovery.models";
const TEST_TYPES: &str = "sqltide.compiler.test_discovery.types";
const TEST_EXCEPTIONS: &str = "sqltide.compiler.test_discovery.exceptions";
const DISCOVERY_EXCEPTIONS: &str = "sqltide.compiler.discovery.exceptions";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const SQL_FILE_PATTERN: &str = "*.sql";
const SCANNER_CONTEXT: &str = "SQL test";
const EXPECTED_PREFIX: &str = "__expected__";
const REF_PREFIX: &str = "__ref__";
const SOURCE_PREFIX: &str = "__source__";
const MACRO_ACTUAL: &str = "__macro_actual__";
const MACRO_EXPECTED: &str = "__macro_expected__";
const MACRO_MODE: &str = "macro";
const MODEL_MODE: &str = "model";
const SELECT_KEYWORD: &str = "SELECT";
const SELECT_LITERAL: char = '1';
const STATEMENT_DELIMITER: char = ';';

/// `SqlTestParseError(message)`.
fn parse_error(py: Python<'_>, message: String) -> PyErr {
    py.import(TEST_EXCEPTIONS)
        .and_then(|module| module.getattr("SqlTestParseError"))
        .and_then(|class| class.call1((message,)))
        .map_or_else(|error| error, PyErr::from_value)
}

/// `SqlTestParseError(message)` raised while handling `handled`, `from None` or `from handled`.
fn parse_error_from(py: Python<'_>, message: String, handled: &PyErr, caused: bool) -> PyErr {
    let error: PyErr = parse_error(py, message);
    let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
    let handled: Bound<'_, PyAny> = handled.value(py).clone().into_any();
    match chained(&value, &handled, caused.then_some(&handled)) {
        Ok(()) => error,
        Err(failure) => failure,
    }
}

fn is_instance(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    error
        .value(py)
        .is_instance(&py.import(module)?.getattr(name)?)
}

fn stripped(text: &str) -> String {
    python_strip(&text.chars().collect::<Vec<char>>())
        .iter()
        .collect()
}

fn missing_header(path: &str) -> String {
    format!("SQL test '{path}' must start with a TEST() header as the first non-whitespace content")
}

/// One parsed `TEST(...)` header: its optional name and mode.
fn test_header<'py>(
    py: Python<'py>,
    file_path: &Bound<'py, PyAny>,
    path: &str,
    header: &str,
) -> PyResult<(Option<String>, String)> {
    let header: String = stripped(header);
    if header.is_empty() {
        return Ok((None, MODEL_MODE.to_owned()));
    }
    let parsed: Bound<'py, PyDict> = match parse_sql_header(py, &header) {
        Ok(parsed) => parsed.cast_into::<PyDict>()?,
        Err(error) if is_instance(py, &error, DISCOVERY_EXCEPTIONS, "ModelHeaderSyntaxError")? => {
            let message: String = format!(
                "TEST() header in '{}' uses invalid SQLBuild syntax: {}",
                file_path.str()?,
                error.value(py).str()?
            );
            return Err(parse_error_from(py, message, &error, true));
        }
        Err(error) => return Err(error),
    };
    let items: Vec<(String, Bound<'py, PyAny>)> = parsed
        .iter()
        .map(|(key, value)| Ok((key.extract()?, value)))
        .collect::<PyResult<_>>()?;
    match sql_test_header_fields(py, path, items) {
        Ok(fields) => Ok(fields),
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            let message: String = error.value(py).str()?.to_cow()?.into_owned();
            Err(parse_error_from(py, message, &error, false))
        }
        Err(error) => Err(error),
    }
}

/// The top-level CTEs of one test body, which must end in a ceremonial `SELECT 1`.
fn test_ctes(py: Python<'_>, sql: &str, path: &str) -> PyResult<Vec<(String, String)>> {
    let top_level: SqlTopLevelCtes =
        match extract_top_level_ctes(&sql.chars().collect::<Vec<char>>(), SCANNER_CONTEXT)
            .map_err(scan_failure)
        {
            Ok(top_level) => top_level,
            Err(error)
                if is_instance(
                    py,
                    &error,
                    SQL_ANALYSIS_EXCEPTIONS,
                    "SqlMissingWithClauseError",
                )? =>
            {
                let message: String = format!(
                    "SQL test '{path}' must declare mock CTEs and one __expected__<model> CTE \
                     before `SELECT 1`"
                );
                return Err(parse_error_from(py, message, &error, true));
            }
            Err(error) if is_instance(py, &error, SQL_ANALYSIS_EXCEPTIONS, "SqlAnalysisError")? => {
                let text: String = error.value(py).str()?.to_cow()?.into_owned();
                let detail: &str = text
                    .strip_prefix(&format!("{SCANNER_CONTEXT} "))
                    .unwrap_or(&text);
                let message: String = format!("SQL test '{path}' {detail}");
                return Err(parse_error_from(py, message, &error, true));
            }
            Err(error) => return Err(error),
        };
    check_ceremonial_select(py, &top_level.trailing_sql, path)?;
    Ok(top_level
        .ctes
        .into_iter()
        .map(|cte| (cte.name, cte.query))
        .collect())
}

fn check_ceremonial_select(py: Python<'_>, sql: &str, path: &str) -> PyResult<()> {
    let sql: Vec<char> = sql.chars().collect();
    let failure = || {
        parse_error(
            py,
            format!(
                "SQL test '{path}' must end with a ceremonial top-level `SELECT 1` after its CTEs"
            ),
        )
    };
    let index: usize = skip_trivia(&sql, 0).map_err(scan_failure)?;
    let Some(select_end) = try_consume_keyword(&sql, index, SELECT_KEYWORD) else {
        return Err(failure());
    };
    let index: usize = skip_trivia(&sql, select_end).map_err(scan_failure)?;
    if sql.get(index) != Some(&SELECT_LITERAL) {
        return Err(failure());
    }
    let mut index: usize = skip_trivia(&sql, index + 1).map_err(scan_failure)?;
    if sql.get(index) == Some(&STATEMENT_DELIMITER) {
        index = skip_trivia(&sql, index + 1).map_err(scan_failure)?;
    }
    if index != sql.len() {
        return Err(failure());
    }
    Ok(())
}

/// Reject macro calls outside `__macro_actual__` in an unexpanded macro-mode test.
fn check_macro_mode(py: Python<'_>, ctes: &[(String, String)], path: &str) -> PyResult<()> {
    for (name, query) in ctes {
        if name == MACRO_ACTUAL || find_macro_call_names(py, query)?.is_empty() {
            continue;
        }
        let message: String = if name == MACRO_EXPECTED {
            format!("SQL test '{path}' mode 'macro' CTE {MACRO_EXPECTED} must not call macros")
        } else {
            format!(
                "SQL test '{path}' mode 'macro' helper CTE '{name}' must not call macros; call \
                 macros only in {MACRO_ACTUAL}"
            )
        };
        return Err(parse_error(py, message));
    }
    Ok(())
}

fn roles(
    py: Python<'_>,
    ctes: &[(String, String)],
    path: &str,
    macro_mode: bool,
) -> PyResult<Vec<&'static str>> {
    let names: Vec<String> = ctes.iter().map(|(name, _)| name.clone()).collect();
    match classify_sql_test_ctes(names, macro_mode) {
        Ok(roles) => Ok(roles),
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            let message: String = format!("SQL test '{path}' {}", error.value(py).str()?);
            Err(parse_error_from(py, message, &error, false))
        }
        Err(error) => Err(error),
    }
}

/// The authored CTEs and payload of one classified test.
fn classified<'py>(
    py: Python<'py>,
    ctes: &[(String, String)],
    path: &str,
    mode: &str,
) -> PyResult<(Bound<'py, PyTuple>, Bound<'py, PyAny>)> {
    let models: Bound<'py, PyModule> = py.import(TEST_MODELS)?;
    let cte_class: Bound<'py, PyAny> = models.getattr("SqlTestCte")?;
    let cte = |(name, query): &(String, String)| -> PyResult<Bound<'py, PyAny>> {
        call(
            &cte_class,
            &[("name", object(py, name)?), ("query", object(py, query)?)],
        )
    };
    let macro_mode: bool = mode == MACRO_MODE;
    let mut authored: Vec<Bound<'py, PyAny>> = Vec::new();
    if macro_mode {
        let mut actual: Option<Bound<'py, PyAny>> = None;
        let mut expected: Option<Bound<'py, PyAny>> = None;
        for (entry, role) in ctes.iter().zip(roles(py, ctes, path, true)?) {
            match role {
                "macro_actual" => actual = Some(cte(entry)?),
                "macro_expected" => expected = Some(cte(entry)?),
                _ => authored.push(cte(entry)?),
            }
        }
        let payload: Bound<'py, PyAny> = call(
            &models.getattr("SqlTestMacroPayload")?,
            &[
                ("actual", object(py, actual)?),
                ("expected", object(py, expected)?),
            ],
        )?;
        return Ok((PyTuple::new(py, authored)?, payload));
    }
    let relation_types: Bound<'py, PyAny> =
        py.import(DISCOVERY_TYPES)?.getattr("SqlRelationType")?;
    let mock_class: Bound<'py, PyAny> = models.getattr("SqlTestMock")?;
    let mut mocks: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut expected: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut assertions: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut assertion_queries: Vec<&str> = Vec::new();
    for (entry, role) in ctes.iter().zip(roles(py, ctes, path, false)?) {
        match role {
            "expected" => expected.push(cte(entry)?),
            "assertion" => {
                assertions.push(cte(entry)?);
                assertion_queries.push(&entry.1);
            }
            _ => {
                authored.push(cte(entry)?);
                if role != "helper" {
                    let is_ref: bool = role == "ref_mock";
                    let prefix: &str = if is_ref { REF_PREFIX } else { SOURCE_PREFIX };
                    mocks.push(call(
                        &mock_class,
                        &[
                            ("cte_name", object(py, &entry.0)?),
                            (
                                "name",
                                object(py, entry.0.strip_prefix(prefix).unwrap_or(&entry.0))?,
                            ),
                            (
                                "relation_type",
                                relation_types.getattr(if is_ref { "REF" } else { "SOURCE" })?,
                            ),
                            ("query", object(py, &entry.1)?),
                        ],
                    )?);
                }
            }
        }
    }
    let ref_relation: Bound<'py, PyAny> = relation_types.getattr("REF")?;
    let mut referenced: Vec<String> = Vec::new();
    for query in assertion_queries {
        for reference in extract_references(py, query)?.try_iter()? {
            let reference: Bound<'py, PyAny> = reference?;
            if reference.getattr("relation_type")?.eq(&ref_relation)? {
                let name: String = reference.getattr("name")?.extract()?;
                if !referenced.contains(&name) {
                    referenced.push(name);
                }
            }
        }
    }
    let payload: Bound<'py, PyAny> = call(
        &models.getattr("SqlTestModelPayload")?,
        &[
            ("mocks", PyTuple::new(py, mocks)?.into_any()),
            ("expected_targets", PyTuple::new(py, expected)?.into_any()),
            ("assertions", PyTuple::new(py, assertions)?.into_any()),
            (
                "assertion_reference_names",
                PyTuple::new(py, referenced)?.into_any(),
            ),
        ],
    )?;
    Ok((PyTuple::new(py, authored)?, payload))
}

type Macros<'a, 'py> = (Option<&'a Bound<'py, PyAny>>, Option<&'a Bound<'py, PyAny>>);

/// Every `TEST(...)` block of one authored SQL test file.
fn test_file<'py>(
    file_path: &Bound<'py, PyAny>,
    contents_by_path: Option<&Bound<'py, PyAny>>,
    macros: Macros<'_, 'py>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = file_path.py();
    let path: String = file_path.str()?.to_cow()?.into_owned();
    let contents: String = match contents_by_path {
        Some(contents) => contents.get_item(file_path)?.extract()?,
        None => {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("encoding", "utf-8")?;
            file_path
                .call_method("read_text", (), Some(&keywords))?
                .extract()?
        }
    };
    let blocks = match sql_test_blocks(&contents) {
        Ok(blocks) => blocks,
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            return Err(parse_error_from(py, missing_header(&path), &error, false));
        }
        Err(error) => return Err(error),
    };
    if blocks.is_empty() {
        return Err(parse_error(py, missing_header(&path)));
    }
    let characters: Vec<char> = contents.chars().collect();
    let test_class: Bound<'py, PyAny> = py.import(TEST_MODELS)?.getattr("LoadedSqlTest")?;
    let mode_class: Bound<'py, PyAny> = py.import(TEST_TYPES)?.getattr("SqlTestMode")?;
    let mut tests: Vec<(Option<String>, Bound<'py, PyAny>)> = Vec::new();
    for (offset, (block_start, header)) in blocks.into_iter().enumerate() {
        let Some((header, raw_body, body_start)) = header else {
            return Err(parse_error(py, missing_header(&path)));
        };
        let (name, mode) = test_header(py, file_path, &path, &header)?;
        let mode_object: Bound<'py, PyAny> = mode_class.call1((&mode,))?;
        let body: String = stripped(&raw_body);
        if mode == MACRO_MODE {
            check_macro_mode(py, &test_ctes(py, &body, &path)?, &path)?;
        }
        let body: String = match macros {
            (Some(registry), Some(context)) => {
                let leading: usize = raw_body
                    .chars()
                    .take_while(|character| python_isspace(*character))
                    .count();
                let index: usize = block_start + body_start + leading;
                let before: &[char] = &characters[..index.min(characters.len())];
                let line: usize = before
                    .iter()
                    .filter(|character| **character == '\n')
                    .count()
                    + 1;
                let column: usize = before
                    .iter()
                    .rposition(|character| *character == '\n')
                    .map_or(index + 1, |newline| index - newline);
                expand_sql_macros_with(
                    &PyString::new(py, &body),
                    file_path,
                    registry,
                    context,
                    &object(py, line)?,
                    &object(py, column)?,
                )?
                .extract()?
            }
            _ => body,
        };
        if body.is_empty() {
            return Err(parse_error(
                py,
                format!("SQL test '{path}' must define mock CTEs and end with `SELECT 1`"),
            ));
        }
        let ctes: Vec<(String, String)> = test_ctes(py, &body, &path)?;
        let (authored, payload) = classified(py, &ctes, &path, &mode)?;
        let loaded: Bound<'py, PyAny> = call(
            &test_class,
            &[
                ("file_path", file_path.clone()),
                ("mode", mode_object),
                ("authored_ctes", authored.into_any()),
                ("payload", payload),
                ("name", object(py, name.clone())?),
                ("test_index", object(py, offset + 1)?),
            ],
        )?;
        tests.push((name, loaded));
    }
    if tests.len() > 1 {
        let missing: Vec<String> = tests
            .iter()
            .enumerate()
            .filter(|(_, (name, _))| name.is_none())
            .map(|(index, _)| (index + 1).to_string())
            .collect();
        if !missing.is_empty() {
            return Err(parse_error(
                py,
                format!(
                    "SQL test '{path}' contains multiple TEST blocks; every block must define a \
                     unique `name`. Missing names for blocks: {}",
                    missing.join(", ")
                ),
            ));
        }
        let mut seen: Vec<&str> = Vec::new();
        for (name, _) in &tests {
            let name: &str = name.as_deref().unwrap_or_default();
            if seen.contains(&name) {
                return Err(parse_error(
                    py,
                    format!("SQL test '{path}' defines duplicate TEST() name '{name}'"),
                ));
            }
            seen.push(name);
        }
    }
    Ok(tests.into_iter().map(|(_, loaded)| loaded).collect())
}

/// Discover and parse the SQL-native test files under a project tests root.
#[pyfunction]
#[pyo3(signature = (*, root, contents_by_path=None, macro_registry=None, macro_context=None))]
pub(crate) fn discover_sql_tests<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    contents_by_path: Option<Bound<'py, PyAny>>,
    macro_registry: Option<Bound<'py, PyAny>>,
    macro_context: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyList>> {
    if !root.call_method0("exists")?.is_truthy()? {
        return Ok(PyList::empty(py));
    }
    let contents_by_path: Option<&Bound<'py, PyAny>> = contents_by_path
        .as_ref()
        .filter(|contents| !contents.is_none());
    let macros: Macros<'_, 'py> = (
        macro_registry
            .as_ref()
            .filter(|registry| !registry.is_none()),
        macro_context.as_ref().filter(|context| !context.is_none()),
    );
    let files: Bound<'py, PyAny> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((root.call_method1("rglob", (SQL_FILE_PATTERN,))?,))?;
    let mut tests: Vec<Bound<'py, PyAny>> = Vec::new();
    for file_path in files.try_iter()? {
        tests.extend(test_file(&file_path?, contents_by_path, macros)?);
    }
    PyList::new(py, tests)
}

/// The logical model names one discovered test evaluates or asserts on.
#[pyfunction]
#[pyo3(signature = (*, loaded_test))]
pub(crate) fn sql_test_target_names<'py>(
    py: Python<'py>,
    loaded_test: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let payload: Bound<'py, PyAny> = loaded_test.getattr("payload")?;
    if !payload.is_instance(&py.import(TEST_MODELS)?.getattr("SqlTestModelPayload")?)? {
        return Ok(PyTuple::empty(py));
    }
    let mut names: Vec<Bound<'py, PyAny>> = Vec::new();
    for target in payload.getattr("expected_targets")?.try_iter()? {
        names.push(
            target?
                .getattr("name")?
                .call_method1("removeprefix", (EXPECTED_PREFIX,))?,
        );
    }
    for name in payload.getattr("assertion_reference_names")?.try_iter()? {
        names.push(name?);
    }
    let unique: Bound<'py, PyAny> = py
        .import("builtins")?
        .getattr("dict")?
        .call_method1("fromkeys", (names,))?;
    PyTuple::new(py, unique.try_iter()?.collect::<PyResult<Vec<_>>>()?)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(discover_sql_tests, module)?)?;
    module.add_function(wrap_pyfunction!(sql_test_target_names, module)?)?;
    Ok(())
}
