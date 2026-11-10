//! Live-versus-compiled model drift evidence for the catalog UI.

use std::collections::{BTreeSet, HashMap, HashSet};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use regex::{NoExpand, Regex, RegexBuilder};
use sqltide_planner::direct::split_lines;
use sqltide_planner::unified_diff::unified_diff;
use sqltide_sqltext::python_text::python_isspace;

use super::support::attr;
use crate::py_kwargs;

const DRIFT: &str = "drift";
const IN_SYNC: &str = "in_sync";
const UNAVAILABLE: &str = "unavailable";
const TABLE_KIND: &str = "table";
const VIEW_KIND: &str = "view";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const DEFAULT_INDEX_GRANULARITY: (&str, &str) = ("index_granularity", "8192");
/// Python's `\s`: Unicode whitespace plus the separators U+001C to U+001F.
const SPACE: &str = r"[\s\x1C-\x1F]";

type Any<'py> = Bound<'py, PyAny>;

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

fn is<'py>(value: &Any<'py>, class: &str) -> PyResult<bool> {
    value.is_instance(&attr(value.py(), ADAPTER_MODELS, class)?)
}

fn text(value: &Any<'_>) -> PyResult<String> {
    Ok(value.str()?.to_str()?.to_owned())
}

fn first_of<'py>(resources: &[Any<'py>], classes: &[&str]) -> PyResult<Option<Any<'py>>> {
    for resource in resources {
        for class in classes {
            if is(resource, class)? {
                return Ok(Some(resource.clone()));
            }
        }
    }
    Ok(None)
}

fn unavailable_payload<'py>(py: Python<'py>, relation_name: &Any<'py>) -> PyResult<Any<'py>> {
    let unavailable = || py_kwargs!(py; status = UNAVAILABLE, changes = PyList::empty(py), unifiedDiff = none(py));
    let query = unavailable()?;
    query.set_item("relationName", relation_name)?;
    Ok(py_kwargs!(py;
        comparison = "live_vs_current_compiled",
        status = UNAVAILABLE,
        message = format!("Relation {} is missing; no live comparison is possible.", relation_name.str()?),
        liveDdl = none(py),
        schema = unavailable()?,
        query = query,
        physicalConfiguration = unavailable()?,
    )?
    .into_any())
}

/// `{name: (type, default)}` in insertion order with later names replacing earlier ones.
fn column_map<'py>(entries: Vec<(String, Any<'py>)>) -> HashMap<String, Any<'py>> {
    entries.into_iter().collect()
}

fn column_value<'py>(py: Python<'py>, value: Option<&Any<'py>>) -> PyResult<Any<'py>> {
    let Some(value) = value else {
        return Ok(none(py));
    };
    let type_name = value.get_item(0)?;
    let default = value.get_item(1)?;
    if default.is_none() {
        return Ok(type_name);
    }
    Ok(format!("{} DEFAULT {}", type_name.str()?, default.str()?).into_pyobject(py)?.into_any())
}

fn schema_payload<'py>(py: Python<'py>, desired_columns: &Any<'py>, live_columns: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let mut desired: Vec<(String, Any<'py>)> = Vec::new();
    for column in desired_columns.try_iter()? {
        let column = column?;
        let default = match column.getattr("default_expression") {
            Ok(value) => value,
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => none(py),
            Err(error) => return Err(error),
        };
        let value = PyTuple::new(py, [column.getattr("type")?.str()?.into_any(), default])?.into_any();
        desired.push((text(&column.getattr("name")?)?, value));
    }
    let mut live: Vec<(String, Any<'py>)> = Vec::new();
    for column in live_columns.try_iter()? {
        let column = column?;
        let value = PyTuple::new(py, [column.getattr("type")?, column.getattr("default_expression")?])?.into_any();
        live.push((column.getattr("name")?.extract()?, value));
    }
    let names: BTreeSet<String> = desired.iter().chain(live.iter()).map(|(name, _)| name.clone()).collect();
    let (desired, live) = (column_map(desired), column_map(live));
    let mut changes: Vec<Any<'py>> = Vec::new();
    for name in names {
        let (desired_value, live_value) = (desired.get(&name), live.get(&name));
        if let (Some(left), Some(right)) = (desired_value, live_value)
            && left.eq(right)?
        {
            continue;
        }
        let change: &str = if live_value.is_none() {
            "added"
        } else if desired_value.is_none() {
            "removed"
        } else {
            "changed"
        };
        changes.push(
            py_kwargs!(py;
                column = name,
                live = column_value(py, live_value)?,
                compiled = column_value(py, desired_value)?,
                change = change,
            )?
            .into_any(),
        );
    }
    py_kwargs!(py; status = if changes.is_empty() { IN_SYNC } else { DRIFT }, changes = PyList::new(py, changes)?)
}

