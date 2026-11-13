//! `sqt playground`: create a packaged local example project, start and stop its Docker
//! Compose services, and run SQL files against it.

use pyo3::exceptions::{PyFileExistsError, PyFileNotFoundError, PyOSError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::cli::python::{flag, getattr_none, print, print_to, raised_from, user_error};
use crate::dev_server::support::attr;

const TEMPLATES: &str = "playground_templates";

/// `shlex.quote`: the text unchanged when it is shell-safe, else single-quoted.
fn shell_quote(text: &str) -> String {
    if text.is_empty() {
        return "''".to_owned();
    }
    let safe =
        |character: char| character.is_ascii_alphanumeric() || "_@%+=:,./-".contains(character);
    if text.chars().all(safe) {
        return text.to_owned();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}

/// Copy one template to `destination`, never over an existing path.
fn copy_template(py: Python<'_>, name: &str, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let template: Bound<'_, PyAny> = attr(py, "importlib.resources", "files")?
        .call1(("sqltide",))?
        .call_method1("joinpath", (TEMPLATES, name))?;
    let context: Bound<'_, PyAny> =
        attr(py, "importlib.resources", "as_file")?.call1((template,))?;
    let path: Bound<'_, PyAny> = context.call_method0("__enter__")?;
    // Leave out bytecode and compile output a source checkout may hold beside the template.
    let ignore: Bound<'_, PyAny> =
        attr(py, "shutil", "ignore_patterns")?.call1(("__pycache__", "target"))?;
    let options: Bound<'_, pyo3::types::PyDict> = pyo3::types::PyDict::new(py);
    options.set_item("ignore", ignore)?;
    let copied: PyResult<Bound<'_, PyAny>> =
        attr(py, "shutil", "copytree")?.call((path, destination), Some(&options));
    match copied {
        Ok(_) => {
            context.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
            Ok(())
        }
        Err(error) => {
            let value: Bound<'_, PyAny> = error.value(py).clone().into_any();
            let suppressed: bool = context
                .call_method1(
                    "__exit__",
                    (value.get_type(), &value, value.getattr("__traceback__")?),
                )?
                .is_truthy()?;
            if suppressed { Ok(()) } else { Err(error) }
        }
    }
}

/// The `--template` choice's default directory.
fn default_directory(template: &str) -> &'static str {
    if template == "streaming" {
        "sqltide-streaming-playground"
    } else {
        "sqltide-playground"
    }
}

/// How long `up` waits for ClickHouse to answer from this machine after Compose reports healthy.
const READY_TIMEOUT_SECONDS: f64 = 120.0;
const COMPOSE_FILE: &str = "compose.yaml";
const PROJECT_FILE: &str = "sqltide_project.toml";

/// The positional words: `query FILE`, `up`, `down`, or the directory to create.
fn arguments(args: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let value: Bound<'_, PyAny> = getattr_none(args, "arguments")?;
    if value.is_none() {
        return Ok(Vec::new());
    }
    value.extract()
}

/// Whether `sqt playground` was given `query FILE`, which needs the project's connection.
pub(crate) fn is_query(args: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(arguments(args)?.first().is_some_and(|word| word == "query"))
}

/// Reject options that do not belong to the chosen action.
fn reject_options(
    py: Python<'_>,
    args: &Bound<'_, PyAny>,
    action: &str,
    options: &[&str],
) -> PyResult<()> {
    for option in options {
        let present: bool = if *option == "project_dir" {
            !getattr_none(args, option)?.is_none()
        } else {
            flag(args, option)?
        };
        if present {
            return Err(user_error(
                py,
                &format!(
                    "--{} does not apply to sqt playground {action}",
                    option.replace('_', "-")
                ),
                None,
            ));
        }
    }
    Ok(())
}

/// `sqt playground [DIRECTORY]`, `up` or `down`; exit status.
pub(crate) fn run_playground(
    py: Python<'_>,
    args: &Bound<'_, PyAny>,
    working_directory: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let words: Vec<String> = arguments(args)?;
    match words.first().map(String::as_str) {
        Some(action @ ("up" | "down")) => {
            if words.len() > 1 {
                return Err(user_error(
                    py,
                    &format!("sqt playground {action} takes no other arguments"),
                    Some("Pass the project with --project-dir."),
                ));
            }
            let mut rejected: Vec<&str> = vec!["no_start", "candidate", "compare"];
            if action == "up" {
                rejected.push("volumes");
            }
            reject_options(py, args, action, &rejected)?;
            let project: Bound<'_, PyAny> = playground_project(py, args, working_directory)?;
            if action == "up" {
                return start(py, &project, None);
            }
            stop(py, &project, flag(args, "volumes")?)
        }
        _ => {
            if words.len() > 1 {
                return Err(user_error(
                    py,
                    "sqt playground takes one directory",
                    Some("Use sqt playground query FILE to run a SQL file."),
                ));
            }
            reject_options(
                py,
                args,
                "",
                &["candidate", "compare", "volumes", "project_dir"],
            )?;
            let template: String = args.getattr("template")?.extract()?;
            let directory: String = words
                .first()
                .cloned()
                .unwrap_or_else(|| default_directory(&template).to_owned());
            create(
                py,
                &directory,
                &template,
                flag(args, "no_start")?,
                working_directory,
            )
        }
    }
}

/// Copy the template to `directory`, then start its services unless `no_start`.
fn create(
    py: Python<'_>,
    directory: &str,
    template: &str,
    no_start: bool,
    working_directory: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let destination: Bound<'_, PyAny> =
        working_directory.call_method1("__truediv__", (directory,))?;
    if let Err(error) = copy_template(py, template, &destination) {
        let replacement: PyErr = if error.is_instance_of::<PyFileExistsError>(py) {
            user_error(
                py,
                &format!("Playground destination already exists: {directory}"),
                Some("Choose a new directory. Existing files are never overwritten."),
            )
        } else if error.is_instance_of::<PyOSError>(py) {
            user_error(
                py,
                &format!("Could not create playground: {}", error.value(py).str()?),
                None,
            )
        } else {
            return Err(error);
        };
        return Err(raised_from(py, replacement, error));
    }
    let shown: String = shell_quote(directory);
    if no_start {
        print(
            py,
            &format!(
                "Created the SQLTide playground in {shown}. Nothing was started.\n\
                 \n\
                 Start its local services with Docker:\n  \
                 cd {shown}\n  \
                 sqt playground up\n\
                 \n\
                 Then follow README.md."
            ),
        )?;
        return Ok(0);
    }
    print(
        py,
        &format!("Created the SQLTide playground in {shown}. Starting it with Docker…"),
    )?;
    start(py, &destination, Some(&shown))
}

/// The playground project of `query`, `up` and `down`: `--project-dir`, else the current
/// directory, which must hold the project file and its Compose file.
pub(crate) fn playground_project<'py>(
    py: Python<'py>,
    args: &Bound<'py, PyAny>,
    working_directory: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let option: Bound<'py, PyAny> = getattr_none(args, "project_dir")?;
    let project: Bound<'py, PyAny> = if option.is_none() {
        working_directory.clone()
    } else {
        working_directory.call_method1("__truediv__", (option,))?
    };
    for file in [PROJECT_FILE, COMPOSE_FILE] {
        if !project
            .call_method1("__truediv__", (file,))?
            .call_method0("is_file")?
            .is_truthy()?
        {
            return Err(user_error(
                py,
                &format!(
                    "{} is not a playground project: it has no {file}",
                    project.str()?
                ),
                Some("Run this in the directory sqt playground created, or pass --project-dir."),
            ));
        }
    }
    Ok(project)
}

