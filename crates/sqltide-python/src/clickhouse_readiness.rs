//! ClickHouse readiness: staged relations compared with their active counterparts, with offset
//! or scalar catch-up summaries by replay boundary. Warehouse reads go through the Python
//! adapter connection, in the same order and with the same statements as before.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::clickhouse_catalog::{analyze, build, object, result_error, text};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const PARTITION: &str = "_replay_partition";
const OFFSET: &str = "_replay_offset";
const TIMESTAMP: &str = "_replay_timestamp";
const LANDED_AT: &str = "_replay_landed_at";

#[derive(Clone, Copy, PartialEq, Eq)]
enum BoundaryMode {
    Offsets,
    Timestamp,
    LandedAt,
}

impl BoundaryMode {
    fn member(self) -> &'static str {
        match self {
            Self::Offsets => "OFFSETS",
            Self::Timestamp => "TIMESTAMP",
            Self::LandedAt => "LANDED_AT",
        }
    }
}

/// The first row of one query as a mapping, through the connection's `query_one`.
fn query_one<'py>(
    connection: &Bound<'py, PyAny>,
    statement: String,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py: Python<'py> = connection.py();
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("statement", statement)?;
    keywords.set_item("decode", py.get_type::<PyDict>())?;
    let row: Bound<'py, PyAny> = connection.getattr("query_one")?.call((), Some(&keywords))?;
    Ok((!row.is_none()).then_some(row))
}

fn column_names(
    connection: &Bound<'_, PyAny>,
    database: &str,
    table: &str,
) -> PyResult<HashSet<String>> {
    crate::clickhouse_catalog::query_mappings(
        connection,
        format!(
            "SELECT name FROM system.columns WHERE database = '{database}' AND table = '{table}'"
        ),
    )?
    .iter()
    .map(|row| text(&row.get_item("name")?))
    .collect()
}

fn int<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("builtins")?
        .getattr("int")?
        .call1((value,))
}

fn float<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("builtins")?
        .getattr("float")?
        .call1((value,))
}

fn optional_float<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        Ok(value)
    } else {
        float(value)
    }
}

/// `None` for a blank value (`None` or `""`), otherwise its text.
fn non_blank<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = value.py();
    if value.is_none() || value.eq("")? {
        Ok(py.None().into_bound(py))
    } else {
        Ok(value.str()?.into_any())
    }
}

fn table_exists(connection: &Bound<'_, PyAny>, database: &str, table: &str) -> PyResult<bool> {
    match query_one(
        connection,
        format!(
            "SELECT count() AS value FROM system.tables WHERE database = '{database}' AND name = '{table}'"
        ),
    )? {
        Some(row) => int(row.get_item("value")?)?.is_truthy(),
        None => Ok(false),
    }
}

/// The row count of one relation, or `None` when it does not exist.
fn safe_row_count<'py>(
    connection: &Bound<'py, PyAny>,
    database: &str,
    table: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py: Python<'py> = connection.py();
    if !table_exists(connection, database, table)? {
        return Ok(None);
    }
    let row = match query_one(
        connection,
        format!("SELECT count() AS value FROM {database}.{table}"),
    ) {
        Ok(row) => row,
        Err(failure)
            if failure.value(py).is_instance(
                &py.import(ADAPTER_EXCEPTIONS)?
                    .getattr("AdapterRelationNotFoundError")?,
            )? =>
        {
            return Ok(None);
        }
        Err(failure) => return Err(failure),
    };
    match row {
        Some(row) => Ok(Some(int(row.get_item("value")?)?)),
        None => Err(result_error(
            py,
            format!("Expected row count for {database}.{table}"),
        )?),
    }
}

fn boundary_mode(
    connection: &Bound<'_, PyAny>,
    database: &str,
    table: &str,
) -> PyResult<Option<BoundaryMode>> {
    let names: HashSet<String> = column_names(connection, database, table)?;
    Ok(if names.contains(PARTITION) && names.contains(OFFSET) {
        Some(BoundaryMode::Offsets)
    } else if names.contains(TIMESTAMP) {
        Some(BoundaryMode::Timestamp)
    } else if names.contains(LANDED_AT) {
        Some(BoundaryMode::LandedAt)
    } else {
        None
    })
}

