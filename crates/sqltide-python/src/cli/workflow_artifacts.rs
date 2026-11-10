//! Atomic publication of the plan and build workflow artifacts under `target/run/<owner>`.
//! Filesystem operations go through Python's `os`, `shutil`, `tempfile` and `pathlib`, so their
//! errors and text-mode writes stay the standard library's.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use sqltide_planner::direct::sha256_hex;

use crate::cli::python::raised_during;
use crate::dev_server::support::attr;
use crate::direct_build::{build, items};

type Any<'py> = Bound<'py, PyAny>;

const WORKFLOW_MODELS: &str = "sqltide.executor.workflow.models";

fn join<'py>(path: &Any<'py>, name: &str) -> PyResult<Any<'py>> {
    path.call_method1("__truediv__", (name,))
}

fn exists(path: &Any<'_>) -> PyResult<bool> {
    path.call_method0("exists")?.is_truthy()
}

/// `path.write_text(contents, encoding="utf-8")`.
fn write_text(path: &Any<'_>, contents: &Any<'_>) -> PyResult<()> {
    let kwargs: Bound<'_, PyDict> = PyDict::new(path.py());
    kwargs.set_item("encoding", "utf-8")?;
    path.call_method("write_text", (contents,), Some(&kwargs))?;
    Ok(())
}

/// `"\n".join(statement.sql for statement in workflow.statements)`.
fn workflow_sql(workflow: &Any<'_>) -> PyResult<String> {
    Ok(items(&workflow.getattr("statements")?)?
        .iter()
        .map(|statement| statement.getattr("sql")?.extract::<String>())
        .collect::<PyResult<Vec<String>>>()?
        .join("\n"))
}

fn write_staged_workflow(
    staged_root: &Any<'_>,
    workflow: &Any<'_>,
    is_template: bool,
    execution_json: &Any<'_>,
) -> PyResult<()> {
    let py: Python<'_> = staged_root.py();
    let steps_root: Any<'_> = join(staged_root, "steps")?;
    steps_root.call_method0("mkdir")?;
    write_text(
        &join(staged_root, "plan.json")?,
        &workflow.getattr("plan_json")?,
    )?;
    let suffix: &str = if is_template { ".sql.template" } else { ".sql" };
    for statement in items(&workflow.getattr("statements")?)? {
        let filename: String = format!(
            "{:04}_{}{suffix}",
            statement.getattr("sequence")?.extract::<i128>()?,
            statement.getattr("step_id")?.str()?
        );
        write_text(&join(&steps_root, &filename)?, &statement.getattr("sql")?)?;
    }
    let workflow_name: &str = if is_template {
        "workflow.template.sql"
    } else {
        "workflow.sql"
    };
    write_text(
        &join(staged_root, workflow_name)?,
        PyString::new(py, &workflow_sql(workflow)?).as_any(),
    )?;
    if !execution_json.is_none() {
        write_text(&join(staged_root, "execution.json")?, execution_json)?;
    }
    Ok(())
}

/// Replace one owner's complete artifact directory without exposing partial files.
fn publish_workflow_artifact<'py>(
    target_dir: &Any<'py>,
    owner: &str,
    workflow: &Any<'py>,
    is_template: bool,
    execution_json: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = target_dir.py();
    let run_dir: Any<'py> = join(target_dir, "run")?;
    let kwargs: Bound<'py, PyDict> = PyDict::new(py);
    kwargs.set_item("parents", true)?;
    kwargs.set_item("exist_ok", true)?;
    run_dir.call_method("mkdir", (), Some(&kwargs))?;
    let kwargs: Bound<'py, PyDict> = PyDict::new(py);
    kwargs.set_item("prefix", format!(".{owner}-staging-"))?;
    kwargs.set_item("dir", &run_dir)?;
    let staged_root: Any<'py> = attr(py, "pathlib", "Path")?
        .call1((attr(py, "tempfile", "mkdtemp")?.call((), Some(&kwargs))?,))?;
    let artifact_root: Any<'py> = join(&run_dir, owner)?;
    let previous_root: Any<'py> = join(&run_dir, &format!(".{owner}-previous"))?;
    let rmtree = |path: &Any<'py>| -> PyResult<()> {
        attr(py, "shutil", "rmtree")?.call1((path,))?;
        Ok(())
    };
    let replace = |source: &Any<'py>, target: &Any<'py>| -> PyResult<()> {
        attr(py, "os", "replace")?.call1((source, target))?;
        Ok(())
    };
    let published: PyResult<()> = (|| {
        write_staged_workflow(&staged_root, workflow, is_template, execution_json)?;
        if exists(&previous_root)? {
            rmtree(&previous_root)?;
        }
        if exists(&artifact_root)? {
            replace(&artifact_root, &previous_root)?;
        }
        if let Err(error) = replace(&staged_root, &artifact_root) {
            if !error.is_instance_of::<pyo3::exceptions::PyOSError>(py) {
                return Err(error);
            }
            let restored: PyResult<()> = (|| {
                if exists(&previous_root)? {
                    replace(&previous_root, &artifact_root)?;
                }
                Ok(())
            })();
            return Err(match restored {
                Ok(()) => error,
                Err(raised) => raised_during(py, raised, error),
            });
        }
        if exists(&previous_root)? {
            rmtree(&previous_root)?;
        }
        Ok(())
    })();
    let cleaned: PyResult<()> = (|| {
        if exists(&staged_root)? {
            rmtree(&staged_root)?;
        }
        Ok(())
    })();
    match (published, cleaned) {
        (Ok(()), Ok(())) => Ok(artifact_root),
        (Err(error), Ok(())) => Err(error),
        (Ok(()), Err(raised)) => Err(raised),
        (Err(error), Err(raised)) => Err(raised_during(py, raised, error)),
    }
}

/// Publish exact workflow bytes before granting permission to execute them.
#[pyfunction]
#[pyo3(signature = (*, target_dir, workflow, execution_json=None))]
fn publish_build_workflow<'py>(
    py: Python<'py>,
    target_dir: Any<'py>,
    workflow: Any<'py>,
    execution_json: Option<Any<'py>>,
) -> PyResult<Any<'py>> {
    let execution_json: Any<'py> = execution_json.unwrap_or_else(|| py.None().into_bound(py));
    let artifact_root: Any<'py> =
        publish_workflow_artifact(&target_dir, "build", &workflow, false, &execution_json)?;
    let digest: String = sha256_hex(&workflow_sql(&workflow)?);
    build(
        py,
        WORKFLOW_MODELS,
        "PublishedBuildWorkflow",
        &[
            ("workflow", workflow),
            ("artifact_root", artifact_root),
            ("workflow_sha256", PyString::new(py, &digest).into_any()),
        ],
    )
}

/// Publish exact plan workflow bytes without granting execution capability.
#[pyfunction]
#[pyo3(signature = (*, target_dir, workflow, is_template=false))]
fn publish_plan_workflow(
    py: Python<'_>,
    target_dir: Any<'_>,
    workflow: Any<'_>,
    is_template: bool,
) -> PyResult<()> {
    publish_workflow_artifact(
        &target_dir,
        "plan",
        &workflow,
        is_template,
        &py.None().into_bound(py),
    )?;
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(publish_build_workflow, module)?)?;
    module.add_function(wrap_pyfunction!(publish_plan_workflow, module)?)
}