fn pattern(source: &str) -> Result<Regex, regex::Error> {
    RegexBuilder::new(source).case_insensitive(true).build()
}

/// Rewrite `CAST(col, 'T') AS col` and `col::T AS col` to `col AS col` for every projection
/// column, as `re.sub(..., flags=re.IGNORECASE)` did.
fn normalize_projection_coercions(py: Python<'_>, query: &str, column_names: &[String]) -> PyResult<String> {
    let mut normalized: String = query.to_owned();
    for column_name in column_names {
        let replacement: String = format!("{column_name} AS {column_name}");
        let escaped: String = regex::escape(column_name);
        let sources: [String; 2] = [
            format!(r"CAST\({SPACE}*{escaped}{SPACE}*,{SPACE}*'[^']+'{SPACE}*\){SPACE}+AS{SPACE}+{escaped}"),
            format!(r"{escaped}{SPACE}*::{SPACE}*[A-Za-z0-9_]+(?:\([^)]*\))?{SPACE}+AS{SPACE}+{escaped}"),
        ];
        for source in sources {
            if replacement.contains('\\') {
                // A backslash makes the replacement a template with its own escape rules.
                let python_escaped: String = attr(py, "re", "escape")?.call1((column_name,))?.extract()?;
                let python_source: String = source.replace(SPACE, r"\s").replace(&escaped, &python_escaped);
                normalized = attr(py, "re", "sub")?
                    .call(
                        (python_source, &replacement, &normalized),
                        Some(&py_kwargs!(py; flags = attr(py, "re", "IGNORECASE")?)?),
                    )?
                    .extract()?;
                continue;
            }
            let compiled: Regex = pattern(&source).map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
            normalized = compiled.replace_all(&normalized, NoExpand(&replacement)).into_owned();
        }
    }
    Ok(normalized)
}

fn query_payload<'py>(
    py: Python<'py>,
    desired_query: &str,
    live_query: Option<String>,
    relation_name: &Any<'py>,
    relation_available: bool,
    column_names: &[String],
) -> PyResult<Bound<'py, PyDict>> {
    let live_query: String = match live_query {
        Some(live_query) if relation_available => live_query,
        _ => {
            return py_kwargs!(py;
                status = UNAVAILABLE,
                relationName = relation_name,
                live = none(py),
                compiled = desired_query,
                unifiedDiff = none(py),
            );
        }
    };
    let same: bool = normalize_projection_coercions(py, desired_query, column_names)?
        == normalize_projection_coercions(py, &live_query, column_names)?;
    let diff: Option<String> = (!same).then(|| {
        unified_diff(
            &split_lines(&live_query, false),
            &split_lines(desired_query, false),
            "live warehouse",
            "current compiled",
            "",
        )
        .join("\n")
    });
    py_kwargs!(py;
        status = if same { IN_SYNC } else { DRIFT },
        relationName = relation_name,
        live = live_query,
        compiled = desired_query,
        unifiedDiff = diff,
    )
}

fn rendered_query<'py>(analysis: &Any<'py>, resource: Option<&Any<'py>>, database: &Any<'py>) -> PyResult<String> {
    let Some(resource) = resource else {
        return Ok(String::new());
    };
    let py: Python<'py> = analysis.py();
    let definition_sql = analysis
        .getattr("adapter_profile")?
        .call_method("render_resource", (), Some(&py_kwargs!(py; resource = resource, database = database)?))?;
    let query_sql = attr(py, "sqltide._native", "analyze_catalog_sql")?
        .call((), Some(&py_kwargs!(py; sql = definition_sql, dialect = "clickhouse")?))?
        .getattr("query_sql")?;
    if query_sql.is_truthy()? { query_sql.extract() } else { Ok(String::new()) }
}

