//! The ClickHouse catalog: one neutral snapshot of a database's relations from its system
//! tables, the external relations that depend on target-owned names, structural resource
//! matching and scheduled refresh states. Warehouse reads go through the Python adapter
//! connection.

use std::collections::{HashMap, HashSet};

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_clickhouse::resources::database_scoped_consumer_group;
use sqltide_planner::direct::sha256_hex;
use sqltide_sqlanalysis::{CatalogAnalysis, SqlDialect, analyze_catalog, expression_list};
use sqltide_sqltext::python_text::python_isspace;

use crate::analysis::analysis_error;
use crate::model_sql::chained;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const DIALECT: &str = "clickhouse";
const VIEW_ENGINE: &str = "View";
const MATERIALIZED_VIEW_ENGINE: &str = "MaterializedView";
const KAFKA_ENGINE: &str = "Kafka";
const ZERO_UUID: &str = "00000000-0000-0000-0000-000000000000";
const DEFAULT_INDEX_GRANULARITY: [&str; 2] = ["index_granularity", "8192"];
const CATALOG_LOAD_ATTEMPTS: u32 = 3;
const CATALOG_RETRY_DELAY_SECONDS: f64 = 0.05;
const EMPTY_KEY_EXPRESSIONS: [&str; 2] = ["", "tuple()"];

type Keywords<'a, 'py> = &'a [(&'a str, Bound<'py, PyAny>)];

pub(crate) fn build<'py>(
    py: Python<'py>,
    module: &str,
    class: &str,
    keywords: Keywords<'_, 'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in keywords {
        arguments.set_item(name, value)?;
    }
    py.import(module)?
        .getattr(class)?
        .call((), Some(&arguments))
}

pub(crate) fn object<'py, T: IntoPyObjectExt<'py>>(
    py: Python<'py>,
    value: T,
) -> PyResult<Bound<'py, PyAny>> {
    value.into_bound_py_any(py)
}

/// `str.strip()` with Python's whitespace definition.
pub(crate) fn strip(text: &str) -> &str {
    text.trim_matches(python_isspace)
}

pub(crate) fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

pub(crate) fn result_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import(ADAPTER_EXCEPTIONS)?
            .getattr("AdapterResultError")?
            .call1((message,))?,
    ))
}

/// `AdapterResultError(f"{prefix}: {error}") from None` while handling the SQL analysis error.
fn parse_error(py: Python<'_>, prefix: &str, failure: PyErr) -> PyResult<PyErr> {
    let context: Bound<'_, PyAny> = failure.value(py).clone().into_any();
    let error: Bound<'_, PyAny> = py
        .import(ADAPTER_EXCEPTIONS)?
        .getattr("AdapterResultError")?
        .call1((format!("{prefix}: {}", context.str()?),))?;
    chained(&error, &context, None)?;
    Ok(PyErr::from_value(error))
}

pub(crate) fn analyze(py: Python<'_>, sql: &str) -> PyResult<CatalogAnalysis> {
    analyze_catalog(sql, &SqlDialect::new(DIALECT)).map_err(|error| {
        parse_error(
            py,
            "ClickHouse catalog SQL could not be parsed",
            analysis_error(error),
        )
        .unwrap_or_else(|error| error)
    })
}

/// `'value'` as one ClickHouse string literal.
fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "''"))
}

/// Quote one ClickHouse string literal without changing its value.
#[pyfunction]
fn quote_clickhouse_sql_string(value: &str) -> String {
    quote(value)
}

fn sorting_key(py: Python<'_>, value: &str) -> PyResult<Vec<String>> {
    let normalized: &str = strip(value);
    if normalized.is_empty() {
        return Ok(Vec::new());
    }
    expression_list(normalized, &SqlDialect::new(DIALECT)).map_err(|error| {
        parse_error(
            py,
            "ClickHouse sorting key could not be parsed",
            analysis_error(error),
        )
        .unwrap_or_else(|error| error)
    })
}

/// Canonical SQL, distinct physical source names and a direct stable binding of one relation
/// query.
type QueryDetails = (Option<String>, Vec<String>, Option<String>);