fn staged_materialized_view_name(
    connection: &Bound<'_, PyAny>,
    database: &Bound<'_, PyAny>,
    staged_relation_name: &str,
) -> PyResult<Option<String>> {
    let catalog: Bound<'_, PyAny> = connection.call_method1("load_catalog", (database,))?;
    let mut matching: Vec<Bound<'_, PyAny>> = Vec::new();
    for relation in catalog.getattr("relations")?.try_iter()? {
        let relation: Bound<'_, PyAny> = relation?;
        if relation
            .getattr("target_relation_name")?
            .eq(staged_relation_name)?
        {
            matching.push(relation);
        }
    }
    if matching.len() > 1 {
        return Err(result_error(
            connection.py(),
            format!("Multiple materialized views target staged relation '{staged_relation_name}'"),
        )?);
    }
    matching
        .first()
        .map(|relation| text(&relation.getattr("name")?))
        .transpose()
}

fn staged_source_relation(
    connection: &Bound<'_, PyAny>,
    database: &str,
    view_name: &str,
) -> PyResult<Option<String>> {
    let Some(row) = query_one(
        connection,
        format!(
            "SELECT create_table_query FROM system.tables WHERE database = '{database}' AND name = '{view_name}'"
        ),
    )?
    else {
        return Ok(None);
    };
    let create_table_query: String = text(&row.get_item("create_table_query")?)?;
    Ok(analyze(connection.py(), &create_table_query)?
        .source_relations
        .into_iter()
        .next()
        .map(|source| source.name))
}

fn offset_boundary_column(
    connection: &Bound<'_, PyAny>,
    database: &str,
    source: &str,
) -> PyResult<Option<&'static str>> {
    let names: HashSet<String> = column_names(connection, database, source)?;
    Ok(if names.contains(TIMESTAMP) {
        Some(TIMESTAMP)
    } else if names.contains(LANDED_AT) {
        Some(LANDED_AT)
    } else {
        None
    })
}

fn offset_summary_query(
    active_offsets: &str,
    staged_offsets: &str,
    database: &str,
    source: Option<&str>,
    boundary: Option<&str>,
) -> String {
    let empty_offsets: String =
        format!("SELECT toInt64(0) AS {PARTITION}, toInt64(0) AS max_offset WHERE 0");
    let raw_offsets: String = match source {
        Some(source) => format!(
            "raw_offsets AS (SELECT {PARTITION}, max({OFFSET}) AS max_offset FROM {database}.{source} GROUP BY {PARTITION}), "
        ),
        None => format!("raw_offsets AS ({empty_offsets}), "),
    };
    let (raw_latest, staged_progress) = match (source, boundary) {
        (Some(source), Some(boundary)) => (
            format!(
                "raw_latest AS (SELECT {PARTITION}, max({boundary}) AS raw_max_boundary FROM {database}.{source} GROUP BY {PARTITION}), "
            ),
            format!(
                "staged_progress AS (SELECT source.{PARTITION}, max(source.{boundary}) AS staged_max_boundary \
                 FROM {database}.{source} AS source INNER JOIN ({staged_offsets}) AS staged_offsets \
                 ON source.{PARTITION} = staged_offsets.{PARTITION} AND source.{OFFSET} = staged_offsets.max_offset \
                 GROUP BY source.{PARTITION}) "
            ),
        ),
        _ => (
            format!(
                "raw_latest AS (SELECT toInt64(0) AS {PARTITION}, CAST(NULL AS Nullable(DateTime64(3))) AS raw_max_boundary WHERE 0), "
            ),
            format!(
                "staged_progress AS (SELECT toInt64(0) AS {PARTITION}, CAST(NULL AS Nullable(DateTime64(3))) AS staged_max_boundary WHERE 0) "
            ),
        ),
    };
    format!(
        "WITH active_offsets AS ({active_offsets}), staged_offsets AS ({staged_offsets}), \
         {raw_offsets}{raw_latest}{staged_progress}SELECT \
         (SELECT count() FROM active_offsets) AS active_partition_count, \
         (SELECT count() FROM staged_offsets) AS staged_partition_count, \
         countIf(active_offsets.max_offset IS NOT NULL AND staged_offsets.max_offset IS NOT NULL) AS partitions_compared, \
         countIf(active_offsets.max_offset IS NOT NULL AND staged_offsets.max_offset IS NULL) AS missing_staged_partition_count, \
         countIf(staged_offsets.max_offset IS NOT NULL AND (staged_progress.staged_max_boundary IS NULL OR raw_latest.raw_max_boundary IS NULL)) AS missing_freshness_partition_count, \
         countIf(raw_offsets.max_offset > coalesce(staged_offsets.max_offset, -1)) AS lagging_partition_count, \
         max(greatest(raw_offsets.max_offset - coalesce(staged_offsets.max_offset, raw_offsets.max_offset), 0)) AS max_offset_gap, \
         avg(greatest(raw_offsets.max_offset - coalesce(staged_offsets.max_offset, raw_offsets.max_offset), 0)) AS average_offset_gap, \
         max(dateDiff('second', staged_progress.staged_max_boundary, raw_latest.raw_max_boundary)) AS max_lag_seconds, \
         avg(dateDiff('second', staged_progress.staged_max_boundary, raw_latest.raw_max_boundary)) AS average_lag_seconds \
         FROM raw_offsets LEFT JOIN staged_offsets USING ({PARTITION}) LEFT JOIN active_offsets USING ({PARTITION}) \
         LEFT JOIN raw_latest USING ({PARTITION}) LEFT JOIN staged_progress USING ({PARTITION})"
    )
}

