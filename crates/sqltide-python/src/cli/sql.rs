//! `sqt lint` and `sqt format`: read-only lint, and formatting with race-checked source writes.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use sqltide_planner::unified_diff::unified_diff;

use crate::cli::invocation::Invocation;
use crate::cli::python::{print, user_error};
use crate::cli::render::render_diagnostic_text;
use crate::dev_server::support::attr;

const NATIVE: &str = "sqltide._native";

/// Python's `str.splitlines(keepends=True)`.
fn split_lines_keeping_ends(text: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = Vec::new();
    let mut start: usize = 0;
    let mut characters = text.char_indices().peekable();
    while let Some((index, character)) = characters.next() {
        if !matches!(
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
        ) {
            continue;
        }
        let mut end: usize = index + character.len_utf8();
        if character == '\r' && characters.peek().is_some_and(|(_, next)| *next == '\n') {
            characters.next();
            end += 1;
        }
        lines.push(&text[start..end]);
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn call<'py>(
    py: Python<'py>,
    function: &Bound<'py, PyAny>,
    entries: Vec<(&str, Bound<'py, PyAny>)>,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in entries {
        kwargs.set_item(name, value)?;
    }
    function.call((), Some(&kwargs))
}

/// `json.dumps(payload, default=str)`.
fn dumps(py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<String> {
    let kwargs: Bound<'_, PyDict> = PyDict::new(py);
    kwargs.set_item("default", py.get_type::<pyo3::types::PyString>())?;
    attr(py, "json", "dumps")?
        .call((payload,), Some(&kwargs))?
        .extract()
}

/// `[asdict(item) for item in diagnostics]`.
fn diagnostic_payloads<'py>(
    py: Python<'py>,
    diagnostics: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyList>> {
    let asdict: Bound<'py, PyAny> = attr(py, "dataclasses", "asdict")?;
    let payloads: Bound<'py, PyList> = PyList::empty(py);
    for diagnostic in diagnostics {
        payloads.append(asdict.call1((diagnostic,))?)?;
    }
    Ok(payloads)
}

/// Whether a diagnostic's path is under one of the selected paths; all are with none selected.
fn selected(path: &Bound<'_, PyAny>, paths: &[Bound<'_, PyAny>]) -> PyResult<bool> {
    if paths.is_empty() {
        return Ok(true);
    }
    let resolved: Bound<'_, PyAny> = path.call_method0("resolve")?;
    for selected in paths {
        if resolved.eq(selected)?
            || resolved
                .call_method1("is_relative_to", (selected,))?
                .is_truthy()?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Lint read-only, or format explicitly; check and diff modes never write.
pub(crate) fn run_sql<'py>(
    py: Python<'py>,
    invocation: &Invocation<'py>,
    adapter_profile: &Bound<'py, PyAny>,
) -> PyResult<i64> {
    let args: &Bound<'py, PyAny> = &invocation.args;
    let root: Bound<'py, PyAny> = match &invocation.pipelines_root {
        Some(root) => root.clone(),
        None => match &invocation.project_dir {
            Some(project_dir) => project_dir.clone(),
            None => attr(py, "pathlib", "Path")?.call_method0("cwd")?,
        }
        .call_method1("__truediv__", ("pipelines",))?,
    };
    let select: Bound<'py, PyAny> = args.getattr("select")?;
    let selection: Bound<'py, PyAny> = if select.is_none() {
        select
    } else {
        py.get_type::<PyTuple>().call1((select,))?
    };
    let project_root: Bound<'py, PyAny> = root.getattr("parent")?;
    let mut paths: Vec<Bound<'py, PyAny>> = Vec::new();
    for path in args.getattr("paths")?.try_iter()? {
        let path: Bound<'py, PyAny> = path?;
        let absolute: Bound<'py, PyAny> = if path.call_method0("is_absolute")?.is_truthy()? {
            path
        } else {
            project_root.call_method1("__truediv__", (path,))?
        };
        paths.push(absolute.call_method0("resolve")?);
    }
    let resolved_root: Bound<'py, PyAny> = project_root.call_method0("resolve")?;
    for path in &paths {
        if !path
            .call_method1("is_relative_to", (&resolved_root,))?
            .is_truthy()?
            || !path.call_method0("exists")?.is_truthy()?
        {
            return Err(user_error(
                py,
                "SQL paths must exist inside the project",
                None,
            ));
        }
    }
    let json_output: bool = args.getattr("json")?.is_truthy()?;
    if args.getattr("command")?.eq("lint")? {
        let analysis: Bound<'py, PyAny> = call(
            py,
            &attr(py, NATIVE, "analyze_project")?,
            vec![
                ("pipelines_root", root),
                ("loaded_project", invocation.loaded_project.clone()),
                ("adapter_profile", adapter_profile.clone()),
                (
                    "enforce_rules",
                    false.into_pyobject(py)?.to_owned().into_any(),
                ),
                ("rule_selection", selection),
            ],
        )?;
        let mut diagnostics: Vec<Bound<'py, PyAny>> = Vec::new();
        for item in analysis.getattr("diagnostics")?.try_iter()? {
            let item: Bound<'py, PyAny> = item?;
            let location: Bound<'py, PyAny> = item.getattr("location")?;
            if !location.is_none() && selected(&location.getattr("path")?, &paths)? {
                diagnostics.push(item);
            }
        }
        if json_output {
            let payload: Bound<'py, PyDict> = PyDict::new(py);
            payload.set_item("diagnostics", diagnostic_payloads(py, &diagnostics)?)?;
            print(py, &dumps(py, payload.as_any())?)?;
        } else {
            for diagnostic in &diagnostics {
                print(py, &render_diagnostic_text(diagnostic, None)?)?;
            }
        }
        return Ok(i64::from(!diagnostics.is_empty()));
    }
    let results: Bound<'py, PyAny> = call(
        py,
        &attr(py, NATIVE, "format_project_sql")?,
        vec![
            ("root", root),
            ("loaded_project", invocation.loaded_project.clone()),
            ("adapter_profile", adapter_profile.clone()),
            ("selection", selection),
            ("paths", PyTuple::new(py, &paths)?.into_any()),
            ("fix", args.getattr("fix")?),
        ],
    )?;
    report_format_results(py, &results, args)
}