fn query_details(py: Python<'_>, engine: &str, value: &str) -> PyResult<QueryDetails> {
    let normalized: &str = strip(value);
    if normalized.is_empty() {
        return Ok((None, Vec::new(), None));
    }
    let analysis: CatalogAnalysis = analyze(py, normalized)?;
    let mut seen: HashSet<String> = HashSet::new();
    let sources: Vec<String> = analysis
        .source_relations
        .iter()
        .filter(|source| seen.insert(source.name.clone()))
        .map(|source| source.name.clone())
        .collect();
    let binding: Option<String> = if engine == VIEW_ENGINE {
        analysis.direct_source.map(|source| source.name)
    } else {
        None
    };
    Ok((Some(analysis.canonical_sql), sources, binding))
}

struct RelationRow {
    name: String,
    engine: String,
    sorting_key: String,
    partition_key: String,
    create_table_query: String,
    as_select: String,
    uuid: String,
}

fn ownership_generation(uuid: &str, definition_sql: &str) -> String {
    if !uuid.is_empty() && uuid != ZERO_UUID {
        uuid.to_owned()
    } else {
        sha256_hex(definition_sql)
    }
}

fn catalog_relation<'py>(
    py: Python<'py>,
    row: &RelationRow,
    columns: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let definition: CatalogAnalysis = analyze(py, &row.create_table_query)?;
    let (query_sql, sources, binding) = query_details(py, &row.engine, &row.as_select)?;
    let partition_by: Option<&str> = if EMPTY_KEY_EXPRESSIONS.contains(&row.partition_key.as_str())
    {
        None
    } else {
        Some(&row.partition_key)
    };
    let order_by: Vec<String> = sorting_key(py, &row.sorting_key)?;
    build(
        py,
        ADAPTER_MODELS,
        "CatalogRelation",
        &[
            ("name", object(py, &row.name)?),
            ("engine", object(py, &row.engine)?),
            ("columns", columns),
            ("full_engine", object(py, definition.engine)?),
            ("order_by", PyTuple::new(py, order_by)?.into_any()),
            ("partition_by", object(py, partition_by)?),
            ("ttl", object(py, definition.ttl)?),
            (
                "settings",
                PyTuple::new(py, definition.settings)?.into_any(),
            ),
            ("definition_sql", object(py, &row.create_table_query)?),
            ("query_sql", object(py, query_sql)?),
            (
                "source_relation_names",
                PyTuple::new(py, sources)?.into_any(),
            ),
            (
                "target_relation_name",
                object(py, definition.target_relation.map(|target| target.name))?,
            ),
            ("stable_binding_name", object(py, binding)?),
            (
                "ownership_generation",
                object(py, ownership_generation(&row.uuid, &row.create_table_query))?,
            ),
        ],
    )
}

fn rows<'py>(result: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    result.getattr("rows")?.try_iter()?.collect()
}

fn warehouse_timezone(connection: &Bound<'_, PyAny>) -> PyResult<String> {
    let py: Python<'_> = connection.py();
    let timezone_rows = rows(&connection.call_method1("query", ("SELECT timezone()",))?)?;
    match timezone_rows.first() {
        Some(row) => text(&row.get_item(0)?),
        None => Err(result_error(
            py,
            "Could not determine ClickHouse server timezone".to_owned(),
        )?),
    }
}

/// `query_many(statement=..., decode=dict)`: rows as mappings through the connection's decoding.
pub(crate) fn query_mappings<'py>(
    connection: &Bound<'py, PyAny>,
    statement: String,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py: Python<'py> = connection.py();
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("statement", statement)?;
    keywords.set_item("decode", py.get_type::<PyDict>())?;
    connection
        .getattr("query_many")?
        .call((), Some(&keywords))?
        .try_iter()?
        .collect()
}

pub(crate) fn field(row: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    text(&row.get_item(name)?)
}

fn relation_rows(
    connection: &Bound<'_, PyAny>,
    quoted_database: &str,
) -> PyResult<Vec<RelationRow>> {
    query_mappings(
        connection,
        format!(
            "SELECT name, engine, sorting_key, partition_key, create_table_query, as_select, \
             toString(uuid) AS uuid FROM system.tables WHERE database = {quoted_database} \
             ORDER BY name"
        ),
    )?
    .iter()
    .map(|row| {
        Ok(RelationRow {
            name: field(row, "name")?,
            engine: field(row, "engine")?,
            sorting_key: field(row, "sorting_key")?,
            partition_key: field(row, "partition_key")?,
            create_table_query: field(row, "create_table_query")?,
            as_select: field(row, "as_select")?,
            uuid: field(row, "uuid")?,
        })
    })
    .collect()
}

