//! One bounded ClickHouse diagnostic snapshot from system tables. Disk capacity is required;
//! every other probe is optional and degrades to a warning instead of failing the snapshot.
//! Warehouse reads go through the Python adapter connection.

use std::time::Instant;

use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFloat, PyInt, PyTuple};

use crate::clickhouse_catalog::{build, object, quote};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const DEAD_LETTERS_TABLE: &str = "_sqltide_dead_letters";
const DEAD_LETTER_PAYLOAD_CHARACTERS: u32 = 500;
const CAPACITY_CRITICAL_AVAILABLE_FRACTION: f64 = 0.05;
const CAPACITY_WARNING_AVAILABLE_FRACTION: f64 = 0.15;
const STUCK_CONSUMER_WINDOW_SECONDS: u32 = 300;
const STUCK_CONSUMER_MIN_ERRORS: u32 = 3;
const STUCK_CONSUMER_ERROR_CHARS: u32 = 500;
/// ClickHouse reports `UInt64` max for capacity it cannot measure.
const UNKNOWN_CAPACITY_BYTES: u64 = u64::MAX;
const DISKS_QUERY: &str = "SELECT name, path, type, is_broken, total_space, free_space, \
     unreserved_space, keep_free_space FROM system.disks ORDER BY name";
const SERVER_QUERY: &str = "SELECT version() AS version, uptime() AS uptime_seconds";
const METRIC_NAMES: [&str; 6] = [
    "MemoryResident",
    "CGroupMemoryUsed",
    "CGroupMemoryTotal",
    "OSMemoryTotal",
    "FilesystemMainPathAvailableINodes",
    "FilesystemMainPathTotalINodes",
];
const ACTIVITY_QUERY: &str = "SELECT \
     (SELECT count() FROM system.processes WHERE query_id != currentQueryID()) \
     AS active_queries, \
     (SELECT count() FROM system.merges) AS active_merges, \
     (SELECT count() FROM system.mutations WHERE NOT is_done) AS incomplete_mutations";
const DISK_CAPACITY_UNAVAILABLE: &str = "Disk capacity is unavailable.";

type Value<'py> = Bound<'py, PyAny>;
type Decode = for<'py> fn(&Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>>;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Status {
    Healthy,
    Warning,
    Critical,
    Unknown,
}

impl Status {
    fn name(self) -> &'static str {
        match self {
            Status::Healthy => "healthy",
            Status::Warning => "warning",
            Status::Critical => "critical",
            Status::Unknown => "unknown",
        }
    }

    /// `AdapterWarehouseHealthStatus(value)`.
    fn parse(value: &Value<'_>) -> PyResult<Status> {
        let name: String = value.str()?.extract()?;
        [
            Status::Healthy,
            Status::Warning,
            Status::Critical,
            Status::Unknown,
        ]
        .into_iter()
        .find(|status| status.name() == name)
        .ok_or_else(|| {
            PyValueError::new_err(format!(
                "'{name}' is not a valid AdapterWarehouseHealthStatus"
            ))
        })
    }
}

/// `int(str(value))`.
fn integer<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    value.py().get_type::<PyInt>().call1((value.str()?,))
}

/// `float(str(value))`.
fn float<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    value.py().get_type::<PyFloat>().call1((value.str()?,))
}

/// `str(value)`.
fn text<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(value.str()?.into_any())
}

/// `tuple(str(item) for item in value)`.
fn texts<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    let items: Vec<Value<'py>> = value
        .try_iter()?
        .map(|item| text(&item?))
        .collect::<PyResult<_>>()?;
    object(value.py(), PyTuple::new(value.py(), items)?)
}

fn capacity_bytes<'py>(value: &Value<'py>) -> PyResult<Option<Value<'py>>> {
    let bytes: Value<'py> = integer(value)?;
    Ok(if bytes.eq(UNKNOWN_CAPACITY_BYTES)? {
        None
    } else {
        Some(bytes)
    })
}