/// `_physical_value`: "not configured" for `None` or `()`, tuples joined, and other values
/// whitespace-collapsed, stripped of a trailing `()` and lowercased.
fn physical_value(value: &Any<'_>) -> PyResult<String> {
    if value.is_none() {
        return Ok("not configured".to_owned());
    }
    if let Ok(items) = value.cast::<PyTuple>() {
        if items.is_empty() {
            return Ok("not configured".to_owned());
        }
        let mut parts: Vec<String> = Vec::new();
        for item in items.iter() {
            if let Ok(pair) = item.cast::<PyTuple>() {
                parts.push(pair.iter().map(|part| text(&part)).collect::<PyResult<Vec<_>>>()?.join("="));
            } else {
                parts.push(text(&item)?);
            }
        }
        return Ok(parts.join(", "));
    }
    let raw: String = text(value)?;
    let collapsed: String = raw.split(python_isspace).filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" ");
    Ok(collapsed.strip_suffix("()").unwrap_or(&collapsed).to_lowercase())
}

fn physical_payload<'py>(desired: &Any<'py>, live: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = desired.py();
    let is_table: bool = is(desired, "AdapterTable")?;
    let live_kind: &str = if text(&live.getattr("engine")?)?.to_lowercase() == VIEW_KIND { VIEW_KIND } else { TABLE_KIND };
    let empty = || PyTuple::empty(py).into_any();
    let (engine, order_by, partition_by, ttl, settings) = if is_table {
        (
            desired.getattr("engine")?,
            desired.getattr("order_by")?,
            desired.getattr("partition_by")?,
            desired.getattr("ttl")?,
            desired.getattr("settings")?,
        )
    } else {
        (none(py), empty(), none(py), none(py), empty())
    };
    let mut desired_setting_names: HashSet<String> = HashSet::new();
    for setting in settings.try_iter()? {
        desired_setting_names.insert(text(&setting?.get_item(0)?)?);
    }
    let default_granularity = PyTuple::new(py, [DEFAULT_INDEX_GRANULARITY.0, DEFAULT_INDEX_GRANULARITY.1])?;
    let mut live_settings: Vec<Any<'py>> = Vec::new();
    for setting in live.getattr("settings")?.try_iter()? {
        let setting = setting?;
        if setting.eq(&default_granularity)? && !desired_setting_names.contains(&text(&setting.get_item(0)?)?) {
            continue;
        }
        live_settings.push(setting);
    }
    let live_or = |name: &str, fallback: Any<'py>| -> PyResult<Any<'py>> { if is_table { live.getattr(name) } else { Ok(fallback) } };
    let fields: [(&str, Any<'py>, Any<'py>); 7] = [
        ("Physical identity", desired.getattr("name")?, live.getattr("name")?),
        (
            "Relation kind",
            (if is_table { TABLE_KIND } else { VIEW_KIND }).into_pyobject(py)?.into_any(),
            live_kind.into_pyobject(py)?.into_any(),
        ),
        ("Engine", engine, live_or("storage_engine", none(py))?),
        ("ORDER BY", order_by, live_or("order_by", empty())?),
        ("PARTITION BY", partition_by, live_or("partition_by", none(py))?),
        ("TTL", ttl, live_or("ttl", none(py))?),
        ("Settings", settings, if is_table { PyTuple::new(py, live_settings)?.into_any() } else { empty() }),
    ];
    let mut drifted: bool = false;
    let mut changes: Vec<Any<'py>> = Vec::new();
    for (field, desired_value, live_value) in fields {
        let (compiled, current) = (physical_value(&desired_value)?, physical_value(&live_value)?);
        let status: &str = if current == compiled { IN_SYNC } else { DRIFT };
        drifted |= status == DRIFT;
        changes.push(py_kwargs!(py; field = field, live = current, compiled = compiled, status = status)?.into_any());
    }
    py_kwargs!(py; status = if drifted { DRIFT } else { IN_SYNC }, changes = PyList::new(py, changes)?)
}

