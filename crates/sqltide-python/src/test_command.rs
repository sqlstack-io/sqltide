//! `sqt test`: analyze the project, select SQL tests by target selectors and paths, run them
//! through the Python test executor, publish the executed SQL under `run/tests`, render the
//! results and record the invocation and per-test node results. The executor, observability
//! records, CLI styling and filesystem operations stay in Python and are looked up when called.

use std::collections::{BTreeMap, HashMap, HashSet};

use pyo3::exceptions::{PyException, PyIndexError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PySlice, PyString};

use crate::build_cli::formatted;
use crate::build_command::{int, invocation_record, print};
use crate::compile_command::{make_directories, remove_tree};
use crate::direct_build::{Value, boolean, build, call, class, items, method, none, text, tuple};
use crate::handling::while_handling;

const NATIVE: &str = "sqltide._native";
const CLI_STYLE: &str = "sqltide._native";
const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";
const OBSERVABILITY_MODELS: &str = "sqltide.executor.observability.models";
const OBSERVABILITY_TYPES: &str = "sqltide.executor.observability.types";
const START_INVOCATION: &str = "sqltide._native";
const NODE_RESULT_RECORD: &str = "sqltide._native";
const TERMINAL_OBSERVATIONS: &str = "sqltide._native";
const EXECUTE_SQL_TESTS: &str = "sqltide._native";
const UPSTREAM_SELECTOR_PREFIX: &str = "+";
const DOWNSTREAM_SELECTOR_SUFFIX: &str = "+";
const SELECTOR_NAMESPACE_SEPARATOR: &str = ":";
const PIPELINE_SELECTOR_NAMESPACE: &str = "pipeline";
const MAX_RENDERED_ROWS: usize = 10;
const MAX_PAYLOAD_ROWS: isize = 5;
const RUNTIME_TARGET_OWNER: &str = "run";
const RUNTIME_TESTS_OWNER: &str = "tests";
const RUNTIME_STAGING_PREFIX: &str = ".sqltide-run-tests-";
const DEFAULT_TARGET_DIRECTORY_NAME: &str = "target";
const ANSI_CODES: [&str; 7] = [
    "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[34m",
];

pub(crate) fn user_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        class(py, CLI_EXCEPTIONS, "CliUserError")?.call1((message,))?,
    ))
}

pub(crate) fn is_none_attribute(value: &Value<'_>, name: &str) -> PyResult<bool> {
    Ok(value.getattr(name)?.is_none())
}

/// The next pair of `zip(first, second, strict=True)`, or `None` once both are exhausted.
pub(crate) fn strict_pair<'py>(
    first: &mut Bound<'py, pyo3::types::PyIterator>,
    second: &mut Bound<'py, pyo3::types::PyIterator>,
) -> PyResult<Option<(Value<'py>, Value<'py>)>> {
    match first.next().transpose()? {
        Some(left) => match second.next().transpose()? {
            Some(right) => Ok(Some((left, right))),
            None => Err(PyValueError::new_err(
                "zip() argument 2 is shorter than argument 1",
            )),
        },
        None => match second.next().transpose()? {
            Some(_) => Err(PyValueError::new_err(
                "zip() argument 2 is longer than argument 1",
            )),
            None => Ok(None),
        },
    }
}

// ---------------------------------------------------------------------------------------------
// Selection

/// Filter discovered SQL tests by target selectors and explicit paths.
#[pyfunction]
#[pyo3(signature = (*, loaded_tests, compiled_pipelines, selectors, paths, project_dir))]
fn select_loaded_sql_tests<'py>(
    py: Python<'py>,
    loaded_tests: &Value<'py>,
    compiled_pipelines: &Value<'py>,
    selectors: &Value<'py>,
    paths: &Value<'py>,
    project_dir: &Value<'py>,
) -> PyResult<Value<'py>> {
    if !selectors.is_truthy()? && !paths.is_truthy()? {
        return Ok(loaded_tests.clone());
    }
    let selected_paths: Bound<'py, PySet> = selected_paths(py, paths, project_dir)?;
    let selected_target_names: HashSet<String> =
        selected_target_names(py, compiled_pipelines, selectors)?;
    let mut selected: Vec<Value<'py>> = Vec::new();
    for loaded_test in items(loaded_tests)? {
        let resolved: Value<'py> = loaded_test.getattr("file_path")?.call_method0("resolve")?;
        if selected_paths.contains(resolved)?
            || targets_selected(py, &loaded_test, &selected_target_names)?
        {
            selected.push(loaded_test);
        }
    }
    if selected.is_empty() {
        return Err(user_error(
            py,
            "No SQL tests matched the requested selectors or paths.".to_owned(),
        )?);
    }
    tuple(py, selected)
}

fn selected_paths<'py>(
    py: Python<'py>,
    paths: &Value<'py>,
    project_dir: &Value<'py>,
) -> PyResult<Bound<'py, PySet>> {
    let resolved_paths: Bound<'py, PySet> = PySet::empty(py)?;
    for raw_path in items(paths)? {
        let resolved_path: Value<'py> = if raw_path.call_method0("is_absolute")?.is_truthy()? {
            raw_path
        } else {
            project_dir.div(&raw_path)?
        };
        if resolved_path.call_method0("is_dir")?.is_truthy()? {
            for path in items(&resolved_path.call_method1("rglob", ("*.sql",))?)? {
                resolved_paths.add(path.call_method0("resolve")?)?;
            }
            continue;
        }
        resolved_paths.add(resolved_path.call_method0("resolve")?)?;
    }
    Ok(resolved_paths)
}

/// Model names grouped by name: `(upstream, downstream)` neighbours of every compiled model.
pub(crate) type ModelGraph = (HashMap<String, Vec<String>>, HashMap<String, Vec<String>>);

