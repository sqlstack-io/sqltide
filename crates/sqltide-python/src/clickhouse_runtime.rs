//! ClickHouse connection decisions made around warehouse round trips: the target-wide mutation
//! lock, the one-time StreamBuild metadata rename, active query progress and cancellation, and
//! committed replay offset frontiers. Statements run through the Python adapter connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PySet, PyTuple};
use sqltide_planner::direct::sha256_hex;

use crate::clickhouse_catalog::{build, object, quote};
use crate::model_sql::chained;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const LOCK_TABLE: &str = "_sqltide_target_mutation_lock";
const LOCK_COMMENT_PREFIX: &str = "sqltide-target-lock-v2:";
const METADATA_TABLE_NAME_PREFIX: &str = "_sqltide_";
const STREAMBUILD_TABLE_NAME_PREFIX: &str = "_streambuild_";
/// The framework metadata tables a StreamBuild target carries under its old prefix.
const METADATA_TABLE_CONSTANTS: [&str; 17] = [
    "METADATA_SCHEMA_VERSIONS_TABLE_NAME",
    "METADATA_OBJECT_STATE_TABLE_NAME",
    "METADATA_DEPLOYMENTS_TABLE_NAME",
    "METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME",
    "METADATA_PUBLISH_HISTORY_TABLE_NAME",
    "METADATA_DIRECT_FINGERPRINTS_TABLE_NAME",
    "METADATA_DEAD_LETTERS_TABLE_NAME",
    "METADATA_MANIFESTS_TABLE_NAME",
    "METADATA_INVOCATIONS_TABLE_NAME",
    "METADATA_NODE_RESULTS_TABLE_NAME",
    "METADATA_RUN_EVENTS_TABLE_NAME",
    "METADATA_RUN_STATEMENTS_TABLE_NAME",
    "METADATA_SENSOR_CHECKPOINTS_TABLE_NAME",
    "METADATA_SENSOR_TICKS_TABLE_NAME",
    "METADATA_SENSOR_STEPS_TABLE_NAME",
    "METADATA_SENSOR_OVERRIDES_TABLE_NAME",
    "METADATA_SENSOR_LEASES_TABLE_NAME",
];
const CANCELLATION_CONFIRM_SECONDS: f64 = 5.0;
const CANCELLATION_POLL_SECONDS: f64 = 0.05;

type Value<'py> = Bound<'py, PyAny>;

fn identifier(value: &str) -> String {
    format!("`{}`", value.replace('`', "``"))
}

fn lock_table(database: &str) -> String {
    format!("{}.`{LOCK_TABLE}`", identifier(database))
}

fn streambuild_name(name: &str) -> String {
    format!(
        "{STREAMBUILD_TABLE_NAME_PREFIX}{}",
        name.strip_prefix(METADATA_TABLE_NAME_PREFIX)
            .unwrap_or(name)
    )
}

fn exception<'py>(py: Python<'py>, class: &str, message: String) -> PyResult<Value<'py>> {
    py.import(ADAPTER_EXCEPTIONS)?
        .getattr(class)?
        .call1((message,))
}

fn adapter_error(py: Python<'_>) -> PyResult<Value<'_>> {
    py.import(ADAPTER_EXCEPTIONS)?.getattr("AdapterError")
}

fn rows<'py>(connection: &Value<'py>, statement: String) -> PyResult<Value<'py>> {
    connection
        .call_method1("query", (statement,))?
        .getattr("rows")
}

fn execute(connection: &Value<'_>, statement: String) -> PyResult<()> {
    connection.call_method1("execute_workflow_sql", (statement,))?;
    Ok(())
}

/// A failure raised while `handled` is being handled: Python links `handled` as its context
/// unless the failure was raised inside a deeper handler that already set one.
fn raised_while_handling(py: Python<'_>, failure: PyErr, handled: &PyErr) -> PyResult<PyErr> {
    if failure.value(py).getattr("__context__")?.is_none() {
        failure
            .value(py)
            .setattr("__context__", handled.value(py))?;
    }
    Ok(failure)
}