fn catalog_columns<'py>(
    connection: &Bound<'py, PyAny>,
    quoted_database: &str,
) -> PyResult<HashMap<String, Vec<Bound<'py, PyAny>>>> {
    let py: Python<'py> = connection.py();
    let column_class: Bound<'py, PyAny> = py.import(ADAPTER_MODELS)?.getattr("CatalogColumn")?;
    let mut columns: HashMap<String, Vec<Bound<'py, PyAny>>> = HashMap::new();
    let statement: String = format!(
        "SELECT table, name, type, default_expression FROM system.columns \
         WHERE database = {quoted_database} ORDER BY table, position"
    );
    for row in query_mappings(connection, statement)? {
        let default: Bound<'py, PyAny> = row.get_item("default_expression")?;
        let default: Option<String> = if default.is_none() || default.eq("")? {
            None
        } else {
            Some(text(&default)?)
        };
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("name", field(&row, "name")?)?;
        keywords.set_item("type", field(&row, "type")?)?;
        keywords.set_item("default_expression", default)?;
        columns
            .entry(field(&row, "table")?)
            .or_default()
            .push(column_class.call((), Some(&keywords))?);
    }
    Ok(columns)
}

/// One fixed-query catalog snapshot, retried while ClickHouse still reports relations without
/// their CREATE statements.
#[pyfunction]
#[pyo3(signature = (*, connection, adapter_identity, database))]
fn load_clickhouse_catalog<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    adapter_identity: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let quoted_database: String = quote(database);
    let mut attempt: u32 = 1;
    loop {
        let timezone: String = warehouse_timezone(connection)?;
        let relations: Vec<RelationRow> = relation_rows(connection, &quoted_database)?;
        let mut incomplete: Vec<&str> = relations
            .iter()
            .filter(|row| strip(&row.create_table_query).is_empty())
            .map(|row| row.name.as_str())
            .collect();
        if incomplete.is_empty() {
            let columns = catalog_columns(connection, &quoted_database)?;
            let relations: Vec<Bound<'py, PyAny>> = relations
                .iter()
                .map(|row| {
                    let relation_columns: &[Bound<'py, PyAny>] =
                        columns.get(&row.name).map_or(&[], Vec::as_slice);
                    catalog_relation(py, row, PyTuple::new(py, relation_columns)?.into_any())
                })
                .collect::<PyResult<_>>()?;
            let identity: Bound<'py, PyAny> = build(
                py,
                ADAPTER_MODELS,
                "CatalogIdentity",
                &[
                    ("adapter", adapter_identity.clone()),
                    ("database", object(py, database)?),
                ],
            )?;
            return build(
                py,
                ADAPTER_MODELS,
                "CatalogSnapshot",
                &[
                    ("identity", identity),
                    ("warehouse_timezone", object(py, timezone)?),
                    ("relations", PyTuple::new(py, relations)?.into_any()),
                ],
            );
        }
        if attempt >= CATALOG_LOAD_ATTEMPTS {
            incomplete.sort_unstable();
            return Err(result_error(
                py,
                format!(
                    "ClickHouse catalog for database '{database}' remained incomplete after \
                     {CATALOG_LOAD_ATTEMPTS} attempts; missing CREATE statements for: {}",
                    incomplete.join(", ")
                ),
            )?);
        }
        // `time.sleep` releases the GIL and keeps the backoff observable to callers.
        py.import("time")?
            .call_method1("sleep", (CATALOG_RETRY_DELAY_SECONDS * f64::from(attempt),))?;
        attempt += 1;
    }
}