/// Compare one current realized model with its live warehouse relations.
#[pyfunction]
#[pyo3(signature = (*, analysis, model, catalog))]
pub(crate) fn model_drift_payload<'py>(analysis: Any<'py>, model: Any<'py>, catalog: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let resources: Vec<Any<'py>> = analysis
        .getattr("realized_project")?
        .getattr("resources_by_logical_key")?
        .call_method1("get", (model.getattr("key")?, PyTuple::empty(py)))?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let desired_relation: Option<Any<'py>> = first_of(&resources, &["AdapterTable", "AdapterView"])?;
    let desired_writer: Option<Any<'py>> = first_of(&resources, &["AdapterMaterializedView"])?;
    let relations: Bound<'py, PyDict> = PyDict::new(py);
    for relation in catalog.getattr("relations")?.try_iter()? {
        let relation = relation?;
        relations.set_item(relation.getattr("name")?, relation)?;
    }
    let live_for = |resource: &Option<Any<'py>>| -> PyResult<Option<Any<'py>>> {
        match resource {
            None => Ok(None),
            Some(resource) => relations.get_item(resource.getattr("name")?),
        }
    };
    let live_relation: Option<Any<'py>> = live_for(&desired_relation)?;
    let live_writer: Option<Any<'py>> = live_for(&desired_writer)?;
    let (Some(desired_relation), Some(live_relation)) = (desired_relation.clone(), live_relation) else {
        let relation_name = match &desired_relation {
            None => model.getattr("relation_name")?,
            Some(relation) => relation.getattr("name")?,
        };
        return unavailable_payload(py, &relation_name);
    };
    let desired_is_table: bool = is(&desired_relation, "AdapterTable")?;
    let desired_columns = if desired_is_table { desired_relation.getattr("columns")? } else { model.getattr("output_columns")? };
    let schema = schema_payload(py, &desired_columns, &live_relation.getattr("columns")?)?;
    let query_resource: Option<Any<'py>> = match &desired_writer {
        Some(writer) => Some(writer.clone()),
        None if is(&desired_relation, "AdapterView")? => Some(desired_relation.clone()),
        None => None,
    };
    let desired_query: String =
        rendered_query(&analysis, query_resource.as_ref(), &catalog.getattr("identity")?.getattr("database")?)?;
    let live_query_value = match (&desired_writer, &live_writer) {
        (Some(_), Some(writer)) => writer.getattr("query_sql")?,
        _ => live_relation.getattr("query_sql")?,
    };
    let live_query: Option<String> = if live_query_value.is_none() { None } else { Some(live_query_value.extract()?) };
    let query_relation_name = desired_writer.as_ref().unwrap_or(&desired_relation).getattr("name")?;
    let mut column_names: Vec<String> = Vec::new();
    for column in model.getattr("output_columns")?.try_iter()? {
        column_names.push(column?.getattr("name")?.extract()?);
    }
    let query = query_payload(
        py,
        &desired_query,
        live_query,
        &query_relation_name,
        desired_writer.is_none() || live_writer.is_some(),
        &column_names,
    )?;
    let physical = physical_payload(&desired_relation, &live_relation)?;
    let mut drifted: bool = false;
    for part in [&schema, &query, &physical] {
        let status: String = text(&part.get_item("status")?.expect("every part has a status"))?;
        drifted |= status == DRIFT || status == UNAVAILABLE;
    }
    let mut live_ddl_parts: Vec<String> = Vec::new();
    for relation in [Some(&live_relation), live_writer.as_ref()].into_iter().flatten() {
        let definition = relation.getattr("definition_sql")?;
        if definition.is_truthy()? {
            live_ddl_parts.push(definition.extract()?);
        }
    }
    let live_ddl: String = live_ddl_parts.join("\n\n");
    Ok(py_kwargs!(py;
        comparison = "live_vs_current_compiled",
        status = if drifted { DRIFT } else { IN_SYNC },
        message = if drifted {
            "Live warehouse differs from the current compiled definition."
        } else {
            "Live warehouse matches the current compiled definition."
        },
        liveDdl = (!live_ddl.is_empty()).then_some(live_ddl),
        schema = schema,
        query = query,
        physicalConfiguration = physical,
    )?
    .into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(model_drift_payload, module)?)
}