fn capacity_status(
    available: Option<&Value<'_>>,
    total: Option<&Value<'_>>,
    broken: bool,
) -> PyResult<Status> {
    if broken {
        return Ok(Status::Critical);
    }
    let (Some(available), Some(total)) = (available, total) else {
        return Ok(Status::Unknown);
    };
    if total.le(0)? {
        return Ok(Status::Unknown);
    }
    let available_fraction: f64 = available.div(total)?.extract()?;
    Ok(
        if available_fraction <= CAPACITY_CRITICAL_AVAILABLE_FRACTION {
            Status::Critical
        } else if available_fraction <= CAPACITY_WARNING_AVAILABLE_FRACTION {
            Status::Warning
        } else {
            Status::Healthy
        },
    )
}

fn overall_status(disk_statuses: &[Status], inode_status: Status) -> Status {
    if disk_statuses
        .iter()
        .all(|status| *status == Status::Unknown)
    {
        return Status::Unknown;
    }
    [Status::Critical, Status::Warning, Status::Healthy]
        .into_iter()
        .find(|candidate| disk_statuses.contains(candidate) || inode_status == *candidate)
        .unwrap_or(Status::Unknown)
}

fn decode_disk<'py>(row: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = row.py();
    let total_bytes: Option<Value<'py>> = capacity_bytes(&row.get_item("total_space")?)?;
    let unreserved_bytes: Option<Value<'py>> = capacity_bytes(&row.get_item("unreserved_space")?)?;
    let broken: bool = integer(&row.get_item("is_broken")?)?.is_truthy()?;
    let name: Value<'py> = text(&row.get_item("name")?)?;
    let path: Value<'py> = text(&row.get_item("path")?)?;
    let disk_type: Value<'py> = text(&row.get_item("type")?)?;
    let free_bytes: Option<Value<'py>> = capacity_bytes(&row.get_item("free_space")?)?;
    let keep_free_bytes: Option<Value<'py>> = capacity_bytes(&row.get_item("keep_free_space")?)?;
    let status: Status = capacity_status(unreserved_bytes.as_ref(), total_bytes.as_ref(), broken)?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseDisk",
        &[
            ("name", name),
            ("path", path),
            ("disk_type", disk_type),
            ("total_bytes", object(py, total_bytes)?),
            ("free_bytes", object(py, free_bytes)?),
            ("unreserved_bytes", object(py, unreserved_bytes)?),
            ("keep_free_bytes", object(py, keep_free_bytes)?),
            ("status", object(py, status.name())?),
        ],
    )
}

fn decode_dead_letter<'py>(row: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = row.py();
    let model_name: Value<'py> = text(&row.get_item("model_name")?)?;
    let row_count: Value<'py> = integer(&row.get_item("row_count")?)?;
    let latest_failed_columns: Value<'py> = texts(&row.get_item("latest_failed_columns")?)?;
    let latest_payload: Value<'py> = text(&row.get_item("latest_payload")?)?;
    let latest_quarantined_at: Value<'py> = text(&row.get_item("latest_quarantined_at")?)?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseDeadLetterModel",
        &[
            ("model_name", model_name),
            ("row_count", row_count),
            ("latest_failed_columns", latest_failed_columns),
            ("latest_payload", latest_payload),
            ("latest_quarantined_at", latest_quarantined_at),
        ],
    )
}

fn decode_table<'py>(row: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = row.py();
    let name: Value<'py> = text(&row.get_item("table")?)?;
    let rows: Value<'py> = integer(&row.get_item("rows")?)?;
    let bytes_on_disk: Value<'py> = integer(&row.get_item("bytes_on_disk")?)?;
    let active_parts: Value<'py> = integer(&row.get_item("active_parts")?)?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseTable",
        &[
            ("name", name),
            ("rows", rows),
            ("bytes_on_disk", bytes_on_disk),
            ("active_parts", active_parts),
        ],
    )
}