/// `docker compose ARGS` in `project`, showing Docker's own output; the failure to report.
fn compose(
    py: Python<'_>,
    project: &Bound<'_, PyAny>,
    arguments: &[&str],
) -> PyResult<Option<String>> {
    let mut command: Vec<&str> = vec!["docker", "compose"];
    command.extend(arguments);
    let options: Bound<'_, PyDict> = PyDict::new(py);
    options.set_item("cwd", project)?;
    match attr(py, "subprocess", "run")?.call((PyList::new(py, command)?,), Some(&options)) {
        Ok(completed) => {
            let code: i64 = completed.getattr("returncode")?.extract()?;
            Ok((code != 0).then(|| {
                format!(
                    "docker compose {} exited with status {code}",
                    arguments.join(" ")
                )
            }))
        }
        Err(error) if error.is_instance_of::<PyFileNotFoundError>(py) => {
            Ok(Some("Docker is not installed or not on PATH".to_owned()))
        }
        Err(error) => Err(error),
    }
}

/// Wait until the project's ClickHouse answers from this machine. Compose's health check runs
/// inside the container, where ClickHouse also answers while its first-start setup still runs.
fn wait_until_ready(py: Python<'_>, project: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let text: Bound<'_, PyAny> = project
        .call_method1("__truediv__", (PROJECT_FILE,))?
        .call_method0("read_text")?;
    let config: Bound<'_, PyAny> = attr(py, "tomllib", "loads")?.call1((text,))?;
    let connection: Bound<'_, PyAny> = config.get_item("connection")?;
    let host: String = connection.get_item("host")?.extract()?;
    let port: i64 = connection.get_item("port")?.extract()?;
    let url: String = format!("http://{host}:{port}/ping");
    let monotonic: Bound<'_, PyAny> = attr(py, "time", "monotonic")?;
    let deadline: f64 = monotonic.call0()?.extract::<f64>()? + READY_TIMEOUT_SECONDS;
    // The ping goes to this machine, never through a configured HTTP proxy.
    let no_proxy: Bound<'_, PyAny> =
        attr(py, "urllib.request", "ProxyHandler")?.call1((PyDict::new(py),))?;
    let opener: Bound<'_, PyAny> =
        attr(py, "urllib.request", "build_opener")?.call1((no_proxy,))?;
    let http_error: Bound<'_, PyAny> = attr(py, "http.client", "HTTPException")?;
    loop {
        let options: Bound<'_, PyDict> = PyDict::new(py);
        options.set_item("timeout", 2)?;
        match opener.call_method("open", (&url,), Some(&options)) {
            Ok(response) => {
                response.call_method0("close")?;
                return Ok(None);
            }
            Err(error)
                if error.is_instance_of::<PyOSError>(py) || error.is_instance(py, &http_error) =>
            {
                if monotonic.call0()?.extract::<f64>()? > deadline {
                    return Ok(Some(format!("ClickHouse did not answer at {url}")));
                }
                attr(py, "time", "sleep")?.call1((1,))?;
            }
            Err(error) => return Err(error),
        }
    }
}