/// Write only accepted candidates; a nonzero status for failures, Rule findings, or changes
/// under `--check`.
fn report_format_results<'py>(
    py: Python<'py>,
    results: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>,
) -> PyResult<i64> {
    let diff: bool = args.getattr("diff")?.is_truthy()?;
    let check: bool = args.getattr("check")?.is_truthy()?;
    let json_output: bool = args.getattr("json")?.is_truthy()?;
    let mut changed: Vec<String> = Vec::new();
    let mut failures: Vec<String> = Vec::new();
    let mut diagnostics: Vec<Bound<'py, PyAny>> = Vec::new();
    for result in results.try_iter()? {
        let result: Bound<'py, PyAny> = result?;
        for diagnostic in result.getattr("diagnostics")?.try_iter()? {
            diagnostics.push(diagnostic?);
        }
        let error: Bound<'py, PyAny> = result.getattr("error")?;
        if !error.is_none() {
            failures.push(error.extract()?);
            continue;
        }
        let formatted: String = result.getattr("formatted")?.extract()?;
        let original: String = result.getattr("original")?.extract()?;
        if formatted == original {
            continue;
        }
        let path: String = result.getattr("path")?.str()?.to_string();
        changed.push(path.clone());
        if diff && !json_output {
            let lines: Vec<String> = unified_diff(
                &split_lines_keeping_ends(&original),
                &split_lines_keeping_ends(&formatted),
                &path,
                &path,
                "\n",
            );
            let stdout: Bound<'py, PyAny> = py.import("sys")?.getattr("stdout")?;
            if !stdout.is_none() {
                stdout.call_method1("write", (lines.concat(),))?;
            }
        }
        if !check
            && !diff
            && let Some(failure) = write_source(py, &result, &formatted, &original)?
        {
            failures.push(failure);
        }
    }
    if json_output {
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("formatted_files", PyList::new(py, &changed)?)?;
        payload.set_item("failures", PyList::new(py, &failures)?)?;
        payload.set_item("diagnostics", diagnostic_payloads(py, &diagnostics)?)?;
        print(py, &dumps(py, payload.as_any())?)?;
    } else {
        for failure in &failures {
            print(py, failure)?;
        }
        for diagnostic in &diagnostics {
            print(py, &render_diagnostic_text(diagnostic, None)?)?;
        }
        if !diff {
            print(
                py,
                &format!(
                    "{} {} file(s)",
                    if check { "Would format" } else { "Formatted" },
                    changed.len()
                ),
            )?;
        }
    }
    Ok(i64::from(
        !failures.is_empty() || !diagnostics.is_empty() || (check && !changed.is_empty()),
    ))
}

