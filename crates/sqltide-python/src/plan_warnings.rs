//! Planner warnings that need a point-in-time warehouse read: rebuild subtrees whose replay
//! source is empty. Counts go through the Python adapter connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt};

use crate::direct_build::{Value, build, class, items, replace, text, tuple};

const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const EMPTY_REPLAY_SOURCE: &str = "empty_replay_source";

/// `int(rows[0][0])` of `SELECT count()` over a cataloged relation, or `None` when the relation
/// is not cataloged, has disappeared, or returned no rows.
fn point_in_time_row_count<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    catalog: &Value<'py>,
    database: &Value<'py>,
    table_name: &Value<'py>,
) -> PyResult<Option<Value<'py>>> {
    if catalog.call_method1("relation", (table_name,))?.is_none() {
        return Ok(None);
    }
    let sql: String = format!(
        "SELECT count() FROM {}.{}",
        database.str()?,
        table_name.str()?
    );
    let rows: Value<'py> = match client.call_method1("query", (sql,)) {
        Ok(result) => result.getattr("rows")?,
        Err(error)
            if error.is_instance(
                py,
                &class(py, ADAPTER_EXCEPTIONS, "AdapterRelationNotFoundError")?,
            ) =>
        {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    if !rows.is_truthy()? {
        return Ok(None);
    }
    Ok(Some(
        py.get_type::<PyInt>()
            .call1((rows.get_item(0)?.get_item(0)?,))?,
    ))
}

/// The plan with an `empty_replay_source` warning for every rebuild subtree whose replay
/// source is cataloged and empty, replacing that root's earlier warning, in key order.
#[pyfunction]
#[pyo3(signature = (*, client, catalog, database, desired_state, plan))]
fn add_empty_replay_source_warnings<'py>(
    py: Python<'py>,
    client: &Value<'py>,
    catalog: &Value<'py>,
    database: &Value<'py>,
    desired_state: &Value<'py>,
    plan: &Value<'py>,
) -> PyResult<Value<'py>> {
    let _ = desired_state;
    let warning_by_root_name: Bound<'py, PyDict> = PyDict::new(py);
    for warning in items(&plan.getattr("warnings")?)? {
        warning_by_root_name.set_item(warning.getattr("root_key")?.getattr("name")?, warning)?;
    }
    for subtree in items(&plan.getattr("rebuild_subtrees")?)? {
        let source_name: Value<'py> = subtree.getattr("upstream_boundary_key")?.getattr("name")?;
        let source_count: Option<Value<'py>> =
            point_in_time_row_count(py, client, catalog, database, &source_name)?;
        // `replay_source_row_count != 0`: only a cataloged, empty source warns.
        match &source_count {
            Some(count) if count.eq(0)? => {}
            _ => continue,
        }
        let root_key: Value<'py> = subtree.getattr("root_key")?;
        let root_name: Value<'py> = root_key.getattr("name")?;
        let active_count: Option<Value<'py>> =
            point_in_time_row_count(py, client, catalog, database, &root_name)?;
        let mut message: String = format!(
            "replay source {} is empty; staged outputs for this subtree may also be empty",
            source_name.str()?
        );
        if let Some(count) = active_count
            && count.gt(0)?
        {
            message.push_str(&format!(
                " (active target {} currently has {} rows)",
                root_name.str()?,
                count.str()?
            ));
        }
        warning_by_root_name.set_item(
            &root_name,
            build(
                py,
                PLANNER_MODELS,
                "PlannerWarning",
                &[
                    ("warning_code", text(py, EMPTY_REPLAY_SOURCE)),
                    ("message", text(py, &message)),
                    ("root_key", root_key),
                ],
            )?,
        )?;
    }
    let mut keyed: Vec<((String, String, String, String), Value<'py>)> = Vec::new();
    for warning in warning_by_root_name.values() {
        let key: Value<'py> = warning.getattr("root_key")?;
        let database: Value<'py> = key.getattr("database")?;
        keyed.push((
            (
                if database.is_truthy()? {
                    database.extract()?
                } else {
                    String::new()
                },
                key.getattr("object_type")?.str()?.extract()?,
                key.getattr("name")?.extract()?,
                warning.getattr("warning_code")?.extract()?,
            ),
            warning,
        ));
    }
    // Stable, like Python's `sorted`, so equal keys keep their insertion order.
    keyed.sort_by(|left, right| left.0.cmp(&right.0));
    replace(
        plan,
        &[(
            "warnings",
            tuple(py, keyed.into_iter().map(|(_, warning)| warning).collect())?,
        )],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(add_empty_replay_source_warnings, module)?)
}