/// Start the project's services and wait until they answer. `created` is the directory just
/// created, for the next steps.
fn start(py: Python<'_>, project: &Bound<'_, PyAny>, created: Option<&str>) -> PyResult<i64> {
    let failure: Option<String> =
        match compose(py, project, &["up", "--detach", "--build", "--wait"])? {
            Some(failure) => Some(failure),
            None => wait_until_ready(py, project)?,
        };
    if let Some(failure) = failure {
        let directory: String = created.map_or_else(|| ".".to_owned(), str::to_owned);
        print_to(
            py,
            "stderr",
            &format!(
                "The playground's services did not start: {failure}.\n\
                 Start Docker, then run `sqt playground up` in {directory}."
            ),
        )?;
        return Ok(1);
    }
    let enter: String = created.map_or_else(String::new, |directory| format!("  cd {directory}\n"));
    print(
        py,
        &format!(
            "\nThe playground is running.\n\
             \n\
             Next:\n\
             {enter}  sqt build\n\
             \n\
             README.md walks through building, comparing and promoting a change.\n\
             Stop it with `sqt playground down`."
        ),
    )?;
    Ok(0)
}

/// Stop the project's services, deleting its data with `volumes`.
fn stop(py: Python<'_>, project: &Bound<'_, PyAny>, volumes: bool) -> PyResult<i64> {
    let arguments: &[&str] = if volumes {
        &["down", "--volumes"]
    } else {
        &["down"]
    };
    if let Some(failure) = compose(py, project, arguments)? {
        print_to(
            py,
            "stderr",
            &format!("Could not stop the playground: {failure}."),
        )?;
        return Ok(1);
    }
    print(
        py,
        if volumes {
            "Stopped the playground and deleted its data."
        } else {
            "Stopped the playground. Its data is kept; `sqt playground up` starts it again."
        },
    )?;
    Ok(0)
}

/// One statement of a SQL file and its first keyword, upper-cased.
struct Statement {
    text: String,
    keyword: String,
}