/// Stage and sync beside the source, then replace only the unchanged raw snapshot; the failure
/// line, if the source was left as it was.
fn write_source(
    py: Python<'_>,
    result: &Bound<'_, PyAny>,
    formatted: &str,
    original: &str,
) -> PyResult<Option<String>> {
    let path: Bound<'_, PyAny> = result.getattr("path")?;
    let mut temporary: Option<Bound<'_, PyAny>> = None;
    let outcome: PyResult<Option<String>> = (|| {
        let kwargs: Bound<'_, PyDict> = PyDict::new(py);
        kwargs.set_item("dir", path.getattr("parent")?)?;
        kwargs.set_item("prefix", format!(".{}.", path.getattr("name")?.str()?))?;
        kwargs.set_item("delete", false)?;
        let handle: Bound<'_, PyAny> =
            attr(py, "tempfile", "NamedTemporaryFile")?.call((), Some(&kwargs))?;
        let file: Bound<'_, PyAny> = handle.call_method0("__enter__")?;
        let written: PyResult<()> = (|| {
            let staged: Bound<'_, PyAny> =
                attr(py, "pathlib", "Path")?.call1((file.getattr("name")?,))?;
            temporary = Some(staged.clone());
            let mode: Bound<'_, PyAny> = attr(py, "stat", "S_IMODE")?
                .call1((path.call_method0("stat")?.getattr("st_mode")?,))?;
            staged.call_method1("chmod", (mode,))?;
            file.call_method1(
                "write",
                (pyo3::types::PyBytes::new(py, formatted.as_bytes()),),
            )?;
            file.call_method0("flush")?;
            attr(py, "os", "fsync")?.call1((file.call_method0("fileno")?,))?;
            Ok(())
        })();
        match &written {
            Ok(()) => {
                handle.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
            }
            Err(error) => {
                let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
                handle.call_method1(
                    "__exit__",
                    (value.get_type(), &value, value.getattr("__traceback__")?),
                )?;
            }
        }
        written?;
        let current: Vec<u8> = path.call_method0("read_bytes")?.extract()?;
        if current != original.as_bytes() {
            return Ok(Some(format!(
                "{}: source changed during formatting; no write performed",
                path.str()?
            )));
        }
        if let Some(staged) = &temporary {
            staged.call_method1("replace", (&path,))?;
        }
        Ok(None)
    })();
    let outcome: PyResult<Option<String>> = match outcome {
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyOSError>(py) => Ok(Some(format!(
            "{}: {}; original source preserved",
            path.str()?,
            error.value(py).str()?
        ))),
        outcome => outcome,
    };
    if let Some(staged) = temporary {
        let kwargs: Bound<'_, PyDict> = PyDict::new(py);
        kwargs.set_item("missing_ok", true)?;
        staged.call_method("unlink", (), Some(&kwargs))?;
    }
    outcome
}