pub(crate) fn model_names(pipeline: &Value<'_>) -> PyResult<Vec<String>> {
    items(&pipeline.getattr("models")?)?
        .iter()
        .map(|model| model.getattr("key")?.getattr("name")?.extract())
        .collect()
}

pub(crate) fn model_graph(compiled_pipelines: &[Value<'_>]) -> PyResult<ModelGraph> {
    let mut known: HashSet<String> = HashSet::new();
    for pipeline in compiled_pipelines {
        known.extend(model_names(pipeline)?);
    }
    let mut upstream: HashMap<String, Vec<String>> = HashMap::new();
    let mut downstream: HashMap<String, Vec<String>> = HashMap::new();
    for pipeline in compiled_pipelines {
        for model in items(&pipeline.getattr("models")?)? {
            let name: String = model.getattr("key")?.getattr("name")?.extract()?;
            upstream.entry(name.clone()).or_default();
            downstream.entry(name.clone()).or_default();
            for parsed_ref in items(&model.getattr("parsed_refs")?)? {
                let reference: String = parsed_ref.getattr("name")?.extract()?;
                if !known.contains(&reference) {
                    continue;
                }
                upstream
                    .entry(name.clone())
                    .or_default()
                    .push(reference.clone());
                downstream.entry(reference).or_default().push(name.clone());
            }
        }
    }
    Ok((upstream, downstream))
}

fn selected_target_names(
    py: Python<'_>,
    compiled_pipelines: &Value<'_>,
    selectors: &Value<'_>,
) -> PyResult<HashSet<String>> {
    let mut selected: HashSet<String> = HashSet::new();
    if !selectors.is_truthy()? {
        return Ok(selected);
    }
    let pipelines: Vec<Value<'_>> = items(compiled_pipelines)?;
    let mut pipeline_model_names: HashMap<String, Vec<String>> = HashMap::new();
    for pipeline in &pipelines {
        let name: String = pipeline.getattr("pipeline")?.getattr("name")?.extract()?;
        pipeline_model_names.insert(name, model_names(pipeline)?);
    }
    let (upstream, downstream): ModelGraph = model_graph(&pipelines)?;
    for raw_selector in items(selectors)? {
        let raw_selector: String = raw_selector.extract()?;
        let include_upstream: bool = raw_selector.starts_with(UPSTREAM_SELECTOR_PREFIX);
        let include_downstream: bool = raw_selector.ends_with(DOWNSTREAM_SELECTOR_SUFFIX);
        let unprefixed: &str = raw_selector
            .strip_prefix(UPSTREAM_SELECTOR_PREFIX)
            .unwrap_or(&raw_selector);
        let selector: &str = unprefixed
            .strip_suffix(DOWNSTREAM_SELECTOR_SUFFIX)
            .unwrap_or(unprefixed);
        if selector.is_empty() || selector.contains(UPSTREAM_SELECTOR_PREFIX) {
            return Err(user_error(
                py,
                format!(
                    "Unsupported test selector syntax '{raw_selector}'. Use bare model names, \
                     pipeline:<name>, or optional leading/trailing '+'."
                ),
            )?);
        }
        let base_names: Vec<String> =
            selector_target_names(py, "test", selector, &pipeline_model_names, &downstream)?;
        selected.extend(base_names.iter().cloned());
        if include_upstream {
            for name in &base_names {
                selected.extend(neighbours(name, &upstream));
            }
        }
        if include_downstream {
            for name in &base_names {
                selected.extend(neighbours(name, &downstream));
            }
        }
    }
    Ok(selected)
}

/// The models one `pipeline:<name>` or bare model selector names, with errors naming the
/// selector's `noun` ("test" or "audit").
pub(crate) fn selector_target_names(
    py: Python<'_>,
    noun: &str,
    selector: &str,
    pipeline_model_names: &HashMap<String, Vec<String>>,
    known_models: &HashMap<String, Vec<String>>,
) -> PyResult<Vec<String>> {
    let Some((kind, value)) = selector.split_once(SELECTOR_NAMESPACE_SEPARATOR) else {
        if !known_models.contains_key(selector) {
            return Err(user_error(
                py,
                format!("Unknown SQL {noun} selector model '{selector}'"),
            )?);
        }
        return Ok(vec![selector.to_owned()]);
    };
    if kind != PIPELINE_SELECTOR_NAMESPACE {
        return Err(user_error(
            py,
            format!("Unsupported {noun} selector namespace '{kind}' in '{selector}'"),
        )?);
    }
    let Some(targets) = pipeline_model_names.get(value) else {
        return Err(user_error(
            py,
            format!("Unknown SQL {noun} selector pipeline '{value}'"),
        )?);
    };
    if targets.is_empty() {
        return Err(user_error(
            py,
            format!("SQL {noun} selector pipeline '{value}' does not define any models"),
        )?);
    }
    Ok(targets.clone())
}

/// Every model reachable from `start` along `neighbours`, excluding `start` unless a cycle
/// returns to it.
pub(crate) fn neighbours(start: &str, by_name: &HashMap<String, Vec<String>>) -> HashSet<String> {
    let mut visited: HashSet<String> = HashSet::new();
    let mut stack: Vec<&str> = vec![start];
    while let Some(current) = stack.pop() {
        for neighbour in by_name.get(current).into_iter().flatten() {
            if visited.insert(neighbour.clone()) {
                stack.push(neighbour);
            }
        }
    }
    visited
}

fn targets_selected(
    py: Python<'_>,
    loaded_test: &Value<'_>,
    selected_target_names: &HashSet<String>,
) -> PyResult<bool> {
    let target_names: Vec<String> = items(&call(
        py,
        NATIVE,
        "sql_test_target_names",
        &[("loaded_test", loaded_test.clone())],
    )?)?
    .iter()
    .map(|name| name.extract())
    .collect::<PyResult<_>>()?;
    Ok(target_names
        .iter()
        .any(|name| selected_target_names.contains(name)))
}

// ---------------------------------------------------------------------------------------------
// Rendering

/// `cli_style().<name>(text)`, resolving colour support for every styled value.
pub(crate) fn styled(py: Python<'_>, name: &str, value: &str) -> PyResult<String> {
    py.import(CLI_STYLE)?
        .getattr("cli_style")?
        .call0()?
        .call_method1(name, (value,))?
        .extract()
}

fn outcome_styled(py: Python<'_>, value: &str, passed: Value<'_>) -> PyResult<String> {
    let style: Value<'_> = py.import(CLI_STYLE)?.getattr("cli_style")?.call0()?;
    method(
        &style,
        "outcome",
        &[("text", text(py, value)), ("passed", passed)],
    )?
    .extract()
}

fn strip_ansi(value: &str) -> String {
    ANSI_CODES.iter().fold(value.to_owned(), |stripped, code| {
        stripped.replace(code, "")
    })
}

fn visible_width(value: &str) -> usize {
    strip_ansi(value).chars().count()
}

/// `", ".join(values)` with Python's own type checks.
pub(crate) fn joined(py: Python<'_>, values: &Value<'_>) -> PyResult<String> {
    PyString::new(py, ", ")
        .call_method1("join", (values,))?
        .extract()
}

fn stringify(value: &Value<'_>) -> PyResult<String> {
    if value.is_none() {
        return Ok("NULL".to_owned());
    }
    value.str()?.extract()
}

/// One rendered diff section.
struct Section {
    title: &'static str,
    headers: Vec<String>,
    rows: Vec<Vec<String>>,
    paired_row_count: usize,
}

/// Render SQL-native test execution results for CLI output.
#[pyfunction]
#[pyo3(signature = (*, results, project_dir, verbose))]
fn render_sql_test_results(
    py: Python<'_>,
    results: &Value<'_>,
    project_dir: &Value<'_>,
    verbose: &Value<'_>,
) -> PyResult<String> {
    render(py, results, project_dir, verbose.is_truthy()?)
}

fn render(
    py: Python<'_>,
    results: &Value<'_>,
    project_dir: &Value<'_>,
    verbose: bool,
) -> PyResult<String> {
    let results: Vec<Value<'_>> = items(results)?;
    let mut passed: Vec<bool> = Vec::with_capacity(results.len());
    for result in &results {
        passed.push(result.getattr("passed")?.is_truthy()?);
    }
    let passed_count: usize = passed.iter().filter(|value| **value).count();
    let mut error_count: usize = 0;
    for result in &results {
        if !is_none_attribute(result, "error_message")? {
            error_count += 1;
        }
    }
    let failed_count: i64 = results.len() as i64 - passed_count as i64 - error_count as i64;
    let mut lines: Vec<String> = Vec::new();
    let mut failed_paths: Vec<Value<'_>> = Vec::new();
    for (index, result) in results.iter().enumerate() {
        if passed[index] && index > 0 && !passed[index - 1] {
            lines.push(String::new());
        }
        if !passed[index] && !lines.is_empty() {
            lines.push(String::new());
        }
        let file_path: Value<'_> = result.getattr("file_path")?;
        let label: String = result_label(
            &file_path,
            &result.getattr("name")?,
            &result.getattr("test_index")?,
            project_dir,
        )?;
        let has_error: bool = !is_none_attribute(result, "error_message")?;
        let status_text: &str = if has_error {
            "ERROR"
        } else if passed[index] {
            "PASS"
        } else {
            "FAIL"
        };
        let status: String = outcome_styled(py, status_text, result.getattr("passed")?)?;
        lines.push(format!("{status}  {}  {label}", target_label(py, result)?));
        for warning in items(&result.getattr("warnings")?)? {
            lines.push(format!("  warning: {}", formatted(&warning)?));
        }
        if passed[index] {
            continue;
        }
        let mut known: bool = false;
        for path in &failed_paths {
            if path.is(&file_path) || path.eq(&file_path)? {
                known = true;
                break;
            }
        }
        if !known {
            failed_paths.push(file_path);
        }
        lines.extend(failed_result(py, result, verbose)?);
        if index + 1 < results.len() {
            lines.push(String::new());
        }
    }
    let mut summary: String = format!("Results: {passed_count} passed, {failed_count} failed");
    if error_count > 0 {
        let error_label: &str = if error_count == 1 { "error" } else { "errors" };
        summary.push_str(&format!(", {error_count} {error_label}"));
    }
    lines.push(outcome_styled(
        py,
        &summary,
        boolean(py, !(failed_count > 0 || error_count > 0))?,
    )?);
    if !failed_paths.is_empty() {
        lines.push("Failed:".to_owned());
        for path in &failed_paths {
            lines.push(format!("  sqt test {}", result_path(path, project_dir)?));
        }
    }
    Ok(lines.join("\n"))
}

fn failed_result(py: Python<'_>, result: &Value<'_>, verbose: bool) -> PyResult<Vec<String>> {
    let error_message: Value<'_> = result.getattr("error_message")?;
    if !error_message.is_none() {
        return Ok(vec![format!("  error: {}", formatted(&error_message)?)]);
    }
    let target_results: Value<'_> = result.getattr("target_results")?;
    if target_results.len()? > 1 {
        let mut lines: Vec<String> = Vec::new();
        let mut first_rendered: bool = false;
        for target in items(&target_results)? {
            if target.getattr("passed")?.is_truthy()? {
                continue;
            }
            if first_rendered {
                lines.push(String::new());
            }
            lines.push(format!(
                "  target: {}",
                formatted(&target.getattr("target_model_name")?)?
            ));
            lines.extend(target_failed_result(py, &target, verbose)?);
            first_rendered = true;
        }
        return Ok(lines);
    }
    target_failed_result(py, &target_results.get_item(0)?, verbose)
}

fn remaining_section(
    title: &'static str,
    headers: &[String],
    rows: &[Value<'_>],
) -> PyResult<Section> {
    let mut rendered: Vec<Vec<String>> = Vec::with_capacity(rows.len());
    for row in rows {
        rendered.push(items(row)?.iter().map(stringify).collect::<PyResult<_>>()?);
    }
    Ok(Section {
        title,
        headers: headers.to_vec(),
        rows: rendered,
        paired_row_count: rows.len(),
    })
}

fn target_failed_result(
    py: Python<'_>,
    result: &Value<'_>,
    verbose: bool,
) -> PyResult<Vec<String>> {
    let (sections, remaining_missing, remaining_unexpected) = paired_sections(py, result)?;
    let column_names: Value<'_> = result.getattr("column_names")?;
    let mut lines: Vec<String> = Vec::new();
    if sections.is_empty() {
        lines.push(format!(
            "  diff summary: {} missing, {} unexpected",
            result.getattr("missing_rows")?.len()?,
            result.getattr("unexpected_rows")?.len()?
        ));
        lines.push(format!("  columns: {}", joined(py, &column_names)?));
    } else {
        let differing: usize = sections
            .iter()
            .map(|section| section.paired_row_count)
            .sum();
        let row_label: &str = if differing == 1 {
            "row differs"
        } else {
            "rows differ"
        };
        lines.push(format!("  diff ({differing} {row_label}):"));
        lines.push(format!("  columns: {}", joined(py, &column_names)?));
        for section in &sections {
            lines.extend(table_section(py, section, "  ", verbose)?);
        }
    }
    let headers: Vec<String> = items(&column_names)?
        .iter()
        .map(|name| name.extract())
        .collect::<PyResult<_>>()?;
    if !remaining_missing.is_empty() {
        let section: Section = remaining_section("missing rows", &headers, &remaining_missing)?;
        lines.extend(table_section(py, &section, "  ", verbose)?);
    }
    if !remaining_unexpected.is_empty() {
        let section: Section =
            remaining_section("unexpected rows", &headers, &remaining_unexpected)?;
        lines.extend(table_section(py, &section, "  ", verbose)?);
    }
    Ok(lines)
}

/// Column indexes where the two rows compare equal (`==`) or differ (`!=`), pairing their values
/// as `zip(missing, unexpected, strict=True)` does.
fn compared_indexes(
    missing: &Value<'_>,
    unexpected: &Value<'_>,
    equal: bool,
) -> PyResult<Vec<usize>> {
    let mut first = missing.try_iter()?;
    let mut second = unexpected.try_iter()?;
    let mut indexes: Vec<usize> = Vec::new();
    let mut index: usize = 0;
    while let Some((left, right)) = strict_pair(&mut first, &mut second)? {
        let matches: bool = if equal {
            left.eq(&right)?
        } else {
            left.ne(&right)?
        };
        if matches {
            indexes.push(index);
        }
        index += 1;
    }
    Ok(indexes)
}

type Pairs<'py> = Vec<(Value<'py>, Value<'py>)>;
type PairedSections<'py> = (Vec<Section>, Vec<Value<'py>>, Vec<Value<'py>>);

/// Pair each missing row with the unmatched unexpected row sharing the most equal columns, group
/// the pairs by which columns match and differ, and return the rows left unpaired.
fn paired_sections<'py>(py: Python<'py>, result: &Value<'py>) -> PyResult<PairedSections<'py>> {
    let missing_rows: Vec<Value<'py>> = items(&result.getattr("missing_rows")?)?;
    let unexpected_source: Value<'py> = result.getattr("unexpected_rows")?;
    let unexpected_rows: Vec<Value<'py>> = items(&unexpected_source)?;
    let mut groups: BTreeMap<(Vec<usize>, Vec<usize>), Pairs<'py>> = BTreeMap::new();
    let mut matched_missing: HashSet<usize> = HashSet::new();
    let mut matched_unexpected: HashSet<usize> = HashSet::new();
    for (missing_index, missing_row) in items(&result.getattr("missing_rows")?)?
        .into_iter()
        .enumerate()
    {
        let mut best: Option<(usize, Vec<usize>, Vec<usize>)> = None;
        let mut best_score: i64 = -1;
        for (unexpected_index, unexpected_row) in items(&result.getattr("unexpected_rows")?)?
            .into_iter()
            .enumerate()
        {
            if matched_unexpected.contains(&unexpected_index) {
                continue;
            }
            let key_indexes: Vec<usize> = compared_indexes(&missing_row, &unexpected_row, true)?;
            let diff_indexes: Vec<usize> = compared_indexes(&missing_row, &unexpected_row, false)?;
            if key_indexes.is_empty() || diff_indexes.is_empty() {
                continue;
            }
            if key_indexes.len() as i64 > best_score {
                best_score = key_indexes.len() as i64;
                best = Some((unexpected_index, key_indexes, diff_indexes));
            }
        }
        let Some((best_index, key_indexes, diff_indexes)) = best else {
            continue;
        };
        matched_missing.insert(missing_index);
        matched_unexpected.insert(best_index);
        groups
            .entry((key_indexes, diff_indexes))
            .or_default()
            .push((missing_row, unexpected_source.get_item(best_index)?));
    }
    let column_names: Value<'py> = result.getattr("column_names")?;
    let column_count: usize = column_names.len()?;
    let mut sections: Vec<Section> = Vec::new();
    let mut paired_row_index: usize = 1;
    for ((key_indexes, diff_indexes), pairs) in &groups {
        let mut rows: Vec<Vec<String>> = Vec::new();
        for (missing_row, unexpected_row) in pairs {
            let mut expected: Vec<String> = vec![
                paired_row_index.to_string(),
                styled(py, "passed", "expected")?,
            ];
            let mut actual: Vec<String> = vec![
                paired_row_index.to_string(),
                styled(py, "failed", "actual")?,
            ];
            for column_index in 0..column_count {
                let expected_value: String = stringify(&missing_row.get_item(column_index)?)?;
                let actual_value: String = stringify(&unexpected_row.get_item(column_index)?)?;
                if diff_indexes.contains(&column_index) {
                    expected.push(styled(py, "passed", &expected_value)?);
                    actual.push(styled(py, "failed", &actual_value)?);
                    continue;
                }
                if key_indexes.contains(&column_index) {
                    expected.push(styled(py, "muted", &expected_value)?);
                    actual.push(styled(py, "muted", &actual_value)?);
                    continue;
                }
                expected.push(expected_value);
                actual.push(actual_value);
            }
            rows.push(expected);
            rows.push(actual);
            paired_row_index += 1;
        }
        let mut headers: Vec<String> = vec!["row".to_owned(), "state".to_owned()];
        for name in items(&column_names)? {
            headers.push(name.extract()?);
        }
        sections.push(Section {
            title: "",
            headers,
            rows,
            paired_row_count: pairs.len(),
        });
    }
    let remaining_missing: Vec<Value<'py>> = missing_rows
        .into_iter()
        .enumerate()
        .filter(|(index, _)| !matched_missing.contains(index))
        .map(|(_, row)| row)
        .collect();
    let remaining_unexpected: Vec<Value<'py>> = unexpected_rows
        .into_iter()
        .enumerate()
        .filter(|(index, _)| !matched_unexpected.contains(index))
        .map(|(_, row)| row)
        .collect();
    Ok((sections, remaining_missing, remaining_unexpected))
}

fn table_section(
    py: Python<'_>,
    section: &Section,
    indent: &str,
    verbose: bool,
) -> PyResult<Vec<String>> {
    let (rendered_rows, hidden_count): (&[Vec<String>], usize) =
        if verbose || section.rows.len() <= MAX_RENDERED_ROWS {
            (&section.rows, 0)
        } else {
            (
                &section.rows[..MAX_RENDERED_ROWS],
                section.rows.len() - MAX_RENDERED_ROWS,
            )
        };
    let mut lines: Vec<String> = Vec::new();
    if !section.title.is_empty() {
        let suffix: String = if hidden_count > 0 {
            format!(
                " ({}, showing first {})",
                section.rows.len(),
                rendered_rows.len()
            )
        } else {
            format!(" ({})", section.rows.len())
        };
        lines.push(format!("{indent}{}{suffix}:", section.title));
    }
    let mut widths: Vec<usize> = section
        .headers
        .iter()
        .map(|header| visible_width(header))
        .collect();
    for row in rendered_rows {
        for (index, cell) in row.iter().enumerate() {
            let Some(width) = widths.get_mut(index) else {
                return Err(PyIndexError::new_err("list index out of range"));
            };
            *width = (*width).max(visible_width(cell));
        }
    }
    lines.push(format!(
        "{indent}  {}",
        table_row(py, &section.headers, &widths, true)?
    ));
    for row in rendered_rows {
        lines.push(format!("{indent}  {}", table_row(py, row, &widths, false)?));
    }
    if hidden_count > 0 {
        let message: String = styled(
            py,
            "muted",
            &format!("({hidden_count} more rows not shown, run with --verbose to see all)"),
        )?;
        lines.push(format!("{indent}  {message}"));
    }
    Ok(lines)
}

fn table_row(py: Python<'_>, row: &[String], widths: &[usize], header: bool) -> PyResult<String> {
    let mut cells: Vec<String> = Vec::with_capacity(row.len());
    for (index, cell) in row.iter().enumerate() {
        let padding: usize = widths[index].saturating_sub(visible_width(cell));
        let padded: String = format!("{cell}{}", " ".repeat(padding));
        cells.push(if header {
            styled(py, "muted_strong", &padded)?
        } else {
            padded
        });
    }
    Ok(cells.join("  "))
}

/// The project-relative POSIX path of a test file, or the path itself outside the project.
fn result_path(file_path: &Value<'_>, project_dir: &Value<'_>) -> PyResult<String> {
    let py: Python<'_> = file_path.py();
    match file_path.call_method1("relative_to", (project_dir,)) {
        Ok(relative) => relative.call_method0("as_posix")?.extract(),
        Err(error) if error.is_instance_of::<PyValueError>(py) => file_path.str()?.extract(),
        Err(error) => Err(error),
    }
}

fn result_label(
    file_path: &Value<'_>,
    name: &Value<'_>,
    test_index: &Value<'_>,
    project_dir: &Value<'_>,
) -> PyResult<String> {
    let relative_path: String = result_path(file_path, project_dir)?;
    if !name.is_none() {
        return Ok(format!("{relative_path}  [{}]", formatted(name)?));
    }
    if test_index.eq(1)? {
        return Ok(relative_path);
    }
    Ok(format!("{relative_path}::{}", formatted(test_index)?))
}

fn target_label(py: Python<'_>, result: &Value<'_>) -> PyResult<String> {
    if !is_none_attribute(result, "error_message")? {
        return Ok("execution".to_owned());
    }
    let names: Vec<Value<'_>> = items(&result.getattr("target_results")?)?
        .iter()
        .map(|target| target.getattr("target_model_name"))
        .collect::<PyResult<_>>()?;
    if names.len() == 1 {
        return formatted(&names[0]);
    }
    joined(py, &PyList::new(py, names)?.into_any())
}

// ---------------------------------------------------------------------------------------------
// Runtime target

/// Persist the exact SQL handed to the adapter under `run/tests`.
#[pyfunction]
#[pyo3(signature = (*, target_dir, test_cases, results))]
fn write_test_runtime_target(
    py: Python<'_>,
    target_dir: &Value<'_>,
    test_cases: &Value<'_>,
    results: &Value<'_>,
) -> PyResult<()> {
    let executed_sql: Bound<'_, PyDict> = PyDict::new(py);
    for result in items(results)? {
        let key: Value<'_> = tuple(
            py,
            vec![result.getattr("file_path")?, result.getattr("test_index")?],
        )?;
        executed_sql.set_item(key, result.getattr("executed_sql")?)?;
    }
    let run_dir: Value<'_> = target_dir.div(RUNTIME_TARGET_OWNER)?;
    make_directories(&run_dir)?;
    let path_class: Value<'_> = py.import("pathlib")?.getattr("Path")?;
    let temporary = || -> PyResult<Value<'_>> {
        let directory: Value<'_> = method(
            &py.import("tempfile")?.into_any(),
            "mkdtemp",
            &[
                ("prefix", text(py, RUNTIME_STAGING_PREFIX)),
                ("dir", target_dir.clone()),
            ],
        )?;
        path_class.call1((directory,))
    };
    let staging_root: Value<'_> = temporary()?;
    let backup_root: Value<'_> = temporary()?;
    let outcome: PyResult<()> = (|| {
        stage_runtime_tests(py, &staging_root, test_cases, &executed_sql)?;
        publish_runtime_tests(py, &staging_root, &backup_root, &run_dir)
    })();
    for root in [&staging_root, &backup_root] {
        if let Err(failure) = remove_tree(root) {
            return Err(match outcome.as_ref().err() {
                Some(pending) => while_handling(py, failure, pending),
                None => failure,
            });
        }
    }
    outcome
}

