//! `sqt playground`: copy the packaged, local-only virtual workflow example.

use pyo3::exceptions::{PyFileExistsError, PyOSError};
use pyo3::prelude::*;

use crate::cli::python::{print, raised_from, user_error};
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

/// Copy the templates to `working_directory / directory`, never over an existing path.
fn copy_templates(py: Python<'_>, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let template: Bound<'_, PyAny> = attr(py, "importlib.resources", "files")?
        .call1(("sqltide",))?
        .call_method1("joinpath", (TEMPLATES,))?;
    let context: Bound<'_, PyAny> =
        attr(py, "importlib.resources", "as_file")?.call1((template,))?;
    let path: Bound<'_, PyAny> = context.call_method0("__enter__")?;
    let copied: PyResult<Bound<'_, PyAny>> =
        attr(py, "shutil", "copytree")?.call1((path, destination));
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

/// Create the playground project and print what to do next; exit status 0.
pub(crate) fn run_playground(
    py: Python<'_>,
    directory: &Bound<'_, PyAny>,
    working_directory: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let destination: Bound<'_, PyAny> =
        working_directory.call_method1("__truediv__", (directory,))?;
    if let Err(error) = copy_templates(py, &destination) {
        let replacement: PyErr = if error.is_instance_of::<PyFileExistsError>(py) {
            user_error(
                py,
                &format!(
                    "Playground destination already exists: {}",
                    directory.str()?
                ),
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
    print(
        py,
        &format!(
            "SQLTide playground created\n\
             \n\
             New version. Same query.\n\
             Plan → build candidate → inspect → promote\n\
             \n\
             Try offline (no Docker, ClickHouse, or Kafka needed):\n  \
             cd {}\n  \
             sqt compile\n\
             \n\
             Then follow README.md for the real ClickHouse workflow.\n\
             It includes Docker Compose setup, seeded orders, and expected query results.\n\
             Plan, build, inspection, and promotion require a running ClickHouse server.\n\
             Nothing has been started, installed, or executed in ClickHouse.",
            shell_quote(&directory.str()?.to_string())
        ),
    )?;
    Ok(0)
}