/// Whether a lock table exists, and the owner its comment records.
fn lock_record(
    connection: &Value<'_>,
    database: &str,
    table_name: &str,
) -> PyResult<(bool, Option<String>)> {
    let found: Value<'_> = rows(
        connection,
        format!(
            "SELECT comment FROM system.tables WHERE database = {} AND name = {} LIMIT 1",
            quote(database),
            quote(table_name)
        ),
    )?;
    if !found.is_truthy()? {
        return Ok((false, None));
    }
    let comment: String = found.get_item(0)?.get_item(0)?.str()?.extract()?;
    Ok((
        true,
        comment.strip_prefix(LOCK_COMMENT_PREFIX).map(str::to_owned),
    ))
}

/// Acquire one target-wide lock using ClickHouse's atomic DDL namespace.
#[pyfunction]
#[pyo3(signature = (*, connection, database, owner_id))]
fn acquire_clickhouse_target_mutation_lock<'py>(
    connection: &Bound<'py, PyAny>,
    database: &str,
    owner_id: &str,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = connection.py();
    let created: PyResult<()> = execute(
        connection,
        format!(
            "CREATE TABLE {} (guard UInt8) ENGINE = TinyLog COMMENT {};",
            lock_table(database),
            quote(&format!("{LOCK_COMMENT_PREFIX}{owner_id}"))
        ),
    );
    if let Err(error) = created {
        let adapter_error: Value<'py> = adapter_error(py)?;
        if !error.is_instance(py, &adapter_error) {
            return Err(error);
        }
        let (exists, owner) = match lock_record(connection, database, LOCK_TABLE) {
            Ok(record) => record,
            Err(inspection) if inspection.is_instance(py, &adapter_error) => {
                chained(error.value(py), inspection.value(py), None)?;
                return Err(error);
            }
            Err(inspection) => return Err(raised_while_handling(py, inspection, &error)?),
        };
        if !exists {
            return Err(error);
        }
        let owner_suffix: String = owner.map_or_else(String::new, |owner| format!(" by '{owner}'"));
        let locked: Value<'py> = exception(
            py,
            "AdapterTargetMutationLockError",
            format!("Target database '{database}' is already mutation-locked{owner_suffix}"),
        )?;
        chained(&locked, error.value(py), Some(error.value(py)))?;
        return Err(PyErr::from_value(locked));
    }
    build(
        py,
        ADAPTER_MODELS,
        "AdapterTargetMutationLock",
        &[
            ("database", object(py, database)?),
            ("owner_id", object(py, owner_id)?),
        ],
    )
}

/// Atomically exchange and verify the exact lock generation before release.
#[pyfunction]
#[pyo3(signature = (*, connection, lock))]
fn release_clickhouse_target_mutation_lock(
    connection: &Bound<'_, PyAny>,
    lock: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py: Python<'_> = connection.py();
    let database: String = lock.getattr("database")?.extract()?;
    let owner_id: String = lock.getattr("owner_id")?.extract()?;
    let release_name: String = format!("{LOCK_TABLE}_release_{}", &sha256_hex(&owner_id)[..16]);
    let release_table: String = format!("{}.{}", identifier(&database), identifier(&release_name));
    execute(
        connection,
        format!(
            "CREATE TABLE {release_table} (guard UInt8) ENGINE = TinyLog COMMENT {};",
            quote(&format!("{LOCK_COMMENT_PREFIX}{owner_id}"))
        ),
    )?;
    let lock_table: String = lock_table(&database);
    execute(
        connection,
        format!("EXCHANGE TABLES {lock_table} AND {release_table};"),
    )?;
    let (marker_exists, current_owner) = lock_record(connection, &database, &release_name)?;
    if !marker_exists || current_owner.as_deref() != Some(owner_id.as_str()) {
        execute(
            connection,
            format!("EXCHANGE TABLES {lock_table} AND {release_table};"),
        )?;
        execute(connection, format!("DROP TABLE {release_table} SYNC;"))?;
        return Err(PyErr::from_value(exception(
            py,
            "AdapterTargetMutationLockError",
            format!("Target mutation lock ownership changed for database '{database}'"),
        )?));
    }
    execute(connection, format!("DROP TABLE {release_table} SYNC;"))?;
    execute(connection, format!("DROP TABLE {lock_table} SYNC;"))
}