fn stage_runtime_tests(
    py: Python<'_>,
    staging_root: &Value<'_>,
    test_cases: &Value<'_>,
    executed_sql: &Bound<'_, PyDict>,
) -> PyResult<()> {
    make_directories(&staging_root.div(RUNTIME_TESTS_OWNER)?)?;
    let path_class: Value<'_> = py.import("pathlib")?.getattr("Path")?;
    for test_case in items(test_cases)? {
        let key: Value<'_> = tuple(
            py,
            vec![
                test_case.getattr("file_path")?,
                test_case.getattr("test_index")?,
            ],
        )?;
        let sql: Value<'_> = executed_sql.call_method1("get", (key,))?;
        if sql.is_none() {
            continue;
        }
        let relative_path: Value<'_> = call(
            py,
            NATIVE,
            "runtime_test_path",
            &[("test_case", test_case.clone())],
        )?;
        let parts: Value<'_> =
            relative_path
                .getattr("parts")?
                .get_item(PySlice::new(py, 1, isize::MAX, 1))?;
        let staged_path: Value<'_> =
            staging_root.div(path_class.call1(parts.downcast_into::<pyo3::types::PyTuple>()?)?)?;
        make_directories(&staged_path.getattr("parent")?)?;
        staged_path.call_method1("write_bytes", (sql.call_method1("encode", ("utf-8",))?,))?;
    }
    Ok(())
}

