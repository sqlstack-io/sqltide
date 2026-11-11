//! Planner warnings that need a point-in-time warehouse read: rebuild subtrees whose replay
//! source is empty, and Kafka tables consuming as a different consumer group than the project
//! declares. Reads go through the Python adapter connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList};
use sqltide_executor::sources::{ConsumerGroupDrift, consumer_group_drift};
use sqltide_planner::cli_style::CliStyle;

use crate::direct_build::{Value, build, class, items, replace, text, tuple};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const CONSUMER_GROUP_DRIFT: &str = "consumer_group_drift";
const CONSUMER_GROUP_WARNINGS_KEY: &str = "consumer_group_warnings";

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

/// The `consumer_group_warnings` entries for one plan: each managed Kafka table in the
/// catalog the plan read that consumes as a different group than the project declares. Empty
/// when there is none or the plan read no catalog.
pub(crate) fn consumer_group_warning_items<'py>(
    analysis: &Value<'py>,
    catalog: &Value<'py>,
    database: &Value<'py>,
) -> PyResult<Bound<'py, PyList>> {
    let py: Python<'py> = analysis.py();
    let items: Bound<'py, PyList> = PyList::empty(py);
    if catalog.is_none() || database.is_none() {
        return Ok(items);
    }
    for (source_name, drift) in
        consumer_group_drifts(analysis, catalog, &database.extract::<String>()?)?
    {
        let warning: Bound<'py, PyDict> = PyDict::new(py);
        warning.set_item("warning_code", CONSUMER_GROUP_DRIFT)?;
        warning.set_item("message", drift.message())?;
        warning.set_item("source_name", source_name)?;
        warning.set_item("table_name", &drift.table_name)?;
        warning.set_item("live_consumer_group", &drift.live_group)?;
        warning.set_item("declared_consumer_group", &drift.declared_group)?;
        items.append(warning)?;
    }
    Ok(items)
}

/// A rendered plan JSON object with `consumer_group_warnings` appended as its last member and
/// every other byte unchanged.
pub(crate) fn with_consumer_group_warnings_json(
    plan_json: &str,
    warnings: &Bound<'_, PyList>,
) -> PyResult<String> {
    let py: Python<'_> = warnings.py();
    let indent: usize = top_level_indent(plan_json);
    let keywords: Bound<'_, PyDict> = PyDict::new(py);
    if indent > 0 {
        keywords.set_item("indent", indent)?;
    }
    let value: String = py
        .import("json")?
        .getattr("dumps")?
        .call((warnings,), Some(&keywords))?
        .extract()?;
    Ok(with_top_level_key(
        plan_json,
        CONSUMER_GROUP_WARNINGS_KEY,
        &value,
    ))
}

/// The plan text followed by a warning line for each consumer group drift the plan's JSON
/// records under `consumer_group_warnings`; unchanged when there is none.
pub(crate) fn with_consumer_group_drift_text<'py>(
    plan_text: Value<'py>,
    plan_json: &Value<'py>,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = plan_text.py();
    let payload: Value<'py> = py.import("json")?.getattr("loads")?.call1((plan_json,))?;
    let mut messages: Vec<String> = Vec::new();
    let mut lists: Vec<Value<'py>> = vec![payload.clone()];
    for mode in ["virtual", "direct"] {
        if let Some(nested) = payload.downcast::<PyDict>()?.get_item(mode)? {
            lists.push(nested);
        }
    }
    for holder in lists {
        let Ok(holder) = holder.downcast_into::<PyDict>() else {
            continue;
        };
        let Some(warnings) = holder.get_item(CONSUMER_GROUP_WARNINGS_KEY)? else {
            continue;
        };
        for warning in warnings.try_iter()? {
            let message: String = warning?.get_item("message")?.extract()?;
            if !messages.contains(&message) {
                messages.push(message);
            }
        }
    }
    if messages.is_empty() {
        return Ok(plan_text);
    }
    let style = CliStyle {
        use_color: crate::cli::style::terminal_style(py)?.use_color,
    };
    let mut lines: Vec<String> = vec![plan_text.extract()?, String::new()];
    for message in &messages {
        lines.push(style.warning(&format!("warning  {message}")));
    }
    Ok(lines.join("\n").into_pyobject(py)?.into_any())
}

/// The indentation of a JSON object's top-level keys: the leading spaces of its second line, or
/// 0 when the object is on one line.
fn top_level_indent(json: &str) -> usize {
    json.lines()
        .nth(1)
        .map_or(0, |line| line.len() - line.trim_start_matches(' ').len())
}

/// `json`, a JSON object, with `"key": value` appended as its last member. Every existing byte
/// is kept; `value` is already rendered at the object's indentation for its first line, and
/// its later lines are shifted one level so they nest under the key.
fn with_top_level_key(json: &str, key: &str, value: &str) -> String {
    let Some(end) = json.rfind('}') else {
        return json.to_owned();
    };
    let indent: usize = top_level_indent(json);
    let head: &str = json[..end].trim_end();
    let tail: &str = &json[end..];
    let separator: &str = if head.ends_with('{') { "" } else { "," };
    let nested: String = value.replace('\n', &format!("\n{}", " ".repeat(indent)));
    if indent == 0 {
        let space: &str = if separator.is_empty() { "" } else { " " };
        return format!("{head}{separator}{space}\"{key}\": {nested}{tail}");
    }
    format!(
        "{head}{separator}\n{}\"{key}\": {nested}\n{tail}",
        " ".repeat(indent)
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(add_empty_replay_source_warnings, module)?)
}

#[cfg(test)]
mod tests {
    use super::with_top_level_key;

    #[test]
    fn appends_a_key_and_keeps_every_existing_byte() {
        assert_eq!(
            with_top_level_key("{\n  \"mode\": \"direct\"\n}\n", "warnings", "[]"),
            "{\n  \"mode\": \"direct\",\n  \"warnings\": []\n}\n"
        );
        assert_eq!(
            with_top_level_key(
                "{\n  \"mode\": \"direct\"\n}\n",
                "warnings",
                "[\n  {\n    \"a\": 1\n  }\n]"
            ),
            "{\n  \"mode\": \"direct\",\n  \"warnings\": [\n    {\n      \"a\": 1\n    }\n  ]\n}\n"
        );
        assert_eq!(
            with_top_level_key("{\"mode\": \"direct\"}", "warnings", "[]"),
            "{\"mode\": \"direct\", \"warnings\": []}"
        );
    }
}