fn existing_table_names(
    connection: &Value<'_>,
    database: &str,
    names: &[String],
) -> PyResult<Vec<String>> {
    let quoted: Vec<String> = names.iter().map(|name| quote(name)).collect();
    rows(
        connection,
        format!(
            "SELECT name FROM system.tables WHERE database = {} AND name IN ({})",
            quote(database),
            quoted.join(", ")
        ),
    )?
    .try_iter()?
    .map(|row| row?.get_item(0)?.str()?.extract())
    .collect()
}

/// Rename StreamBuild metadata tables, tolerating a concurrent identical rename.
#[pyfunction]
#[pyo3(signature = (*, connection, database))]
fn rename_clickhouse_streambuild_metadata(
    connection: &Bound<'_, PyAny>,
    database: &str,
) -> PyResult<()> {
    let py: Python<'_> = connection.py();
    let constants: Bound<'_, PyModule> = py.import(ADAPTER_CONSTANTS)?;
    let renames: Vec<(String, String)> = METADATA_TABLE_CONSTANTS
        .iter()
        .map(|constant| {
            let name: String = constants.getattr(*constant)?.extract()?;
            Ok((streambuild_name(&name), name))
        })
        .collect::<PyResult<_>>()?;
    let streambuild_lock: String = streambuild_name(LOCK_TABLE);
    let candidates: Vec<String> = renames
        .iter()
        .map(|(old, _)| old.clone())
        .chain(renames.iter().map(|(_, new)| new.clone()))
        .chain([streambuild_lock.clone()])
        .collect();
    let existing: Vec<String> = existing_table_names(connection, database, &candidates)?;
    let pending: Vec<&(String, String)> = renames
        .iter()
        .filter(|(old, _)| existing.contains(old))
        .collect();
    if pending.is_empty() {
        return Ok(());
    }
    if existing.contains(&streambuild_lock) {
        return Err(PyErr::from_value(exception(
            py,
            "AdapterError",
            format!(
                "Target database '{database}' is mutation-locked by a StreamBuild run. Wait for \
                 it to finish, or drop the stale {database}.{streambuild_lock} table after \
                 confirming no run is active, then retry"
            ),
        )?));
    }
    let conflicts: Vec<&str> = pending
        .iter()
        .filter(|(_, new)| existing.contains(new))
        .map(|(_, new)| new.as_str())
        .collect();
    if !conflicts.is_empty() {
        return Err(PyErr::from_value(exception(
            py,
            "AdapterError",
            format!(
                "Target database '{database}' contains both StreamBuild and SQLTide metadata \
                 ({}). A StreamBuild version wrote to this target after it was renamed; stop \
                 using StreamBuild here and drop the outdated {STREAMBUILD_TABLE_NAME_PREFIX}* \
                 tables, then retry",
                conflicts.join(", ")
            ),
        )?));
    }
    let quoted_database: String = identifier(database);
    let clauses: Vec<String> = pending
        .iter()
        .map(|(old, new)| {
            format!(
                "{quoted_database}.{} TO {quoted_database}.{}",
                identifier(old),
                identifier(new)
            )
        })
        .collect();
    let Err(error) = execute(connection, format!("RENAME TABLE {};", clauses.join(", "))) else {
        return Ok(());
    };
    if !error.is_instance(py, &adapter_error(py)?) {
        return Err(error);
    }
    let pending_names: Vec<String> = pending.iter().map(|(old, _)| old.clone()).collect();
    match existing_table_names(connection, database, &pending_names) {
        Ok(remaining) if remaining.is_empty() => Ok(()),
        Ok(_) => Err(error),
        Err(inspection) => Err(raised_while_handling(py, inspection, &error)?),
    }
}