fn replace_path(py: Python<'_>, source: &Value<'_>, destination: &Value<'_>) -> PyResult<()> {
    py.import("os")?
        .getattr("replace")?
        .call1((source, destination))?;
    Ok(())
}

fn publish_runtime_tests(
    py: Python<'_>,
    staging_root: &Value<'_>,
    backup_root: &Value<'_>,
    run_dir: &Value<'_>,
) -> PyResult<()> {
    let live_path: Value<'_> = run_dir.div(RUNTIME_TESTS_OWNER)?;
    let backup_path: Value<'_> = backup_root.div(RUNTIME_TESTS_OWNER)?;
    let moved: bool = live_path.call_method0("exists")?.is_truthy()?;
    if moved {
        replace_path(py, &live_path, &backup_path)?;
    }
    match replace_path(py, &staging_root.div(RUNTIME_TESTS_OWNER)?, &live_path) {
        Err(error) if error.is_instance_of::<PyOSError>(py) => {
            if moved && let Err(failure) = replace_path(py, &backup_path, &live_path) {
                return Err(while_handling(py, failure, &error));
            }
            Err(error)
        }
        outcome => outcome,
    }
}

// ---------------------------------------------------------------------------------------------
// Command

pub(crate) fn summary<'py>(py: Python<'py>, key: &str, value: Value<'py>) -> PyResult<Value<'py>> {
    let summary: Bound<'py, PyDict> = PyDict::new(py);
    summary.set_item(key, value)?;
    Ok(summary.into_any())
}