/// A Python `decode` callable for `AdapterConnection.query_many`.
fn decoder(py: Python<'_>, decode: Decode) -> PyResult<Value<'_>> {
    Ok(PyCFunction::new_closure(
        py,
        None,
        None,
        move |arguments: &Bound<'_, PyTuple>,
              _keywords: Option<&Bound<'_, PyDict>>|
              -> PyResult<Py<PyAny>> { Ok(decode(&arguments.get_item(0)?)?.unbind()) },
    )?
    .into_any())
}

fn query<'py>(
    connection: &Value<'py>,
    method: &str,
    statement: String,
    decode: Value<'py>,
) -> PyResult<Value<'py>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(connection.py());
    keywords.set_item("statement", statement)?;
    keywords.set_item("decode", decode)?;
    connection.call_method(method, (), Some(&keywords))
}

/// `connection.query_one(statement=..., decode=dict)`, or `None` without a row.
fn query_row<'py>(connection: &Value<'py>, statement: String) -> PyResult<Option<Value<'py>>> {
    let decode: Value<'py> = connection.py().get_type::<PyDict>().into_any();
    let row: Value<'py> = query(connection, "query_one", statement, decode)?;
    Ok((!row.is_none()).then_some(row))
}

/// `quote_clickhouse_sql_string(value)` for a value the caller passed through.
fn quote_value(value: &Value<'_>) -> PyResult<String> {
    Ok(quote(&value.extract::<String>()?))
}

fn metrics_query() -> String {
    let names: Vec<String> = METRIC_NAMES
        .iter()
        .map(|name| format!("'{name}'"))
        .collect();
    format!(
        "SELECT metric, value FROM system.asynchronous_metrics WHERE metric IN ({})",
        names.join(", ")
    )
}

fn tables_query(database: &Value<'_>) -> PyResult<String> {
    Ok(format!(
        "SELECT table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes_on_disk, \
         count() AS active_parts FROM system.parts \
         WHERE active AND database = {} GROUP BY table \
         ORDER BY bytes_on_disk DESC, table LIMIT 10",
        quote_value(database)?
    ))
}

/// The database the live dead-letter views write to, else the target database.
fn dead_letters_database<'py>(
    connection: &Value<'py>,
    database: &Value<'py>,
) -> PyResult<Value<'py>> {
    let statement: String = format!(
        "SELECT extract(create_table_query, \
         'TO `?([^.` ]+)`?\\\\.`?{DEAD_LETTERS_TABLE}') AS target_database FROM system.tables \
         WHERE database = {} \
         AND engine = 'MaterializedView' AND target_database != '' LIMIT 1",
        quote_value(database)?
    );
    let target_database: Value<'py> = match query_row(connection, statement)? {
        Some(row) => text(&row.get_item("target_database")?)?,
        None => object(connection.py(), "")?,
    };
    Ok(if target_database.is_truthy()? {
        target_database
    } else {
        database.clone()
    })
}

fn dead_letters_table_exists(connection: &Value<'_>, database: &Value<'_>) -> PyResult<bool> {
    let statement: String = format!(
        "SELECT 1 AS present FROM system.tables WHERE database = {} AND name = {}",
        quote_value(database)?,
        quote(DEAD_LETTERS_TABLE)
    );
    Ok(query_row(connection, statement)?.is_some())
}

fn dead_letters_query(database: &Value<'_>) -> PyResult<String> {
    Ok(format!(
        "SELECT model_name, count() AS row_count, \
         argMax(failed_columns, quarantined_at) AS latest_failed_columns, \
         substringUTF8(argMax(payload, quarantined_at), 1, {DEAD_LETTER_PAYLOAD_CHARACTERS}) \
         AS latest_payload, toString(max(quarantined_at)) AS latest_quarantined_at \
         FROM {}.{DEAD_LETTERS_TABLE} \
         WHERE quarantined_at >= now64(3) - INTERVAL 24 HOUR \
         GROUP BY model_name ORDER BY model_name",
        database.str()?
    ))
}