/// Split SQL text on `;` outside quotes and comments, dropping statements with no code.
fn statements(sql: &str) -> Vec<Statement> {
    let characters: Vec<char> = sql.chars().collect();
    let mut found: Vec<Statement> = Vec::new();
    let mut text: String = String::new();
    let mut keyword: String = String::new();
    let mut keyword_done: bool = false;
    let mut index: usize = 0;
    let mut finish = |text: &mut String, keyword: &mut String, keyword_done: &mut bool| {
        if !keyword.is_empty() || *keyword_done {
            found.push(Statement {
                text: text.trim().to_owned(),
                keyword: keyword.to_uppercase(),
            });
        }
        text.clear();
        keyword.clear();
        *keyword_done = false;
    };
    while index < characters.len() {
        let character: char = characters[index];
        let next: Option<char> = characters.get(index + 1).copied();
        if character == '-' && next == Some('-') {
            while index < characters.len() && characters[index] != '\n' {
                text.push(characters[index]);
                index += 1;
            }
            continue;
        }
        if character == '/' && next == Some('*') {
            text.push_str("/*");
            index += 2;
            while index < characters.len()
                && !(characters[index] == '*' && characters.get(index + 1) == Some(&'/'))
            {
                text.push(characters[index]);
                index += 1;
            }
            text.push_str("*/");
            index += 2;
            continue;
        }
        if character == ';' {
            finish(&mut text, &mut keyword, &mut keyword_done);
            index += 1;
            continue;
        }
        if !keyword_done && !character.is_whitespace() {
            if character.is_ascii_alphabetic() {
                keyword.push(character);
            } else {
                keyword_done = true;
            }
        } else if !keyword.is_empty() {
            keyword_done = true;
        }
        if matches!(character, '\'' | '"' | '`') {
            text.push(character);
            index += 1;
            while index < characters.len() && characters[index] != character {
                if characters[index] == '\\' && index + 1 < characters.len() {
                    text.push(characters[index]);
                    index += 1;
                }
                text.push(characters[index]);
                index += 1;
            }
        }
        if index < characters.len() {
            text.push(characters[index]);
        }
        index += 1;
    }
    finish(&mut text, &mut keyword, &mut keyword_done);
    found
}

/// `sql` with each whole identifier in `names` replaced, outside string literals; the count.
fn replace_identifiers(sql: &str, names: &[(String, String)]) -> (String, usize) {
    let characters: Vec<char> = sql.chars().collect();
    let identifier = |character: char| character.is_ascii_alphanumeric() || character == '_';
    let mut replaced: String = String::with_capacity(sql.len());
    let mut count: usize = 0;
    let mut index: usize = 0;
    while index < characters.len() {
        let character: char = characters[index];
        let next: Option<char> = characters.get(index + 1).copied();
        let comment_end: Option<&str> = match (character, next) {
            ('-', Some('-')) => Some("\n"),
            ('/', Some('*')) => Some("*/"),
            _ => None,
        };
        if let Some(end) = comment_end {
            let rest: String = characters[index..].iter().collect();
            let length: usize = rest[2..]
                .find(end)
                .map_or(rest.len(), |position| position + 2 + end.len());
            replaced.push_str(&rest[..length]);
            index += rest[..length].chars().count();
            continue;
        }
        if character == '\'' {
            replaced.push(character);
            index += 1;
            while index < characters.len() && characters[index] != '\'' {
                if characters[index] == '\\' && index + 1 < characters.len() {
                    replaced.push(characters[index]);
                    index += 1;
                }
                replaced.push(characters[index]);
                index += 1;
            }
            if index < characters.len() {
                replaced.push('\'');
                index += 1;
            }
            continue;
        }
        if identifier(character) {
            let start: usize = index;
            while index < characters.len() && identifier(characters[index]) {
                index += 1;
            }
            let word: String = characters[start..index].iter().collect();
            match names.iter().find(|(logical, _)| *logical == word) {
                Some((_, physical)) => {
                    replaced.push_str(physical);
                    count += 1;
                }
                None => replaced.push_str(&word),
            }
            continue;
        }
        replaced.push(character);
        index += 1;
    }
    (replaced, count)
}