/// `int(str(value))`.
fn integer<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    value.py().get_type::<PyInt>().call1((value.str()?,))
}

/// Read one active query snapshot from ClickHouse `system.processes`.
#[pyfunction]
#[pyo3(signature = (*, connection, query_id))]
fn load_clickhouse_statement_progress<'py>(
    connection: &Bound<'py, PyAny>,
    query_id: &str,
) -> PyResult<Option<Value<'py>>> {
    let py: Python<'py> = connection.py();
    let literal: String = query_id.replace('\\', "\\\\").replace('\'', "\\'");
    let found: Value<'py> = connection
        .call_method1(
            "query",
            (format!(
                "SELECT elapsed, read_rows, read_bytes, total_rows_approx, memory_usage, \
                 Settings AS settings FROM system.processes WHERE query_id = '{literal}' LIMIT 1"
            ),),
        )?
        .call_method0("named_rows")?;
    if !found.is_truthy()? {
        return Ok(None);
    }
    let row: Value<'py> = found.get_item(0)?;
    let raw_settings: Value<'py> = row.get_item("settings")?;
    let settings: Value<'py> =
        if raw_settings.is_instance(&py.import("collections.abc")?.getattr("Mapping")?)? {
            let pairs: Vec<Value<'py>> = raw_settings
                .call_method0("items")?
                .try_iter()?
                .map(|item| {
                    let (name, value): (Value<'py>, Value<'py>) = item?.extract()?;
                    object(py, (name.str()?, value.str()?))
                })
                .collect::<PyResult<_>>()?;
            let pairs: Bound<'py, PyList> = PyList::new(py, pairs)?;
            pairs.sort()?;
            object(py, pairs.to_tuple())?
        } else {
            object(py, PyTuple::empty(py))?
        };
    let elapsed: Value<'py> = py
        .get_type::<PyFloat>()
        .call1((row.get_item("elapsed")?.str()?,))?;
    let read_rows: Value<'py> = integer(&row.get_item("read_rows")?)?;
    let read_bytes: Value<'py> = integer(&row.get_item("read_bytes")?)?;
    let total_rows_approx: Value<'py> = integer(&row.get_item("total_rows_approx")?)?;
    let memory_usage: Value<'py> = integer(&row.get_item("memory_usage")?)?;
    Ok(Some(build(
        py,
        ADAPTER_MODELS,
        "AdapterStatementProgress",
        &[
            ("elapsed_seconds", elapsed),
            ("read_rows", read_rows),
            ("read_bytes", read_bytes),
            ("total_rows_approx", total_rows_approx),
            ("memory_usage_bytes", memory_usage),
            ("settings", settings),
        ],
    )?))
}

/// Kill one exact ClickHouse query and confirm it leaves `system.processes`.
#[pyfunction]
#[pyo3(signature = (*, connection, query_id))]
fn cancel_clickhouse_workflow_query<'py>(
    connection: &Bound<'py, PyAny>,
    query_id: &str,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = connection.py();
    let query_found: bool = rows(
        connection,
        format!("KILL QUERY WHERE query_id = {} SYNC", quote(query_id)),
    )?
    .is_truthy()?;
    let time: Bound<'py, PyModule> = py.import("time")?;
    let deadline: f64 =
        time.call_method0("monotonic")?.extract::<f64>()? + CANCELLATION_CONFIRM_SECONDS;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("query_id", query_id)?;
    let mut confirmed: bool = false;
    while time.call_method0("monotonic")?.extract::<f64>()? < deadline {
        if connection
            .call_method("load_statement_progress", (), Some(&keywords))?
            .is_none()
        {
            confirmed = true;
            break;
        }
        time.call_method1("sleep", (CANCELLATION_POLL_SECONDS,))?;
    }
    let mut fields: Vec<(&str, Value<'py>)> = vec![
        ("query_id", object(py, query_id)?),
        ("supported", object(py, true)?),
        ("query_found", object(py, query_found)?),
        ("termination_confirmed", object(py, confirmed)?),
    ];
    if !confirmed {
        fields.push((
            "detail",
            object(py, "ClickHouse query remained active after KILL QUERY SYNC")?,
        ));
    }
    build(py, ADAPTER_MODELS, "AdapterQueryCancellation", &fields)
}