/// `persist_terminal_observations(...)`, ignoring the returned warning as the commands always have.
pub(crate) fn persist_observations<'py>(
    client: &Value<'py>,
    database: &Value<'py>,
    invocation: Value<'py>,
    node_results: Vec<Value<'py>>,
) -> PyResult<()> {
    let py: Python<'py> = client.py();
    call(
        py,
        TERMINAL_OBSERVATIONS,
        "persist_terminal_observations",
        &[
            ("client", client.clone()),
            ("database", database.clone()),
            ("invocation", invocation),
            ("node_results", tuple(py, node_results)?),
        ],
    )?;
    Ok(())
}

/// The quality-result context of a manual run, with the node's cadence and warmup when given.
pub(crate) fn manual_context<'py>(
    py: Python<'py>,
    timing: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let trigger: Value<'py> =
        class(py, OBSERVABILITY_TYPES, "QualityResultTrigger")?.getattr("MANUAL")?;
    let mut arguments: Vec<(&str, Value<'py>)> = vec![("trigger", trigger)];
    arguments.extend(timing.iter().cloned());
    build(py, OBSERVABILITY_MODELS, "QualityResultContext", &arguments)
}

/// `_native.require_quality_identity(node.quality_identity)`.
pub(crate) fn quality_identity<'py>(node: &Value<'py>) -> PyResult<Value<'py>> {
    node.py()
        .import(NATIVE)?
        .getattr("require_quality_identity")?
        .call1((node.getattr("quality_identity")?,))
}