fn kafka_consumers_query(database: &Value<'_>, source_names: &Value<'_>) -> PyResult<String> {
    let quoted_database: String = quote_value(database)?;
    let quoted_tables: String = source_names
        .try_iter()?
        .map(|name| quote_value(&name?))
        .collect::<PyResult<Vec<String>>>()?
        .join(", ");
    let window: String = format!("now() - INTERVAL {STUCK_CONSUMER_WINDOW_SECONDS} SECOND");
    let recent_errors: String =
        format!("arrayCount(t -> t > last_commit_time AND t >= {window}, exceptions.time)");
    let latest_error: &str = "exceptions.text[indexOf(exceptions.time, arrayMax(exceptions.time))]";
    let stuck_consumers: String = format!(
        "SELECT groupArray((table, stuck_seconds, recent_errors, latest_error)) FROM (\
         SELECT table, \
         if(min(last_commit_time) = toDateTime(0), NULL, \
         dateDiff('second', min(last_commit_time), now())) AS stuck_seconds, \
         toUInt64(sum({recent_errors})) AS recent_errors, \
         substring(argMax({latest_error}, last_poll_time), 1, \
         {STUCK_CONSUMER_ERROR_CHARS}) AS latest_error \
         FROM system.kafka_consumers \
         WHERE database = {quoted_database} AND table IN ({quoted_tables}) \
         AND last_commit_time < {window} \
         AND {recent_errors} >= {STUCK_CONSUMER_MIN_ERRORS} \
         GROUP BY table ORDER BY table)"
    );
    Ok(format!(
        "SELECT \
         (SELECT count() FROM system.tables \
         WHERE database = {quoted_database} AND name IN ({quoted_tables}) \
         AND engine = 'Kafka') AS materialized_tables, \
         (SELECT groupArray(name) FROM system.tables \
         WHERE database = {quoted_database} AND name IN ({quoted_tables}) \
         AND engine = 'Kafka') AS materialized_table_names, \
         countDistinct(table) AS polling_tables, \
         countDistinctIf(table, notEmpty(exceptions.time) AND \
         arrayMax(exceptions.time) >= last_poll_time) AS exception_tables, \
         ({stuck_consumers}) AS stuck_consumers \
         FROM system.kafka_consumers \
         WHERE database = {quoted_database} AND table IN ({quoted_tables})"
    ))
}

fn stuck_consumers<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = value.py();
    let mut consumers: Vec<Value<'py>> = Vec::new();
    for entry in value.try_iter()? {
        let fields: Vec<Value<'py>> = entry?.try_iter()?.take(5).collect::<PyResult<_>>()?;
        let count: usize = fields.len();
        let [table, stuck_seconds, recent_errors, latest_error] =
            <[Value<'py>; 4]>::try_from(fields).map_err(|_| {
                PyValueError::new_err(if count < 4 {
                    format!("not enough values to unpack (expected 4, got {count})")
                } else {
                    "too many values to unpack (expected 4)".to_owned()
                })
            })?;
        let table: Value<'py> = text(&table)?;
        let stuck_seconds: Value<'py> = if stuck_seconds.is_none() {
            stuck_seconds
        } else {
            integer(&stuck_seconds)?
        };
        let recent_errors: Value<'py> = integer(&recent_errors)?;
        let latest_error: Value<'py> = text(&latest_error)?;
        consumers.push(build(
            py,
            ADAPTER_MODELS,
            "AdapterWarehouseStuckKafkaConsumer",
            &[
                ("table", table),
                ("stuck_seconds", stuck_seconds),
                ("recent_errors", recent_errors),
                ("latest_error", latest_error),
            ],
        )?);
    }
    object(py, PyTuple::new(py, consumers)?)
}

/// `int(metrics[name])`, or `None` when the metric is absent.
fn integer_metric<'py>(metrics: &Bound<'py, PyDict>, name: &str) -> PyResult<Option<Value<'py>>> {
    metrics
        .get_item(name)?
        .map(|value| metrics.py().get_type::<PyInt>().call1((value,)))
        .transpose()
}