struct Root<'py> {
    request: Bound<'py, PyAny>,
    database: String,
    logical_name: String,
    staged_name: String,
}

fn offset_summary<'py>(
    connection: &Bound<'py, PyAny>,
    root: &Root<'py>,
    view_name: Option<&str>,
    active_queryable: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = connection.py();
    let (database, logical, staged) = (&root.database, &root.logical_name, &root.staged_name);
    let active_offsets: String = if active_queryable {
        format!(
            "SELECT {PARTITION}, max({OFFSET}) AS max_offset FROM {database}.{logical} GROUP BY {PARTITION}"
        )
    } else {
        format!("SELECT toInt64(0) AS {PARTITION}, toInt64(0) AS max_offset WHERE 0")
    };
    let staged_offsets: String = format!(
        "SELECT {PARTITION}, max({OFFSET}) AS max_offset FROM {database}.{staged} GROUP BY {PARTITION}"
    );
    let source: Option<String> = match view_name {
        Some(view_name) => staged_source_relation(connection, database, view_name)?,
        None => None,
    };
    let boundary: Option<&str> = match &source {
        Some(source) => offset_boundary_column(connection, database, source)?,
        None => None,
    };
    let Some(row) = query_one(
        connection,
        offset_summary_query(
            &active_offsets,
            &staged_offsets,
            database,
            source.as_deref(),
            boundary,
        ),
    )?
    else {
        return Err(result_error(
            py,
            "Expected offset catchup summary row".to_owned(),
        )?);
    };
    let value = |name: &str| row.get_item(name);
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    for name in [
        "active_partition_count",
        "staged_partition_count",
        "partitions_compared",
        "missing_staged_partition_count",
        "missing_freshness_partition_count",
        "lagging_partition_count",
        "max_offset_gap",
    ] {
        keywords.set_item(name, int(value(name)?)?)?;
    }
    keywords.set_item("average_offset_gap", float(value("average_offset_gap")?)?)?;
    let max_lag = optional_float(value("max_lag_seconds")?)?;
    let average_lag = optional_float(value("average_lag_seconds")?)?;
    keywords.set_item("lag_boundary_column", boundary)?;
    keywords.set_item("max_lag_seconds", max_lag)?;
    keywords.set_item("average_lag_seconds", average_lag)?;
    py.import(ADAPTER_MODELS)?
        .getattr("AdapterReadinessOffsetSummary")?
        .call((), Some(&keywords))
}