fn frontier<'py>(
    py: Python<'py>,
    partition: Value<'py>,
    offset: Value<'py>,
) -> PyResult<Value<'py>> {
    build(
        py,
        ADAPTER_MODELS,
        "AdapterReplayOffsetFrontier",
        &[("partition", partition), ("completed_offset", offset)],
    )
}

/// Observe the greatest committed target offset per captured partition, never moving a
/// partition's frontier backwards from `previous`.
#[pyfunction]
#[pyo3(signature = (*, connection, request, previous))]
fn observe_clickhouse_replay_offset_frontiers<'py>(
    connection: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
    previous: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = connection.py();
    let builtins: Bound<'py, PyModule> = py.import("builtins")?;
    let partitions: Bound<'py, PySet> = PySet::empty(py)?;
    for item in request.getattr("ranges")?.try_iter()? {
        partitions.add(item?.getattr("partition")?)?;
    }
    let partitions: Value<'py> = builtins.call_method1("sorted", (partitions,))?;
    let mut observed: Vec<(Value<'py>, Value<'py>)> = Vec::new();
    if partitions.is_truthy()? {
        let column = |name: &str| -> PyResult<String> {
            Ok(identifier(&request.getattr(name)?.extract::<String>()?))
        };
        let database: String = column("database")?;
        let relation: String = column("relation")?;
        let partition_column: String = column("partition_column")?;
        let offset_column: String = column("offset_column")?;
        let rendered: Vec<String> = partitions
            .try_iter()?
            .map(|partition| Ok(partition?.str()?.to_string()))
            .collect::<PyResult<_>>()?;
        let found: Value<'py> = connection
            .call_method1(
                "query",
                (format!(
                    "SELECT toInt64({partition_column}) AS partition, \
                     toInt64(max({offset_column})) AS completed_offset \
                     FROM {database}.{relation} \
                     WHERE {partition_column} IN ({}) \
                     GROUP BY partition ORDER BY partition",
                    rendered.join(", ")
                ),),
            )?
            .call_method0("named_rows")?;
        for row in found.try_iter()? {
            let row: Value<'py> = row?;
            observed.push((
                integer(&row.get_item("partition")?)?,
                integer(&row.get_item("completed_offset")?)?,
            ));
        }
    }
    let completed: Bound<'py, PyDict> = PyDict::new(py);
    for item in previous.try_iter()? {
        let item: Value<'py> = item?;
        completed.set_item(
            item.getattr("partition")?,
            item.getattr("completed_offset")?,
        )?;
    }
    for (partition, offset) in observed {
        let current: Value<'py> = completed
            .get_item(&partition)?
            .unwrap_or_else(|| offset.clone());
        completed.set_item(&partition, builtins.call_method1("max", (offset, current))?)?;
    }
    let frontiers: Vec<Value<'py>> = builtins
        .call_method1("sorted", (&completed,))?
        .try_iter()?
        .map(|partition| {
            let partition: Value<'py> = partition?;
            let offset: Value<'py> = completed.as_any().get_item(&partition)?;
            frontier(py, partition, offset)
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, frontiers)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        acquire_clickhouse_target_mutation_lock,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        release_clickhouse_target_mutation_lock,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rename_clickhouse_streambuild_metadata,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        load_clickhouse_statement_progress,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(cancel_clickhouse_workflow_query, module)?)?;
    module.add_function(wrap_pyfunction!(
        observe_clickhouse_replay_offset_frontiers,
        module
    )?)
}