fn memory<'py>(metrics: &Bound<'py, PyDict>) -> PyResult<Option<Value<'py>>> {
    let py: Python<'py> = metrics.py();
    let resident: Option<Value<'py>> = integer_metric(metrics, "MemoryResident")?;
    let host_total: Option<Value<'py>> = integer_metric(metrics, "OSMemoryTotal")?;
    let cgroup_used: Option<Value<'py>> = integer_metric(metrics, "CGroupMemoryUsed")?;
    let cgroup_total: Option<Value<'py>> = integer_metric(metrics, "CGroupMemoryTotal")?;
    let pressure_fraction: Option<Value<'py>> = match (&cgroup_used, &cgroup_total) {
        (Some(used), Some(total)) if total.gt(0)? => Some(used.div(total)?),
        _ => None,
    };
    let fallback_available: bool = match (&resident, &host_total) {
        (Some(_), Some(total)) => total.gt(0)?,
        _ => false,
    };
    let cgroup: bool = pressure_fraction.is_some();
    if !cgroup && !fallback_available {
        return Ok(None);
    }
    Ok(Some(build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseMemory",
        &[
            ("resident_bytes", object(py, resident)?),
            ("host_total_bytes", object(py, host_total)?),
            (
                "cgroup_used_bytes",
                object(py, cgroup_used.filter(|_| cgroup))?,
            ),
            (
                "cgroup_limit_bytes",
                object(py, cgroup_total.filter(|_| cgroup))?,
            ),
            (
                "basis",
                object(py, if cgroup { "cgroup" } else { "server_rss_host" })?,
            ),
            ("pressure_fraction", object(py, pressure_fraction)?),
        ],
    )?))
}

fn elapsed_ms(started_at: Instant) -> u64 {
    (started_at.elapsed().as_secs_f64() * 1000.0).round_ties_even() as u64
}

fn unavailable(py: Python<'_>, started_at: Instant) -> PyResult<Value<'_>> {
    let none: Value<'_> = py.None().into_bound(py);
    build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseHealth",
        &[
            ("availability", object(py, "unavailable")?),
            ("status", object(py, Status::Unknown.name())?),
            ("version", none.clone()),
            ("uptime_seconds", none.clone()),
            ("disks", object(py, PyTuple::empty(py))?),
            ("inode_total", none.clone()),
            ("inode_free", none.clone()),
            ("inode_status", object(py, Status::Unknown.name())?),
            ("memory", none.clone()),
            ("activity", none.clone()),
            ("tables", none),
            (
                "collection_duration_ms",
                object(py, elapsed_ms(started_at))?,
            ),
            ("warnings", object(py, (DISK_CAPACITY_UNAVAILABLE,))?),
        ],
    )
}

/// Whether an optional probe succeeded; adapter, key, type and value failures only degrade it.
fn succeeded(adapter_error: &Value<'_>, result: PyResult<()>) -> PyResult<bool> {
    let py: Python<'_> = adapter_error.py();
    match result {
        Ok(()) => Ok(true),
        Err(error)
            if error.is_instance_of::<PyKeyError>(py)
                || error.is_instance_of::<PyTypeError>(py)
                || error.is_instance_of::<PyValueError>(py)
                || error.is_instance(py, adapter_error) =>
        {
            Ok(false)
        }
        Err(error) => Err(error),
    }
}