fn scalar_summary<'py>(
    connection: &Bound<'py, PyAny>,
    root: &Root<'py>,
    mode: BoundaryMode,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = connection.py();
    let column: &str = if mode == BoundaryMode::Timestamp {
        TIMESTAMP
    } else {
        LANDED_AT
    };
    let (database, logical, staged) = (&root.database, &root.logical_name, &root.staged_name);
    let Some(row) = query_one(
        connection,
        format!(
            "SELECT toString(min(active.{column})) AS active_min_value, \
             toString(max(active.{column})) AS active_max_value, \
             toString(min(staged.{column})) AS staged_min_value, \
             toString(max(staged.{column})) AS staged_max_value, \
             dateDiff('second', max(staged.{column}), max(active.{column})) AS lag_seconds \
             FROM {database}.{logical} AS active CROSS JOIN {database}.{staged} AS staged"
        ),
    )?
    else {
        return Err(result_error(
            py,
            "Expected scalar catchup summary row".to_owned(),
        )?);
    };
    let value = |name: &str| row.get_item(name);
    let active_min = non_blank(value("active_min_value")?)?;
    let active_max = non_blank(value("active_max_value")?)?;
    let staged_min = non_blank(value("staged_min_value")?)?;
    let staged_max = non_blank(value("staged_max_value")?)?;
    let lag = optional_float(value("lag_seconds")?)?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterReadinessScalarSummary",
        &[
            ("active_min_value", active_min),
            ("active_max_value", active_max),
            ("staged_min_value", staged_min),
            ("staged_max_value", staged_max),
            ("lag_seconds", lag),
        ],
    )
}

fn compare_root<'py>(
    connection: &Bound<'py, PyAny>,
    request: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = connection.py();
    let database_value: Bound<'py, PyAny> = request.getattr("database")?;
    let root: Root<'py> = Root {
        database: text(&database_value)?,
        logical_name: text(&request.getattr("logical_name")?)?,
        staged_name: text(&request.getattr("staged_relation_name")?)?,
        request,
    };
    let (database, staged) = (root.database.as_str(), root.staged_name.as_str());
    let staged_exists: bool = table_exists(connection, database, staged)?;
    let active_exists: bool = root.request.getattr("active_exists")?.is_truthy()?;
    let active_rows = if active_exists {
        safe_row_count(connection, database, &root.logical_name)?
    } else {
        None
    };
    let staged_rows = if staged_exists {
        safe_row_count(connection, database, staged)?
    } else {
        None
    };
    let mode: Option<BoundaryMode> = boundary_mode(connection, database, staged)?;
    let view_name: Option<String> =
        staged_materialized_view_name(connection, &database_value, staged)?;
    let source: Option<String> = match (&view_name, staged_exists) {
        (Some(view_name), true) => staged_source_relation(connection, database, view_name)?,
        _ => None,
    };
    let source_rows = match &source {
        Some(source) => safe_row_count(connection, database, source)?,
        None => None,
    };
    let active_queryable: bool = active_exists && active_rows.is_some();
    let none = || py.None().into_bound(py);
    let mut offsets: Bound<'py, PyAny> = none();
    let mut scalars: Bound<'py, PyAny> = none();
    if staged_exists && mode == Some(BoundaryMode::Offsets) {
        offsets = offset_summary(connection, &root, view_name.as_deref(), active_queryable)?;
    } else if let Some(mode @ (BoundaryMode::Timestamp | BoundaryMode::LandedAt)) = mode
        && active_queryable
        && staged_exists
    {
        scalars = scalar_summary(connection, &root, mode)?;
    }
    let mode: Bound<'py, PyAny> = match mode {
        Some(mode) => py
            .import(ADAPTER_TYPES)?
            .getattr("AdapterReplayBoundaryMode")?
            .getattr(mode.member())?,
        None => none(),
    };
    build(
        py,
        ADAPTER_MODELS,
        "AdapterReadinessRootObservation",
        &[
            ("root", root.request.clone()),
            ("staged_exists", object(py, staged_exists)?),
            ("active_row_count", object(py, active_rows)?),
            ("staged_row_count", object(py, staged_rows)?),
            ("replay_source_name", object(py, source)?),
            ("replay_source_row_count", object(py, source_rows)?),
            ("replay_boundary_mode", mode),
            ("offset_summary", offsets),
            ("scalar_summary", scalars),
        ],
    )
}

/// ClickHouse observations for every requested staged root.
#[pyfunction]
#[pyo3(signature = (*, connection, request))]
fn compare_clickhouse_readiness<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let observations: Vec<Bound<'py, PyAny>> = request
        .getattr("roots")?
        .try_iter()?
        .map(|root| compare_root(connection, root?))
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, observations)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(compare_clickhouse_readiness, module)?)
}