/// `build_node_result_record(...)` for one quality node.
pub(crate) fn node_result_record<'py>(
    py: Python<'py>,
    arguments: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    call(
        py,
        NODE_RESULT_RECORD,
        "build_node_result_record",
        arguments,
    )
}

/// Executed tests, or `None` when nothing was selected and the invocation is already recorded.
type Executed<'py> = Option<(Value<'py>, Value<'py>)>;

/// The inputs of one `sqt test` invocation.
struct TestCommand<'a, 'py> {
    py: Python<'py>,
    started: Value<'py>,
    pipelines_root: &'a Value<'py>,
    project_dir: Value<'py>,
    selectors: &'a Value<'py>,
    paths: &'a Value<'py>,
    verbose: &'a Value<'py>,
    client: &'a Value<'py>,
    loaded_project: &'a Value<'py>,
    adapter_profile: &'a Value<'py>,
    target_dir: Value<'py>,
    database: Value<'py>,
}

impl<'py> TestCommand<'_, 'py> {
    fn invocation(
        &self,
        outcome: &str,
        exit_code: i64,
        selected_node_count: i64,
        error_message: Value<'py>,
        summary: Value<'py>,
    ) -> PyResult<Value<'py>> {
        let py: Python<'py> = self.py;
        invocation_record(
            py,
            &self.started,
            &[
                ("project_dir", self.project_dir.clone()),
                ("target_identity", self.database.clone()),
                ("command", text(py, "test")),
                ("mode", none(py)),
                ("outcome", text(py, outcome)),
                ("exit_code", int(py, exit_code)?),
                ("materialized_outcome", none(py)),
                ("deployment_id", none(py)),
                ("workflow_id", none(py)),
                ("selected_node_count", int(py, selected_node_count)?),
                ("error_message", error_message),
                ("summary", summary),
            ],
        )
    }

    fn persist(&self, invocation: Value<'py>, node_results: Vec<Value<'py>>) -> PyResult<()> {
        persist_observations(self.client, &self.database, invocation, node_results)
    }

    /// The command's `try` body: executed tests, or `None` when nothing was selected and the
    /// invocation is already recorded.
    fn execute(&self, selected_node_count: &mut i64) -> PyResult<Executed<'py>> {
        let py: Python<'py> = self.py;
        let project_dir: &Value<'py> = &self.project_dir;
        self.client
            .call_method1("validate_metadata_state", (&self.database,))?;
        let analysis: Value<'py> = call(
            py,
            NATIVE,
            "analyze_project",
            &[
                ("pipelines_root", self.pipelines_root.clone()),
                ("loaded_project", self.loaded_project.clone()),
                ("adapter_profile", self.adapter_profile.clone()),
            ],
        )?;
        let compiled_project: Value<'py> = analysis.getattr("compiled_project")?;
        let compiled_pipelines: Value<'py> = compiled_project.getattr("pipelines")?;
        let loaded_tests: Value<'py> = compiled_project.getattr("tests")?;
        let selected: Value<'py> = select_loaded_sql_tests(
            py,
            &loaded_tests,
            &compiled_pipelines,
            self.selectors,
            self.paths,
            project_dir,
        )?;
        *selected_node_count = selected.len()? as i64;
        if !selected.is_truthy()? {
            print(py, &text(py, "No SQL tests found."), false)?;
            let invocation: Value<'py> = self.invocation(
                "succeeded",
                0,
                0,
                none(py),
                summary(py, "failed_count", int(py, 0)?)?,
            )?;
            self.persist(invocation, Vec::new())?;
            return Ok(None);
        }
        let selected_keys: Bound<'py, PyFrozenSet> = PyFrozenSet::new(
            py,
            items(&selected)?
                .iter()
                .map(|loaded_test| {
                    tuple(
                        py,
                        vec![
                            loaded_test.getattr("file_path")?,
                            loaded_test.getattr("test_index")?,
                        ],
                    )
                })
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let mut test_cases: Vec<Value<'py>> = Vec::new();
        for test_case in items(&compiled_project.getattr("test_cases")?)? {
            let key: Value<'py> = tuple(
                py,
                vec![
                    test_case.getattr("file_path")?,
                    test_case.getattr("test_index")?,
                ],
            )?;
            if selected_keys.contains(key)? {
                test_cases.push(test_case);
            }
        }
        let test_cases: Value<'py> = tuple(py, test_cases)?;
        let results: Value<'py> = call(
            py,
            EXECUTE_SQL_TESTS,
            "execute_sql_tests",
            &[
                ("test_cases", test_cases.clone()),
                ("client", self.client.clone()),
            ],
        )?;
        let runtime_target: Value<'py> = if self.target_dir.is_truthy()? {
            self.target_dir.clone()
        } else {
            project_dir.div(DEFAULT_TARGET_DIRECTORY_NAME)?
        };
        write_test_runtime_target(py, &runtime_target, &test_cases, &results)?;
        let rendered: String = render_sql_test_results(py, &results, project_dir, self.verbose)?;
        print(py, &text(py, &rendered), false)?;
        Ok(Some((test_cases, results)))
    }
}

