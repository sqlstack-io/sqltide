//! Planner warnings that need a point-in-time warehouse read: rebuild subtrees whose replay
//! source is empty, and Kafka tables consuming as a different consumer group than the project
//! declares. Reads go through the Python adapter connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt};
use sqltide_executor::sources::{ConsumerGroupDrift, consumer_group_drift};
use sqltide_planner::cli_style::CliStyle;

use crate::direct_build::{Value, build, class, items, replace, text, tuple};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const CONSUMER_GROUP_DRIFT: &str = "consumer_group_drift";

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

/// `(source name, drift)` for every realized managed source whose live table in `database`
/// consumes as a different consumer group than the project declares, by source name.
pub(crate) fn consumer_group_drifts(
    analysis: &Value<'_>,
    catalog: &Value<'_>,
    database: &str,
) -> PyResult<Vec<(String, ConsumerGroupDrift)>> {
    let py: Python<'_> = analysis.py();
    let managed_source: Value<'_> = class(py, ADAPTER_MODELS, "AdapterManagedSource")?;
    let mut drifts: Vec<(String, ConsumerGroupDrift)> = Vec::new();
    for entry in analysis
        .getattr("realized_project")?
        .getattr("resources_by_logical_key")?
        .call_method0("items")?
        .try_iter()?
    {
        let entry: Value<'_> = entry?;
        let source_name: String = entry.get_item(0)?.getattr("name")?.extract()?;
        for resource in items(&entry.get_item(1)?)? {
            if !resource.is_instance(&managed_source)? {
                continue;
            }
            let table_name: String = resource.getattr("name")?.extract()?;
            let relation: Value<'_> = catalog.call_method1("relation", (&table_name,))?;
            if relation.is_none() {
                continue;
            }
            let settings: Vec<(String, String)> = relation.getattr("settings")?.extract()?;
            let declared: String = resource.getattr("consumer_group")?.extract()?;
            if let Some(drift) = consumer_group_drift(&table_name, &declared, database, &settings) {
                drifts.push((source_name.clone(), drift));
            }
        }
    }
    drifts.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(drifts)
}

/// The plan text and JSON with a consumer group drift warning for each Kafka table that
/// consumes as a different group than the project declares, read from the catalogs the plan
/// already loaded; unchanged when none does.
pub(crate) fn with_consumer_group_drift_warnings<'py>(
    analysis: &Value<'py>,
    preparation: &Value<'py>,
    plan_text: Value<'py>,
    plan_json: Value<'py>,
) -> PyResult<(Value<'py>, Value<'py>)> {
    let py: Python<'py> = analysis.py();
    let previews: Vec<Value<'py>> = if preparation.hasattr("virtual")? {
        vec![
            preparation.getattr("virtual")?.getattr("preview")?,
            preparation.getattr("direct")?.getattr("preview")?,
        ]
    } else {
        vec![preparation.getattr("preview")?]
    };
    let mut drifts: Vec<(String, ConsumerGroupDrift)> = Vec::new();
    for preview in previews {
        let catalog: Value<'py> = if preview.hasattr("warehouse_snapshot")? {
            preview.getattr("warehouse_snapshot")?.getattr("catalog")?
        } else {
            preview.getattr("target_catalog")?
        };
        if catalog.is_none() {
            continue;
        }
        let database: String = preview.getattr("database")?.extract()?;
        for drift in consumer_group_drifts(analysis, &catalog, &database)? {
            if !drifts.contains(&drift) {
                drifts.push(drift);
            }
        }
    }
    if drifts.is_empty() {
        return Ok((plan_text, plan_json));
    }
    let style = CliStyle {
        use_color: crate::cli::style::terminal_style(py)?.use_color,
    };
    let mut lines: Vec<String> = vec![plan_text.extract()?, String::new()];
    for (_, drift) in &drifts {
        lines.push(style.warning(&format!("warning  {}", drift.message())));
    }
    let json = py.import("json")?;
    let payload: Value<'py> = json.getattr("loads")?.call1((&plan_json,))?;
    let mut warnings: Vec<Value<'py>> = Vec::new();
    for (source_name, drift) in &drifts {
        let warning: Bound<'py, PyDict> = PyDict::new(py);
        warning.set_item("warning_code", CONSUMER_GROUP_DRIFT)?;
        warning.set_item("message", drift.message())?;
        warning.set_item("source_name", source_name)?;
        warning.set_item("table_name", &drift.table_name)?;
        warning.set_item("live_consumer_group", &drift.live_group)?;
        warning.set_item("declared_consumer_group", &drift.declared_group)?;
        warnings.push(warning.into_any());
    }
    payload.set_item(
        "consumer_group_warnings",
        pyo3::types::PyList::new(py, warnings)?,
    )?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("indent", 2)?;
    let plan_json: String = json
        .getattr("dumps")?
        .call((payload,), Some(&keywords))?
        .extract()?;
    Ok((
        lines.join("\n").into_pyobject(py)?.into_any(),
        format!("{plan_json}\n").into_pyobject(py)?.into_any(),
    ))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(add_empty_replay_source_warnings, module)?)
}