/// Every relation in a user database that depends on one of the target-owned relation names.
#[pyfunction]
#[pyo3(signature = (*, connection, database, relation_names))]
fn load_clickhouse_external_dependants<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
    relation_names: Vec<String>,
) -> PyResult<Bound<'py, PyTuple>> {
    let owned: HashSet<(&str, &str)> = relation_names
        .iter()
        .map(|name| (database, name.as_str()))
        .collect();
    let result: Bound<'_, PyAny> = connection.call_method1(
        "query",
        (
            "SELECT database, name, as_select, create_table_query FROM system.tables \
          WHERE database NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA') \
          AND (as_select != '' OR create_table_query != '') ORDER BY database, name",
        ),
    )?;
    let mut blocked: Vec<String> = Vec::new();
    for row in rows(&result)? {
        let relation_database: String = text(&row.get_item(0)?)?;
        let relation_name: String = text(&row.get_item(1)?)?;
        if owned.contains(&(relation_database.as_str(), relation_name.as_str())) {
            continue;
        }
        let select: String = text(&row.get_item(2)?)?;
        let mut dependencies = if strip(&select).is_empty() {
            Vec::new()
        } else {
            analyze(py, strip(&select))?.source_relations
        };
        let definition: String = text(&row.get_item(3)?)?;
        if !strip(&definition).is_empty()
            && let Some(target) = analyze(py, strip(&definition))?.target_relation
        {
            dependencies.push(target);
        }
        let depends: bool = dependencies.iter().any(|dependency| {
            let dependency_database: &str = match dependency.database.as_deref() {
                Some(name) if !name.is_empty() => name,
                _ => &relation_database,
            };
            owned.contains(&(dependency_database, dependency.name.as_str()))
        });
        if depends {
            blocked.push(format!("{relation_database}.{relation_name}"));
        }
    }
    PyTuple::new(py, blocked)
}

fn column_triples<'py>(columns: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = columns.py();
    let triples: Vec<Bound<'py, PyAny>> = columns
        .try_iter()?
        .map(|column| {
            let column: Bound<'py, PyAny> = column?;
            Ok(PyTuple::new(
                py,
                [
                    column.getattr("name")?,
                    column.getattr("type")?,
                    column.getattr("default_expression")?,
                ],
            )?
            .into_any())
        })
        .collect::<PyResult<_>>()?;
    Ok(PyTuple::new(py, triples)?.into_any())
}

fn is_a(value: &Bound<'_, PyAny>, class: &str) -> PyResult<bool> {
    value.is_instance(&value.py().import(ADAPTER_MODELS)?.getattr(class)?)
}