fn target_payloads<'py>(py: Python<'py>, result: &Value<'py>) -> PyResult<Value<'py>> {
    let rows = |target: &Value<'py>, name: &str| -> PyResult<Value<'py>> {
        let mut rows: Vec<Value<'py>> = Vec::new();
        for row in items(&target.getattr(name)?.get_item(PySlice::new(
            py,
            0,
            MAX_PAYLOAD_ROWS,
            1,
        ))?)? {
            rows.push(PyList::new(py, items(&row)?)?.into_any());
        }
        Ok(PyList::new(py, rows)?.into_any())
    };
    let mut payloads: Vec<Value<'py>> = Vec::new();
    for target in items(&result.getattr("target_results")?)? {
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("name", target.getattr("target_model_name")?)?;
        payload.set_item("missing_rows", rows(&target, "missing_rows")?)?;
        payload.set_item("unexpected_rows", rows(&target, "unexpected_rows")?)?;
        payloads.push(payload.into_any());
    }
    Ok(PyList::new(py, payloads)?.into_any())
}

fn row_count(result: &Value<'_>, name: &str) -> PyResult<usize> {
    let mut count: usize = 0;
    for target in items(&result.getattr("target_results")?)? {
        count += target.getattr(name)?.len()?;
    }
    Ok(count)
}