/// The newest staged deployment (built, complete, never published), and each of its relations' stable and physical names.
fn candidate_relations(
    py: Python<'_>,
    client: &Bound<'_, PyAny>,
    database: &Bound<'_, PyAny>,
) -> PyResult<(String, Vec<(String, String)>)> {
    let options: Bound<'_, PyDict> = PyDict::new(py);
    options.set_item("client", client)?;
    options.set_item("metadata_database", database)?;
    options.set_item("default_database", database)?;
    let inventory: Bound<'_, PyAny> =
        attr(py, "sqltide._native", "load_deployments")?.call((), Some(&options))?;
    let mut candidates: Vec<(String, String, Vec<String>)> = Vec::new();
    for deployment in inventory.getattr("deployments")?.try_iter()? {
        let deployment: Bound<'_, PyAny> = deployment?;
        if deployment.getattr("state")?.str()? == "staged" {
            candidates.push((
                deployment.getattr("created_at")?.str()?.to_string(),
                deployment.getattr("deployment_id")?.extract()?,
                deployment.getattr("physical_relation_names")?.extract()?,
            ));
        }
    }
    candidates.sort();
    let Some((_, deployment_id, physical)) = candidates.pop() else {
        return Err(user_error(
            py,
            "There is no staged deployment to read.",
            Some("Change a model, then run sqt build."),
        ));
    };
    let suffix: String = format!("__{deployment_id}");
    let names: Vec<(String, String)> = physical
        .into_iter()
        .filter_map(|name| {
            name.strip_suffix(&suffix)
                .map(|logical| (logical.to_owned(), name.clone()))
        })
        .collect();
    Ok((deployment_id, names))
}

/// Rows as an aligned text table with a row count.
fn table(result: &Bound<'_, PyAny>) -> PyResult<String> {
    let columns: Vec<String> = result.getattr("column_names")?.extract()?;
    let mut rows: Vec<Vec<String>> = Vec::new();
    for row in result.getattr("rows")?.try_iter()? {
        let mut cells: Vec<String> = Vec::new();
        for value in row?.try_iter()? {
            let value: Bound<'_, PyAny> = value?;
            cells.push(if value.is_none() {
                "NULL".to_owned()
            } else {
                value.str()?.to_string()
            });
        }
        rows.push(cells);
    }
    let mut widths: Vec<usize> = columns
        .iter()
        .map(|column| column.chars().count())
        .collect();
    for row in &rows {
        for (index, cell) in row.iter().enumerate() {
            if let Some(width) = widths.get_mut(index) {
                *width = (*width).max(cell.chars().count());
            }
        }
    }
    let line = |cells: &[String]| -> String {
        cells
            .iter()
            .zip(&widths)
            .map(|(cell, width)| format!("{cell:<width$}"))
            .collect::<Vec<String>>()
            .join("  ")
            .trim_end()
            .to_owned()
    };
    let mut lines: Vec<String> = vec![line(&columns)];
    lines.extend(rows.iter().map(|row| line(row)));
    let count: usize = rows.len();
    lines.push(format!(
        "({count} row{})",
        if count == 1 { "" } else { "s" }
    ));
    Ok(lines.join("\n"))
}

const READ_KEYWORDS: [&str; 6] = ["SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXISTS"];

/// Each statement's output: a table for a read, `KEYWORD: done` otherwise; and whether any
/// statement read a renamed relation.
fn run_statements(
    client: &Bound<'_, PyAny>,
    found: &[Statement],
    names: &[(String, String)],
) -> PyResult<(Vec<String>, bool)> {
    let mut outputs: Vec<String> = Vec::new();
    let mut renamed: bool = false;
    for statement in found {
        let (text, count) = replace_identifiers(&statement.text, names);
        renamed |= count > 0;
        if READ_KEYWORDS.contains(&statement.keyword.as_str()) {
            outputs.push(table(&client.call_method1("query", (text,))?)?);
        } else {
            client.call_method1("execute_workflow_sql", (text,))?;
            outputs.push(format!("{}: done", statement.keyword));
        }
    }
    Ok((outputs, renamed))
}