/// Collect one bounded snapshot without making optional failures authoritative.
#[pyfunction]
#[pyo3(signature = (*, connection, database, managed_source_names = None, include_dead_letters = false))]
fn load_clickhouse_warehouse_health<'py>(
    connection: &Bound<'py, PyAny>,
    database: Bound<'py, PyAny>,
    managed_source_names: Option<Bound<'py, PyAny>>,
    include_dead_letters: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = connection.py();
    let started_at: Instant = Instant::now();
    let adapter_error: Value<'py> = py.import(ADAPTER_EXCEPTIONS)?.getattr("AdapterError")?;
    let source_names: Value<'py> =
        managed_source_names.unwrap_or_else(|| PyTuple::empty(py).into_any());
    let mut warnings: Vec<&'static str> = Vec::new();

    let mut disks: Option<(Value<'py>, Vec<Status>)> = None;
    let read: PyResult<()> = (|| {
        let rows: Value<'py> = query(
            connection,
            "query_many",
            DISKS_QUERY.to_owned(),
            decoder(py, decode_disk)?,
        )?;
        if !rows.is_truthy()? {
            return Ok(());
        }
        let statuses: Vec<Status> = rows
            .try_iter()?
            .map(|disk| Status::parse(&disk?.getattr("status")?))
            .collect::<PyResult<_>>()?;
        if statuses.iter().all(|status| *status == Status::Unknown) {
            warnings.push("Usable disk capacity is unavailable.");
        }
        disks = Some((rows, statuses));
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        return unavailable(py, started_at);
    }
    let Some((disks, disk_statuses)) = disks else {
        return unavailable(py, started_at);
    };

    let mut version: Option<Value<'py>> = None;
    let mut uptime_seconds: Option<Value<'py>> = None;
    let read: PyResult<()> = (|| {
        match query_row(connection, SERVER_QUERY.to_owned())? {
            Some(row) => {
                version = Some(text(&row.get_item("version")?)?);
                uptime_seconds = Some(integer(&row.get_item("uptime_seconds")?)?);
            }
            None => warnings.push("Server identity is unavailable."),
        }
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        warnings.push("Server identity is unavailable.");
    }

    let mut inode_total: Option<Value<'py>> = None;
    let mut inode_free: Option<Value<'py>> = None;
    let mut inode_status: Status = Status::Unknown;
    let mut memory_context: Option<Value<'py>> = None;
    let read: PyResult<()> = (|| {
        let rows: Value<'py> = query(
            connection,
            "query_many",
            metrics_query(),
            py.get_type::<PyDict>().into_any(),
        )?;
        let metrics: Bound<'py, PyDict> = PyDict::new(py);
        for row in rows.try_iter()? {
            let row: Value<'py> = row?;
            let name: Value<'py> = text(&row.get_item("metric")?)?;
            metrics.set_item(name, float(&row.get_item("value")?)?)?;
        }
        inode_total = integer_metric(&metrics, "FilesystemMainPathTotalINodes")?;
        inode_free = integer_metric(&metrics, "FilesystemMainPathAvailableINodes")?;
        inode_status = capacity_status(inode_free.as_ref(), inode_total.as_ref(), false)?;
        memory_context = memory(&metrics)?;
        if inode_status == Status::Unknown {
            warnings.push("Main-path inode metrics are unavailable.");
        }
        if memory_context.is_none() {
            warnings.push("Meaningful memory context is unavailable.");
        }
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        warnings.push("Memory and inode metrics are unavailable.");
    }

    let mut activity: Option<Value<'py>> = None;
    let read: PyResult<()> = (|| {
        match query_row(connection, ACTIVITY_QUERY.to_owned())? {
            Some(row) => {
                let active_queries: Value<'py> = integer(&row.get_item("active_queries")?)?;
                let active_merges: Value<'py> = integer(&row.get_item("active_merges")?)?;
                let incomplete_mutations: Value<'py> =
                    integer(&row.get_item("incomplete_mutations")?)?;
                activity = Some(build(
                    py,
                    ADAPTER_MODELS,
                    "AdapterWarehouseActivity",
                    &[
                        ("active_queries", active_queries),
                        ("active_merges", active_merges),
                        ("incomplete_mutations", incomplete_mutations),
                    ],
                )?);
            }
            None => warnings.push("Current warehouse activity is unavailable."),
        }
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        warnings.push("Current warehouse activity is unavailable.");
    }

    let mut kafka_consumers: Option<Value<'py>> = None;
    if source_names.is_truthy()? {
        let read: PyResult<()> = (|| {
            let statement: String = kafka_consumers_query(&database, &source_names)?;
            match query_row(connection, statement)? {
                Some(row) => {
                    let configured_tables: Value<'py> = object(py, source_names.len()?)?;
                    let materialized_tables: Value<'py> =
                        integer(&row.get_item("materialized_tables")?)?;
                    let materialized_table_names: Value<'py> =
                        texts(&row.get_item("materialized_table_names")?)?;
                    let polling_tables: Value<'py> = integer(&row.get_item("polling_tables")?)?;
                    let exception_tables: Value<'py> = integer(&row.get_item("exception_tables")?)?;
                    let stuck: Value<'py> = stuck_consumers(&row.get_item("stuck_consumers")?)?;
                    kafka_consumers = Some(build(
                        py,
                        ADAPTER_MODELS,
                        "AdapterWarehouseKafkaConsumers",
                        &[
                            ("configured_tables", configured_tables),
                            ("materialized_tables", materialized_tables),
                            ("materialized_table_names", materialized_table_names),
                            ("polling_tables", polling_tables),
                            ("exception_tables", exception_tables),
                            ("stuck_consumers", stuck),
                        ],
                    )?);
                }
                None => warnings.push("Managed Kafka consumer health is unavailable."),
            }
            Ok(())
        })();
        if !succeeded(&adapter_error, read)? {
            warnings.push("Managed Kafka consumer health is unavailable.");
        }
    } else {
        let zero: Value<'py> = object(py, 0)?;
        kafka_consumers = Some(build(
            py,
            ADAPTER_MODELS,
            "AdapterWarehouseKafkaConsumers",
            &[
                ("configured_tables", zero.clone()),
                ("materialized_tables", zero.clone()),
                ("materialized_table_names", object(py, PyTuple::empty(py))?),
                ("polling_tables", zero.clone()),
                ("exception_tables", zero),
            ],
        )?);
    }

    let mut dead_letters: Option<Value<'py>> =
        (!include_dead_letters).then(|| PyTuple::empty(py).into_any());
    let read: PyResult<()> = (|| {
        if !include_dead_letters {
            return Ok(());
        }
        let target_database: Value<'py> = dead_letters_database(connection, &database)?;
        if dead_letters_table_exists(connection, &target_database)? {
            dead_letters = Some(query(
                connection,
                "query_many",
                dead_letters_query(&target_database)?,
                decoder(py, decode_dead_letter)?,
            )?);
        }
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        warnings.push("Dead-letter counts are unavailable.");
    }

    let mut tables: Option<Value<'py>> = None;
    let read: PyResult<()> = (|| {
        tables = Some(query(
            connection,
            "query_many",
            tables_query(&database)?,
            decoder(py, decode_table)?,
        )?);
        Ok(())
    })();
    if !succeeded(&adapter_error, read)? {
        warnings.push("Project table footprint is unavailable.");
    }

    let status: Status = overall_status(&disk_statuses, inode_status);
    build(
        py,
        ADAPTER_MODELS,
        "AdapterWarehouseHealth",
        &[
            (
                "availability",
                object(
                    py,
                    if warnings.is_empty() {
                        "available"
                    } else {
                        "partial"
                    },
                )?,
            ),
            ("status", object(py, status.name())?),
            ("version", object(py, version)?),
            ("uptime_seconds", object(py, uptime_seconds)?),
            ("disks", disks),
            ("inode_total", object(py, inode_total)?),
            ("inode_free", object(py, inode_free)?),
            ("inode_status", object(py, inode_status.name())?),
            ("memory", object(py, memory_context)?),
            ("activity", object(py, activity)?),
            ("tables", object(py, tables)?),
            (
                "collection_duration_ms",
                object(py, elapsed_ms(started_at))?,
            ),
            ("kafka_consumers", object(py, kafka_consumers)?),
            ("dead_letters", object(py, dead_letters)?),
            (
                "capacity_warning_fraction",
                object(py, CAPACITY_WARNING_AVAILABLE_FRACTION)?,
            ),
            (
                "capacity_critical_fraction",
                object(py, CAPACITY_CRITICAL_AVAILABLE_FRACTION)?,
            ),
            ("warnings", object(py, PyTuple::new(py, warnings)?)?),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(load_clickhouse_warehouse_health, module)?)
}