/// Whether one catalog relation matches every structural field of the resource the catalog
/// exposes.
#[pyfunction]
#[pyo3(signature = (*, resource, relation, database))]
fn clickhouse_catalog_resource_matches(
    py: Python<'_>,
    resource: &Bound<'_, PyAny>,
    relation: &Bound<'_, PyAny>,
    database: &str,
) -> PyResult<bool> {
    let managed_source: bool = is_a(resource, "AdapterManagedSource")?;
    let table: bool = is_a(resource, "AdapterTable")?;
    let engine: Bound<'_, PyAny> = relation.getattr("engine")?;
    if managed_source || table {
        let expected: Bound<'_, PyAny> = column_triples(&resource.getattr("columns")?)?;
        if expected.is_truthy()? && !expected.eq(column_triples(&relation.getattr("columns")?)?)? {
            return Ok(false);
        }
    }
    if managed_source {
        let expected: Bound<'_, PyDict> = PyDict::new(py);
        expected.set_item("kafka_broker_list", resource.getattr("broker_list")?)?;
        expected.set_item("kafka_topic_list", resource.getattr("topic")?)?;
        let consumer_group: String = resource.getattr("consumer_group")?.extract()?;
        expected.set_item(
            "kafka_group_name",
            database_scoped_consumer_group(&consumer_group, database),
        )?;
        expected.set_item("kafka_format", resource.getattr("format")?)?;
        for setting in resource.getattr("settings")?.try_iter()? {
            let (name, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = setting?.extract()?;
            expected.set_item(name, value)?;
        }
        let actual: Bound<'_, PyDict> = PyDict::new(py);
        for setting in relation.getattr("settings")?.try_iter()? {
            let (name, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = setting?.extract()?;
            actual.set_item(
                name,
                value.call_method0("strip")?.call_method1("strip", ("'",))?,
            )?;
        }
        return Ok(engine.eq(KAFKA_ENGINE)? && actual.eq(expected)?);
    }
    if table {
        let expected_engine: Bound<'_, PyAny> = resource
            .getattr("engine")?
            .call_method0("strip")?
            .call_method1("removesuffix", ("()",))?;
        let resource_settings: Bound<'_, PyAny> = resource.getattr("settings")?;
        let keep_default: bool = resource_settings.is_truthy()?;
        let default_granularity: Bound<'_, PyTuple> = PyTuple::new(py, DEFAULT_INDEX_GRANULARITY)?;
        let actual_settings: Vec<Bound<'_, PyAny>> = relation
            .getattr("settings")?
            .try_iter()?
            .filter_map(|setting| match setting {
                Ok(setting) => match setting.ne(&default_granularity) {
                    Ok(differs) if differs || keep_default => Some(Ok(setting)),
                    Ok(_) => None,
                    Err(error) => Some(Err(error)),
                },
                Err(error) => Some(Err(error)),
            })
            .collect::<PyResult<_>>()?;
        return Ok(engine.eq(expected_engine)?
            && resource
                .getattr("order_by")?
                .eq(relation.getattr("order_by")?)?
            && resource
                .getattr("partition_by")?
                .eq(relation.getattr("partition_by")?)?
            && resource.getattr("ttl")?.eq(relation.getattr("ttl")?)?
            && resource_settings.eq(PyTuple::new(py, actual_settings)?)?);
    }
    let placeholder: String = py
        .import(ADAPTER_CONSTANTS)?
        .getattr("ADAPTER_DATABASE_PLACEHOLDER")?
        .extract()?;
    let template: String = resource.getattr("database_template")?.extract()?;
    let template: String = template.replace(&format!("{placeholder}."), &format!("{database}."));
    let expected_query: Option<String> = match strip(&template) {
        "" => None,
        normalized => Some(analyze(py, normalized)?.canonical_sql),
    };
    let query_matches: bool = relation.getattr("query_sql")?.eq(expected_query)?;
    if is_a(resource, "AdapterMaterializedView")? {
        return Ok(engine.eq(MATERIALIZED_VIEW_ENGINE)?
            && relation
                .getattr("source_relation_name")?
                .eq(resource.getattr("source_relation_name")?)?
            && relation
                .getattr("target_relation_name")?
                .eq(resource.getattr("target_relation_name")?)?
            && query_matches);
    }
    Ok(engine.eq(VIEW_ENGINE)? && query_matches)
}

fn optional_text(value: Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    let rendered: String = text(&value)?;
    Ok((!rendered.is_empty()).then_some(rendered))
}

/// The refresh state of every scheduled relation in one database.
#[pyfunction]
#[pyo3(signature = (*, connection, database))]
fn load_clickhouse_refresh_states<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let escaped: String = database.replace('\\', "\\\\").replace('\'', "\\'");
    let result: Bound<'py, PyAny> = connection.call_method1(
        "query",
        (format!(
            "SELECT view, status, last_refresh_time, last_success_time, next_refresh_time, \
             exception FROM system.view_refreshes WHERE database = '{escaped}' ORDER BY view"
        ),),
    )?;
    let states: Vec<Bound<'py, PyAny>> = rows(&result)?
        .iter()
        .map(|row| {
            build(
                py,
                ADAPTER_MODELS,
                "AdapterRefreshState",
                &[
                    ("view_name", object(py, text(&row.get_item(0)?)?)?),
                    ("status", object(py, text(&row.get_item(1)?)?)?),
                    (
                        "last_refresh_at",
                        object(py, optional_text(row.get_item(2)?)?)?,
                    ),
                    (
                        "last_success_at",
                        object(py, optional_text(row.get_item(3)?)?)?,
                    ),
                    (
                        "next_refresh_at",
                        object(py, optional_text(row.get_item(4)?)?)?,
                    ),
                    ("exception", object(py, optional_text(row.get_item(5)?)?)?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, states)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(quote_clickhouse_sql_string, module)?)?;
    module.add_function(wrap_pyfunction!(load_clickhouse_catalog, module)?)?;
    module.add_function(wrap_pyfunction!(
        load_clickhouse_external_dependants,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        clickhouse_catalog_resource_matches,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(load_clickhouse_refresh_states, module)?)
}