/// `sqt playground query FILE [--candidate | --compare]`: run each statement of `FILE` against
/// the project's warehouse and print what reads return.
pub(crate) fn run_playground_query(
    py: Python<'_>,
    args: &Bound<'_, PyAny>,
    client: &Bound<'_, PyAny>,
    database: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let words: Vec<String> = arguments(args)?;
    let [_, file] = words.as_slice() else {
        return Err(user_error(
            py,
            "sqt playground query takes one SQL file",
            Some("For example: sqt playground query consumer.sql"),
        ));
    };
    reject_options(py, args, "query", &["no_start", "volumes"])?;
    let candidate: bool = flag(args, "candidate")?;
    let compare: bool = flag(args, "compare")?;
    if candidate && compare {
        return Err(user_error(
            py,
            "Pass --candidate or --compare, not both",
            None,
        ));
    }
    let mut path: Bound<'_, PyAny> = attr(py, "pathlib", "Path")?.call1((file,))?;
    let project_dir: Bound<'_, PyAny> = getattr_none(args, "project_dir")?;
    if !path.call_method0("exists")?.is_truthy()? && !project_dir.is_none() {
        path = attr(py, "pathlib", "Path")?
            .call1((project_dir,))?
            .call_method1("__truediv__", (file,))?;
    }
    let sql: String = match path.call_method0("read_text") {
        Ok(text) => text.extract()?,
        Err(error) if error.is_instance_of::<PyOSError>(py) => {
            return Err(raised_from(
                py,
                user_error(
                    py,
                    &format!("Could not read {file}: {}", error.value(py).str()?),
                    None,
                ),
                error,
            ));
        }
        Err(error) => return Err(error),
    };
    let found: Vec<Statement> = statements(&sql);
    if !candidate && !compare {
        print(py, &run_statements(client, &found, &[])?.0.join("\n\n"))?;
        return Ok(0);
    }
    if compare
        && found
            .iter()
            .any(|statement| !READ_KEYWORDS.contains(&statement.keyword.as_str()))
    {
        return Err(user_error(
            py,
            &format!("--compare runs {file} twice, so it only takes files that read"),
            Some("Run a file that writes without --compare."),
        ));
    }
    let (deployment_id, names) = candidate_relations(py, client, database)?;
    let (candidate_outputs, renamed) = run_statements(client, &found, &names)?;
    if !renamed {
        print_to(
            py,
            "stderr",
            &format!(
                "Note: {file} reads none of the relations deployment {deployment_id} changed."
            ),
        )?;
    }
    if candidate {
        print_to(
            py,
            "stderr",
            &format!("Reading unpublished deployment {deployment_id}"),
        )?;
        print(py, &candidate_outputs.join("\n\n"))?;
        return Ok(0);
    }
    let published: Vec<String> = run_statements(client, &found, &[])?.0;
    print(
        py,
        &format!(
            "Published (what consumers read):\n{}\n\nUnpublished deployment {deployment_id}:\n{}",
            published.join("\n\n"),
            candidate_outputs.join("\n\n")
        ),
    )?;
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_statements_outside_quotes_and_comments() {
        let found: Vec<Statement> = statements(
            "-- what consumers read; stable\nSELECT ';' AS a FROM t;\n\n/* x; */ insert INTO t VALUES (1);\n-- end;\n",
        );
        let parts: Vec<(&str, &str)> = found
            .iter()
            .map(|statement| (statement.keyword.as_str(), statement.text.as_str()))
            .collect();
        assert_eq!(
            parts,
            vec![
                (
                    "SELECT",
                    "-- what consumers read; stable\nSELECT ';' AS a FROM t"
                ),
                ("INSERT", "/* x; */ insert INTO t VALUES (1)"),
            ]
        );
    }

    #[test]
    fn replaces_whole_identifiers_outside_strings() {
        let names: Vec<(String, String)> = vec![(
            "tbl__orders".to_owned(),
            "tbl__orders__20260101T000000Z_a".to_owned(),
        )];
        assert_eq!(
            replace_identifiers(
                "-- today's tbl__orders\nSELECT 'tbl__orders' /* it's tbl__orders */ \
                 FROM db.tbl__orders JOIN tbl__orders_daily",
                &names
            ),
            (
                "-- today's tbl__orders\nSELECT 'tbl__orders' /* it's tbl__orders */ \
                 FROM db.tbl__orders__20260101T000000Z_a JOIN tbl__orders_daily"
                    .to_owned(),
                1
            )
        );
    }
}