fn test_node_result<'py>(
    py: Python<'py>,
    invocation: &Value<'py>,
    test_case: &Value<'py>,
    result: &Value<'py>,
) -> PyResult<Value<'py>> {
    let missing_count: usize = row_count(result, "missing_rows")?;
    let unexpected_count: usize = row_count(result, "unexpected_rows")?;
    let has_error: bool = !is_none_attribute(result, "error_message")?;
    let status: &str = if has_error {
        "error"
    } else if result.getattr("passed")?.is_truthy()? {
        "passed"
    } else {
        "failed"
    };
    let identity: Value<'py> = quality_identity(test_case)?;
    let context: Value<'py> = manual_context(py, &[])?;
    let failure_count: i64 = (missing_count + unexpected_count + usize::from(has_error)) as i64;
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item("missing_count", missing_count)?;
    payload.set_item("unexpected_count", unexpected_count)?;
    payload.set_item("targets", target_payloads(py, result)?)?;
    node_result_record(
        py,
        &[
            ("invocation", invocation.clone()),
            ("identity", identity),
            ("context", context),
            ("status", text(py, status)),
            ("severity", none(py)),
            ("failure_count", int(py, failure_count)?),
            ("payload", payload.into_any()),
            ("error_message", result.getattr("error_message")?),
        ],
    )
}

/// Discover, assemble, and execute SQL-native tests for a project.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, project_dir, selectors, paths, verbose, client, loaded_project,
    adapter_profile, target_dir=None, database=None
))]
#[allow(clippy::too_many_arguments)]
fn run_test<'py>(
    py: Python<'py>,
    pipelines_root: &Value<'py>,
    project_dir: &Value<'py>,
    selectors: &Value<'py>,
    paths: &Value<'py>,
    verbose: &Value<'py>,
    client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
    target_dir: Option<Value<'py>>,
    database: Option<Value<'py>>,
) -> PyResult<i64> {
    let started: Value<'py> = call(py, START_INVOCATION, "start_invocation", &[])?;
    let project_dir: Value<'py> = if project_dir.is_truthy()? {
        project_dir.clone()
    } else {
        pipelines_root.getattr("parent")?
    };
    let command: TestCommand<'_, 'py> = TestCommand {
        py,
        started,
        pipelines_root,
        project_dir,
        selectors,
        paths,
        verbose,
        client,
        loaded_project,
        adapter_profile,
        target_dir: target_dir.unwrap_or_else(|| none(py)),
        database: database.unwrap_or_else(|| text(py, "")),
    };
    let mut selected_node_count: i64 = 0;
    let executed: Executed<'py> = match command.execute(&mut selected_node_count) {
        Ok(executed) => executed,
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let record = || -> PyResult<()> {
                let message: Value<'py> = error.value(py).str()?.into_any();
                let invocation: Value<'py> = command.invocation(
                    "failed",
                    1,
                    selected_node_count,
                    message,
                    summary(py, "failed_before_results", boolean(py, true)?)?,
                )?;
                command.persist(invocation, Vec::new())
            };
            if let Err(failure) = record() {
                return Err(while_handling(py, failure, &error));
            }
            return Err(error);
        }
        Err(error) => return Err(error),
    };
    let Some((test_cases, results)) = executed else {
        return Ok(0);
    };
    let mut failed_count: i64 = 0;
    for result in items(&results)? {
        if !result.getattr("passed")?.is_truthy()? {
            failed_count += 1;
        }
    }
    let exit_code: i64 = i64::from(failed_count > 0);
    let outcome: &str = if exit_code != 0 {
        "failed"
    } else {
        "succeeded"
    };
    let invocation: Value<'py> = command.invocation(
        outcome,
        exit_code,
        test_cases.len()? as i64,
        none(py),
        summary(py, "failed_count", int(py, failed_count)?)?,
    )?;
    let mut cases = test_cases.try_iter()?;
    let mut outcomes = results.try_iter()?;
    let mut node_results: Vec<Value<'py>> = Vec::new();
    while let Some((test_case, result)) = strict_pair(&mut cases, &mut outcomes)? {
        node_results.push(test_node_result(py, &invocation, &test_case, &result)?);
    }
    command.persist(invocation, node_results)?;
    Ok(exit_code)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_test, module)?)?;
    module.add_function(wrap_pyfunction!(select_loaded_sql_tests, module)?)?;
    module.add_function(wrap_pyfunction!(render_sql_test_results, module)?)?;
    module.add_function(wrap_pyfunction!(write_test_runtime_target, module)?)
}
