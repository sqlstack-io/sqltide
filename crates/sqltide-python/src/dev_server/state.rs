//! The `/api/state` and `/api/topics` live overlays, assembled from warehouse and broker reads.

use std::collections::BTreeSet;

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::activity::{ACTIVITY_WINDOW_SECONDS, parse_warehouse_timestamp, read_model_activity};
use super::drift::model_drift_payload;
use super::support::{attr, unpack};
use crate::py_kwargs;

const LANDED_AT_COLUMN: &str = "_replay_landed_at";
const NO_MANAGED_SOURCES_REASON: &str =
    "no managed Kafka sources define broker connections; the dev server has no credentials to inspect a cluster";
const THROUGHPUT_WINDOW_LADDER: [(i64, i64); 3] = [(3600, 60), (86400, 1800), (604800, 10800)];
const FRESH: &str = "fresh";
const LAGGING: &str = "lagging";
const STALLED: &str = "stalled";

type Any<'py> = Bound<'py, PyAny>;

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

fn text(value: &Any<'_>) -> PyResult<String> {
    Ok(value.str()?.to_str()?.to_owned())
}

/// `int(str(value))`.
fn int<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "int")?.call1((value.str()?,))
}

fn builtin<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, "builtins", name)
}

fn is<'py>(value: &Any<'py>, module: &str, class: &str) -> PyResult<bool> {
    value.is_instance(&attr(value.py(), module, class)?)
}

fn is_kafka_landing(source: &Any<'_>) -> PyResult<bool> {
    is(&source.getattr("source")?, "sqltide.compiler.discovery.models", "KafkaLandingStep")
}

fn is_table_model(model: &Any<'_>) -> PyResult<bool> {
    is(model, "sqltide.compiler.compile.models", "CompiledTableModel")
}

/// `dict.get(key)` on any mapping-like object.
fn get<'py>(mapping: &Any<'py>, key: impl IntoPyObject<'py>) -> PyResult<Any<'py>> {
    mapping.call_method1("get", (key,))
}

fn get_or_empty<'py>(mapping: &Any<'py>, key: &Any<'py>) -> PyResult<Any<'py>> {
    mapping.call_method1("get", (key, PyDict::new(mapping.py())))
}

fn optional_str<'py>(value: Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() { Ok(value) } else { Ok(value.str()?.into_any()) }
}

fn sources<'py>(analysis: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    analysis.getattr("compiled_project")?.getattr("sources")?.try_iter()?.collect()
}

fn relation_name_of<'py>(analysis: &Any<'py>, key: &Any<'py>) -> PyResult<Any<'py>> {
    analysis.getattr("realized_project")?.getattr("relation_name_by_logical_key")?.get_item(key)
}

fn resources_of<'py>(analysis: &Any<'py>, key: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = analysis.py();
    analysis
        .getattr("realized_project")?
        .getattr("resources_by_logical_key")?
        .call_method1("get", (key, PyTuple::empty(py)))?
        .try_iter()?
        .collect()
}

fn named_rows<'py>(connection: &Any<'py>, query: String) -> PyResult<Vec<Any<'py>>> {
    connection.call_method1("query", (query,))?.call_method0("named_rows")?.try_iter()?.collect()
}

/// Approximate rows and bytes for every relation in one scan of system.tables.
#[pyfunction]
#[pyo3(signature = (*, database))]
pub(crate) fn build_relation_stats_query(database: &str) -> String {
    format!(
        "SELECT name, coalesce(total_rows, 0) AS total_rows, coalesce(total_bytes, 0) AS total_bytes \
         FROM system.tables WHERE database = '{database}'"
    )
}

/// Active part counts per relation in one scan of system.parts.
#[pyfunction]
#[pyo3(signature = (*, database))]
pub(crate) fn build_parts_query(database: &str) -> String {
    format!("SELECT table, count() AS parts FROM system.parts WHERE database = '{database}' AND active GROUP BY table")
}

/// Oldest and newest landed event per lineage-bearing relation, batched.
#[pyfunction]
#[pyo3(signature = (*, database, relation_names))]
pub(crate) fn build_extents_query(database: &str, relation_names: Vec<Any<'_>>) -> PyResult<String> {
    Ok(relation_names
        .iter()
        .map(|name| {
            let name: String = text(name)?;
            Ok(format!(
                "SELECT '{name}' AS relation, toString(min(_replay_landed_at)) AS oldest, \
                 toString(max(_replay_landed_at)) AS newest, count() AS rows \
                 FROM `{database}`.`{name}` HAVING count() > 0"
            ))
        })
        .collect::<PyResult<Vec<_>>>()?
        .join(" UNION ALL "))
}

/// Landed-event counts for every source in one warehouse round trip.
#[pyfunction]
#[pyo3(signature = (*, database, windows))]
pub(crate) fn build_throughputs_query(database: &str, windows: Any<'_>) -> PyResult<String> {
    let mut selects: Vec<String> = Vec::new();
    for window in windows.try_iter()? {
        let parts = unpack(&window?, 3)?;
        let (relation_name, window_seconds, bucket_seconds) = (text(&parts[0])?, &parts[1], &parts[2]);
        selects.push(format!(
            "SELECT '{relation_name}' AS relation, toUnixTimestamp(toStartOfInterval(\
             _replay_landed_at, INTERVAL {} SECOND)) AS bucket, count() AS rows \
             FROM `{database}`.`{relation_name}` WHERE _replay_landed_at >= now64(3) - INTERVAL {} SECOND \
             GROUP BY bucket",
            text(bucket_seconds)?,
            text(window_seconds)?,
        ));
    }
    Ok(selects.join(" UNION ALL ") + " ORDER BY relation, bucket")
}

/// Per-partition landed offsets for every source in one warehouse round trip.
#[pyfunction]
#[pyo3(signature = (*, database, relation_names))]
pub(crate) fn build_partitions_query(database: &str, relation_names: Vec<Any<'_>>) -> PyResult<String> {
    Ok(relation_names
        .iter()
        .map(|name| {
            let name: String = text(name)?;
            Ok(format!(
                "SELECT '{name}' AS relation, _replay_partition AS partition, max(_replay_offset) AS max_offset, \
                 toString(min(_replay_landed_at)) AS oldest, toString(max(_replay_landed_at)) AS newest, count() AS rows \
                 FROM `{database}`.`{name}` GROUP BY partition"
            ))
        })
        .collect::<PyResult<Vec<_>>>()?
        .join(" UNION ALL ")
        + " ORDER BY relation, partition")
}

/// Pick the smallest ladder rung whose window still contains the newest event.
#[pyfunction]
#[pyo3(signature = (*, newest_age_seconds))]
pub(crate) fn choose_throughput_window(newest_age_seconds: Option<f64>) -> (i64, i64) {
    let last: (i64, i64) = THROUGHPUT_WINDOW_LADDER[THROUGHPUT_WINDOW_LADDER.len() - 1];
    let Some(age) = newest_age_seconds else {
        return last;
    };
    THROUGHPUT_WINDOW_LADDER.into_iter().find(|(window, _)| age < *window as f64).unwrap_or(last)
}

/// Measure a stable logical view by the deployment relation it is bound to.
#[pyfunction]
#[pyo3(signature = (*, stats, bindings))]
pub(crate) fn apply_bound_relation_stats<'py>(stats: Any<'py>, bindings: Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = stats.py();
    for binding in bindings.try_iter()? {
        let parts = unpack(&binding?, 2)?;
        let (logical_name, physical_name) = (&parts[0], &parts[1]);
        let measured = get(&stats, physical_name)?;
        if !measured.is_none() {
            stats.set_item(logical_name, builtin(py, "dict")?.call1((measured,))?)?;
        }
    }
    Ok(stats)
}

fn warehouse_health_payload<'py>(
    health: &Any<'py>,
    captured_at: &Any<'py>,
    adapter_name: Any<'py>,
    database: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = health.py();
    let memory = health.getattr("memory")?;
    let memory = if memory.is_none() {
        memory
    } else {
        py_kwargs!(py;
            residentBytes = memory.getattr("resident_bytes")?,
            hostTotalBytes = memory.getattr("host_total_bytes")?,
            cgroupUsedBytes = memory.getattr("cgroup_used_bytes")?,
            cgroupLimitBytes = memory.getattr("cgroup_limit_bytes")?,
            basis = memory.getattr("basis")?,
            pressureFraction = memory.getattr("pressure_fraction")?,
        )?
        .into_any()
    };
    let activity = health.getattr("activity")?;
    let activity = if activity.is_none() {
        activity
    } else {
        py_kwargs!(py;
            activeQueries = activity.getattr("active_queries")?,
            activeMerges = activity.getattr("active_merges")?,
            incompleteMutations = activity.getattr("incomplete_mutations")?,
        )?
        .into_any()
    };
    let consumers = health.getattr("kafka_consumers")?;
    let consumers = if consumers.is_none() {
        consumers
    } else {
        let mut stuck: Vec<Any<'py>> = Vec::new();
        for consumer in consumers.getattr("stuck_consumers")?.try_iter()? {
            let consumer = consumer?;
            stuck.push(
                py_kwargs!(py;
                    table = consumer.getattr("table")?,
                    stuckSeconds = consumer.getattr("stuck_seconds")?,
                    recentErrors = consumer.getattr("recent_errors")?,
                    latestError = consumer.getattr("latest_error")?,
                )?
                .into_any(),
            );
        }
        py_kwargs!(py;
            configuredTables = consumers.getattr("configured_tables")?,
            materializedTables = consumers.getattr("materialized_tables")?,
            materializedTableNames = PyList::new(py, consumers.getattr("materialized_table_names")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
            pollingTables = consumers.getattr("polling_tables")?,
            exceptionTables = consumers.getattr("exception_tables")?,
            stuckConsumers = PyList::new(py, stuck)?,
        )?
        .into_any()
    };
    let mut disks: Vec<Any<'py>> = Vec::new();
    for disk in health.getattr("disks")?.try_iter()? {
        let disk = disk?;
        disks.push(
            py_kwargs!(py;
                name = disk.getattr("name")?,
                path = disk.getattr("path")?,
                type = disk.getattr("disk_type")?,
                totalBytes = disk.getattr("total_bytes")?,
                freeBytes = disk.getattr("free_bytes")?,
                unreservedBytes = disk.getattr("unreserved_bytes")?,
                keepFreeBytes = disk.getattr("keep_free_bytes")?,
                status = disk.getattr("status")?.str()?,
            )?
            .into_any(),
        );
    }
    let dead_letters = health.getattr("dead_letters")?;
    let dead_letters = if dead_letters.is_none() {
        dead_letters
    } else {
        let mut models: Vec<Any<'py>> = Vec::new();
        for model in dead_letters.try_iter()? {
            let model = model?;
            models.push(
                py_kwargs!(py;
                    modelName = model.getattr("model_name")?,
                    rowCount = model.getattr("row_count")?,
                    latestFailedColumns = PyList::new(py, model.getattr("latest_failed_columns")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
                    latestPayload = model.getattr("latest_payload")?,
                    latestQuarantinedAt = model.getattr("latest_quarantined_at")?,
                )?
                .into_any(),
            );
        }
        PyList::new(py, models)?.into_any()
    };
    let tables = health.getattr("tables")?;
    let tables = if tables.is_none() {
        tables
    } else {
        let mut items: Vec<Any<'py>> = Vec::new();
        for table in tables.try_iter()? {
            let table = table?;
            items.push(
                py_kwargs!(py;
                    name = table.getattr("name")?,
                    rows = table.getattr("rows")?,
                    bytesOnDisk = table.getattr("bytes_on_disk")?,
                    activeParts = table.getattr("active_parts")?,
                )?
                .into_any(),
            );
        }
        PyList::new(py, items)?.into_any()
    };
    let measured_at = health.getattr("measured_at")?;
    Ok(py_kwargs!(py;
        availability = health.getattr("availability")?.str()?,
        status = health.getattr("status")?.str()?,
        adapter = adapter_name,
        database = database,
        version = health.getattr("version")?,
        uptimeSeconds = health.getattr("uptime_seconds")?,
        measuredAt = if measured_at.is_truthy()? { measured_at } else { captured_at.clone() },
        collectionDurationMs = health.getattr("collection_duration_ms")?,
        stale = health.getattr("stale")?,
        warnings = PyList::new(py, health.getattr("warnings")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
        capacityWarningFraction = health.getattr("capacity_warning_fraction")?,
        capacityCriticalFraction = health.getattr("capacity_critical_fraction")?,
        disks = PyList::new(py, disks)?,
        inodes = py_kwargs!(py;
            total = health.getattr("inode_total")?,
            free = health.getattr("inode_free")?,
            status = health.getattr("inode_status")?.str()?,
        )?,
        memory = memory,
        activity = activity,
        kafkaConsumers = consumers,
        deadLetters = dead_letters,
        tables = tables,
    )?
    .into_any())
}

fn has_quarantine_model(analysis: &Any<'_>) -> PyResult<bool> {
    let quarantine = attr(analysis.py(), "sqltide.compiler.discovery.types", "RowErrorPolicy")?.getattr("QUARANTINE")?;
    for model in analysis.getattr("compiled_project")?.getattr("models")?.try_iter()? {
        let model = model?;
        if is_table_model(&model)? && model.getattr("on_row_error")?.is(&quarantine) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn managed_source_of<'py>(analysis: &Any<'py>, key: &Any<'py>) -> PyResult<Option<Any<'py>>> {
    for resource in resources_of(analysis, key)? {
        if is(&resource, "sqltide.adapter.models", "AdapterManagedSource")? {
            return Ok(Some(resource));
        }
    }
    Ok(None)
}

fn managed_kafka_relation_names<'py>(analysis: &Any<'py>) -> PyResult<Bound<'py, PyTuple>> {
    let mut names: BTreeSet<String> = BTreeSet::new();
    for source in sources(analysis)? {
        if !is_kafka_landing(&source)? {
            continue;
        }
        for resource in resources_of(analysis, &source.getattr("key")?)? {
            if is(&resource, "sqltide.adapter.models", "AdapterManagedSource")? {
                names.insert(resource.getattr("name")?.extract()?);
            }
        }
    }
    PyTuple::new(analysis.py(), names)
}

fn active_bindings<'py>(connection: &Any<'py>, database: &str) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = connection.py();
    if !connection.getattr("capabilities")?.getattr("stable_logical_bindings")?.is_truthy()? {
        return Ok(PyTuple::empty(py));
    }
    let inspected = connection.call_method1("inspect_managed_table_state", (database,))?;
    let mut bindings: Vec<Any<'py>> = Vec::new();
    for binding in inspected.getattr("active_bindings")?.try_iter()? {
        let binding = binding?;
        bindings.push(PyTuple::new(py, [binding.getattr("logical_name")?, binding.getattr("physical_name")?])?.into_any());
    }
    PyTuple::new(py, bindings)
}

fn relation_stats<'py>(connection: &Any<'py>, database: &str, bindings: &Bound<'py, PyTuple>) -> PyResult<Any<'py>> {
    let py: Python<'py> = connection.py();
    let stats: Bound<'py, PyDict> = PyDict::new(py);
    for row in named_rows(connection, build_relation_stats_query(database))? {
        let entry = py_kwargs!(py;
            rows = int(&row.get_item("total_rows")?)?,
            bytes = int(&row.get_item("total_bytes")?)?,
            parts = 0,
        )?;
        stats.set_item(row.get_item("name")?.str()?, entry)?;
    }
    for row in named_rows(connection, build_parts_query(database))? {
        if let Some(entry) = stats.get_item(row.get_item("table")?.str()?)? {
            entry.set_item("parts", int(&row.get_item("parts")?)?)?;
        }
    }
    apply_bound_relation_stats(stats.into_any(), bindings.clone().into_any())
}

fn lineage_relation_names(catalog: &Any<'_>) -> PyResult<Vec<String>> {
    let mut names: Vec<String> = Vec::new();
    for relation in catalog.getattr("relations")?.try_iter()? {
        let relation = relation?;
        let mut landed: bool = false;
        for column in relation.getattr("columns")?.try_iter()? {
            landed |= column?.getattr("name")?.eq(LANDED_AT_COLUMN)?;
        }
        if landed {
            names.push(relation.getattr("name")?.extract()?);
        }
    }
    Ok(names)
}

fn extents<'py>(connection: &Any<'py>, database: &str, relation_names: Vec<String>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let extents: Bound<'py, PyDict> = PyDict::new(py);
    if relation_names.is_empty() {
        return Ok(extents);
    }
    let names: Vec<Any<'py>> = relation_names.iter().map(|name| Ok(name.into_pyobject(py)?.into_any())).collect::<PyResult<_>>()?;
    for row in named_rows(connection, build_extents_query(database, names)?)? {
        let entry = py_kwargs!(py;
            oldest = row.get_item("oldest")?.str()?,
            newest = row.get_item("newest")?.str()?,
            rows = int(&row.get_item("rows")?)?,
        )?;
        extents.set_item(row.get_item("relation")?.str()?, entry)?;
    }
    Ok(extents)
}

/// `{relation: {partition: (partition, max_offset, oldest, newest, rows)}}`.
fn warehouse_partitions<'py>(connection: &Any<'py>, database: &str, relation_names: Vec<String>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let observations: Bound<'py, PyDict> = PyDict::new(py);
    if relation_names.is_empty() {
        return Ok(observations);
    }
    for relation_name in &relation_names {
        observations.set_item(relation_name, PyDict::new(py))?;
    }
    let names: Vec<Any<'py>> = relation_names.iter().map(|name| Ok(name.into_pyobject(py)?.into_any())).collect::<PyResult<_>>()?;
    for row in named_rows(connection, build_partitions_query(database, names)?)? {
        let relation_name = row.get_item("relation")?.str()?;
        let partition = int(&row.get_item("partition")?)?;
        let by_partition = observations.as_any().get_item(&relation_name)?;
        let observation = PyTuple::new(
            py,
            [
                partition.clone(),
                int(&row.get_item("max_offset")?)?,
                row.get_item("oldest")?.str()?.into_any(),
                row.get_item("newest")?.str()?.into_any(),
                int(&row.get_item("rows")?)?,
            ],
        )?;
        by_partition.set_item(partition, observation)?;
    }
    Ok(observations)
}

fn source_extents<'py>(partitions: &Bound<'py, PyDict>, extents: &Bound<'py, PyDict>) -> PyResult<()> {
    let py: Python<'py> = partitions.py();
    for (relation_name, by_partition) in partitions.iter() {
        let observations: Vec<Any<'py>> = by_partition.call_method0("values")?.try_iter()?.collect::<PyResult<_>>()?;
        if observations.is_empty() {
            continue;
        }
        let mut oldest: Option<String> = None;
        let mut newest: Option<String> = None;
        let mut rows: Any<'py> = 0_i64.into_pyobject(py)?.into_any();
        for observation in &observations {
            let (first, last): (String, String) = (observation.get_item(2)?.extract()?, observation.get_item(3)?.extract()?);
            if oldest.as_ref().is_none_or(|current| first < *current) {
                oldest = Some(first);
            }
            if newest.as_ref().is_none_or(|current| last > *current) {
                newest = Some(last);
            }
            rows = rows.add(observation.get_item(4)?)?;
        }
        extents.set_item(relation_name, py_kwargs!(py; oldest = oldest, newest = newest, rows = rows)?)?;
    }
    Ok(())
}

fn fingerprint_baselines<'py>(analysis: &Any<'py>, connection: &Any<'py>, database: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let mut identities: Vec<String> = Vec::new();
    for model in analysis.getattr("compiled_project")?.getattr("models")?.try_iter()? {
        identities.push(format!("{database}.{}", text(&model?.getattr("key")?.getattr("name")?)?));
    }
    let snapshot = connection.call_method(
        "load_direct_fingerprints",
        (),
        Some(&py_kwargs!(py; database = database, logical_model_identities = PyTuple::new(py, identities)?)?),
    )?;
    let statuses = attr(py, "sqltide.adapter.types", "AdapterOptionalStateStatus")?;
    let status = snapshot.getattr("status")?;
    if status.ne(statuses.getattr("AVAILABLE")?)? {
        return Ok(if status.eq(statuses.getattr("UNAVAILABLE")?)? { none(py) } else { PyDict::new(py).into_any() });
    }
    let prefix: String = format!("{database}.");
    let baselines: Bound<'py, PyDict> = PyDict::new(py);
    for record in snapshot.getattr("baselines")?.try_iter()? {
        let record = record?;
        baselines.set_item(record.getattr("logical_model_identity")?.call_method1("removeprefix", (&prefix,))?, record)?;
    }
    Ok(baselines.into_any())
}

/// `round((captured_at - newest).total_seconds(), 3)`; `None` when either does not parse.
fn age_seconds<'py>(newest: &Any<'py>, captured_at: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = newest.py();
    if newest.is_none() {
        return Ok(none(py));
    }
    let Some(newest_at) = parse_warehouse_timestamp(py, &newest.extract::<String>()?)? else {
        return Ok(none(py));
    };
    let Some(now_at) = parse_warehouse_timestamp(py, captured_at)? else {
        return Ok(none(py));
    };
    builtin(py, "round")?.call1((now_at.sub(newest_at)?.call_method0("total_seconds")?, 3))
}

fn policy_seconds<'py>(value: &Any<'py>) -> PyResult<Option<Any<'py>>> {
    let py: Python<'py> = value.py();
    if value.is_none() {
        return Ok(None);
    }
    let raw: String = value.extract()?;
    let Some(unit) = raw.chars().last() else {
        return Err(PyIndexError::new_err("string index out of range"));
    };
    let amount = builtin(py, "float")?.call1((&raw[..raw.len() - unit.len_utf8()],))?;
    let multiplier = attr(py, "sqltide.compiler.discovery.constants", "SECONDS_BY_DURATION_UNIT")?.get_item(unit.to_string())?;
    Ok(Some(amount.mul(multiplier)?))
}

fn freshness<'py>(newest: &Any<'py>, captured_at: &str, policy: &Any<'py>) -> PyResult<Option<&'static str>> {
    if policy.is_none() {
        return Ok(None);
    }
    let age = age_seconds(newest, captured_at)?;
    if age.is_none() {
        return Ok(Some(STALLED));
    }
    if let Some(error_after) = policy_seconds(&policy.getattr("error_after")?)?
        && age.gt(error_after)?
    {
        return Ok(Some(STALLED));
    }
    if let Some(warn_after) = policy_seconds(&policy.getattr("warn_after")?)?
        && age.gt(warn_after)?
    {
        return Ok(Some(LAGGING));
    }
    Ok(Some(FRESH))
}

fn kafka_settings<'py>(source: &Any<'py>, managed: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = source.py();
    let settings = builtin(py, "dict")?.call1((managed.getattr("settings")?,))?;
    attr(py, "dataclasses", "replace")?.call(
        (source.getattr("source")?.getattr("kafka")?,),
        Some(&py_kwargs!(py;
            broker_list = managed.getattr("broker_list")?,
            topic = managed.getattr("topic")?,
            consumer_group = managed.getattr("consumer_group")?,
            format = managed.getattr("format")?,
            settings = if settings.is_truthy()? { settings } else { none(py) },
        )?),
    )
}

fn kafka_lag<'py>(analysis: &Any<'py>, source: &Any<'py>, database: &str, reader: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    if !is_kafka_landing(source)? || reader.is_none() {
        return Ok(none(py));
    }
    let Some(managed) = managed_source_of(analysis, &source.getattr("key")?)? else {
        return Ok(none(py));
    };
    let kafka = kafka_settings(source, &managed)?;
    reader.call_method("read", (), Some(&py_kwargs!(py; kafka = kafka, database = database)?))
}

#[allow(clippy::too_many_arguments)]
fn model_states<'py>(
    analysis: &Any<'py>,
    catalog: &Any<'py>,
    baselines: &Any<'py>,
    stats: &Any<'py>,
    extents: &Bound<'py, PyDict>,
    captured_at: &str,
    activity: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    let compiled = analysis.getattr("compiled_project")?;
    let policies: Bound<'py, PyDict> = PyDict::new(py);
    let mut direct_model_names: Vec<Any<'py>> = Vec::new();
    let modes = attr(py, "sqltide.compiler.discovery.types", "PipelineMode")?;
    for pipeline in compiled.getattr("pipelines")?.try_iter()? {
        let pipeline = pipeline?;
        let source = pipeline.getattr("source")?;
        let policy = if source.is_none() { none(py) } else { source.getattr("source")?.getattr("freshness")? };
        for model in pipeline.getattr("models")?.try_iter()? {
            policies.set_item(model?.getattr("key")?.getattr("name")?, &policy)?;
        }
    }
    for pipeline in compiled.getattr("pipelines")?.try_iter()? {
        let pipeline = pipeline?;
        if modes.call1((pipeline.getattr("pipeline")?.getattr("mode")?,))?.eq(modes.getattr("DIRECT")?)? {
            for model in pipeline.getattr("models")?.try_iter()? {
                direct_model_names.push(model?.getattr("key")?.getattr("name")?);
            }
        }
    }
    let direct_names = attr(py, "builtins", "set")?.call1((PyList::new(py, direct_model_names)?,))?;
    let fingerprint = attr(py, "sqltide._native", "DirectModelFingerprint")?;
    let states: Bound<'py, PyDict> = PyDict::new(py);
    let empty = || PyDict::new(py).into_any();
    for model in compiled.getattr("models")?.try_iter()? {
        let model = model?;
        let is_table: bool = is_table_model(&model)?;
        let name = model.getattr("key")?.getattr("name")?;
        let relation_name = relation_name_of(analysis, &model.getattr("key")?)?;
        let relation_stats = if is_table { get_or_empty(stats, &relation_name)? } else { empty() };
        let extent = if is_table { get_or_empty(extents.as_any(), &relation_name)? } else { empty() };
        let newest = optional_str(get(&extent, "newest")?)?;
        let drift_reasons = if !baselines.is_none() && direct_names.contains(&name)? {
            fingerprint.call_method(
                "drift_reasons",
                (),
                Some(&py_kwargs!(py; model = &model, baseline = get(baselines, &name)?)?),
            )?
        } else {
            PyTuple::empty(py).into_any()
        };
        let policy = if is_table { get(policies.as_any(), &name)? } else { none(py) };
        let state = py_kwargs!(py;
            relationName = &relation_name,
            rows = get(&relation_stats, "rows")?,
            diskBytes = get(&relation_stats, "bytes")?,
            parts = get(&relation_stats, "parts")?,
            oldestRowAt = optional_str(get(&extent, "oldest")?)?,
            newestRowAt = &newest,
            lagSeconds = age_seconds(&newest, captured_at)?,
            freshness = freshness(&newest, captured_at, &policy)?,
            activity = get(activity.as_any(), &relation_name)?,
            drift = drift_reasons.is_truthy()?,
            driftReasons = PyList::new(py, drift_reasons.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
            semanticDrift = model_drift_payload(analysis.clone(), model.clone(), catalog.clone())?,
        )?;
        states.set_item(name, state)?;
    }
    Ok(states)
}

fn zero_filled_buckets<'py>(counts: &Bound<'py, PyDict>, window_seconds: i64, bucket_seconds: i64) -> PyResult<Any<'py>> {
    let py: Python<'py> = counts.py();
    let bucket_count: i64 = window_seconds.div_euclid(bucket_seconds);
    if counts.is_empty() {
        return Ok(PyList::new(py, vec![0_i64; usize::try_from(bucket_count).unwrap_or(0)])?.into_any());
    }
    let newest: i64 = builtin(py, "max")?.call1((counts,))?.extract()?;
    let start: i64 = newest - (bucket_count - 1) * bucket_seconds;
    let mut buckets: Vec<Any<'py>> = Vec::new();
    for index in 0..bucket_count {
        buckets.push(counts.as_any().call_method1("get", (start + index * bucket_seconds, 0))?);
    }
    Ok(PyList::new(py, buckets)?.into_any())
}

fn throughputs<'py>(connection: &Any<'py>, database: &str, newest_ages: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let result: Bound<'py, PyDict> = PyDict::new(py);
    if newest_ages.is_empty() {
        return Ok(result);
    }
    let mut windows: Vec<(String, i64, i64)> = Vec::new();
    for (relation_name, age) in newest_ages.iter() {
        let (window, bucket) = choose_throughput_window(age.extract()?);
        windows.push((relation_name.extract()?, window, bucket));
    }
    let query_windows = PyList::new(py, windows.iter().map(|(name, window, bucket)| (name.clone(), *window, *bucket)))?.into_any();
    let counts: Bound<'py, PyDict> = PyDict::new(py);
    for row in named_rows(connection, build_throughputs_query(database, query_windows)?)? {
        let relation = row.get_item("relation")?.str()?;
        let by_bucket = counts.as_any().call_method1("setdefault", (relation, PyDict::new(py)))?;
        let bucket = int(&row.get_item("bucket")?)?;
        by_bucket.set_item(bucket, int(&row.get_item("rows")?)?)?;
    }
    for (relation_name, window, bucket) in windows {
        let by_bucket = counts.get_item(&relation_name)?.map_or_else(|| Ok(PyDict::new(py)), |found| found.cast_into::<PyDict>().map_err(PyErr::from))?;
        result.set_item(
            &relation_name,
            py_kwargs!(py;
                windowSeconds = window,
                bucketSeconds = bucket,
                buckets = zero_filled_buckets(&by_bucket, window, bucket)?,
            )?,
        )?;
    }
    Ok(result)
}

fn rows_per_second<'py>(throughput: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = throughput.py();
    if throughput.is_none() {
        return Ok(none(py));
    }
    let buckets = throughput.get_item("buckets")?;
    let window: i64 = throughput.get_item("windowSeconds")?.extract()?;
    if window == 0 {
        return Ok(none(py));
    }
    let float = builtin(py, "float")?;
    let total = float.call1((builtin(py, "sum")?.call1((buckets,))?,))?;
    builtin(py, "round")?.call1((total.div(float.call1((window,))?)?, 3))
}

fn partitions<'py>(warehouse: Option<Any<'py>>, kafka_lag: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = kafka_lag.py();
    if warehouse.is_none() && kafka_lag.is_none() {
        return Ok(none(py));
    }
    let lag_by_partition: Bound<'py, PyDict> = PyDict::new(py);
    if !kafka_lag.is_none() {
        for item in kafka_lag.getattr("partitions")?.try_iter()? {
            let item = item?;
            lag_by_partition.set_item(item.getattr("partition")?, item)?;
        }
    }
    let broker_value = |lag: &Option<Any<'py>>, name: &str| -> PyResult<Any<'py>> {
        match lag {
            None => Ok(none(py)),
            Some(lag) => lag.getattr(name),
        }
    };
    let by_id: Bound<'py, PyDict> = PyDict::new(py);
    if let Some(warehouse) = warehouse.filter(|value| value.is_truthy().unwrap_or(true)) {
        for observation in warehouse.call_method0("values")?.try_iter()? {
            let observation = observation?;
            let partition = observation.get_item(0)?;
            let lag: Option<Any<'py>> = lag_by_partition.get_item(&partition)?;
            by_id.set_item(
                &partition,
                py_kwargs!(py;
                    partition = &partition,
                    maxOffset = observation.get_item(1)?,
                    newestEventAt = observation.get_item(3)?,
                    committedOffset = broker_value(&lag, "committed_offset")?,
                    endOffset = broker_value(&lag, "end_offset")?,
                    kafkaLagMessages = broker_value(&lag, "lag_messages")?,
                )?,
            )?;
        }
    }
    for (partition, lag) in lag_by_partition.iter() {
        if by_id.contains(&partition)? {
            continue;
        }
        by_id.set_item(
            &partition,
            py_kwargs!(py;
                partition = &partition,
                maxOffset = none(py),
                newestEventAt = none(py),
                committedOffset = lag.getattr("committed_offset")?,
                endOffset = lag.getattr("end_offset")?,
                kafkaLagMessages = lag.getattr("lag_messages")?,
            )?,
        )?;
    }
    let mut ordered: Vec<Any<'py>> = Vec::new();
    for partition in builtin(py, "sorted")?.call1((&by_id,))?.try_iter()? {
        ordered.push(by_id.as_any().get_item(partition?)?);
    }
    Ok(PyList::new(py, ordered)?.into_any())
}

#[allow(clippy::too_many_arguments)]
fn source_states<'py>(
    analysis: &Any<'py>,
    connection: &Any<'py>,
    database: &str,
    stats: &Any<'py>,
    extents: &Bound<'py, PyDict>,
    captured_at: &str,
    reader: &Any<'py>,
    warehouse: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    struct Input<'py> {
        source: Any<'py>,
        relation_name: Any<'py>,
        extent: Any<'py>,
        newest: Any<'py>,
        age: Any<'py>,
        kafka_lag: Any<'py>,
    }
    let mut inputs: Vec<Input<'py>> = Vec::new();
    for source in sources(analysis)? {
        let relation_name = relation_name_of(analysis, &source.getattr("key")?)?;
        let extent = get_or_empty(extents.as_any(), &relation_name)?;
        let newest = optional_str(get(&extent, "newest")?)?;
        let age = age_seconds(&newest, captured_at)?;
        let kafka_lag = kafka_lag(analysis, &source, database, reader)?;
        inputs.push(Input { source, relation_name, extent, newest, age, kafka_lag });
    }
    let newest_ages: Bound<'py, PyDict> = PyDict::new(py);
    for item in &inputs {
        if extents.contains(&item.relation_name)? {
            newest_ages.set_item(&item.relation_name, &item.age)?;
        }
    }
    let throughputs = throughputs(connection, database, &newest_ages)?;
    let states: Bound<'py, PyDict> = PyDict::new(py);
    for item in inputs {
        let throughput = get(throughputs.as_any(), &item.relation_name)?;
        let source_stats = get_or_empty(stats, &item.relation_name)?;
        let state = py_kwargs!(py;
            relationName = &item.relation_name,
            rows = get(&source_stats, "rows")?,
            oldestEventAt = optional_str(get(&item.extent, "oldest")?)?,
            newestEventAt = &item.newest,
            lastArrivalSeconds = &item.age,
            kafkaLagMessages = if item.kafka_lag.is_none() { none(py) } else { item.kafka_lag.getattr("total_messages")? },
            freshness = freshness(&item.newest, captured_at, &item.source.getattr("source")?.getattr("freshness")?)?,
            throughput = &throughput,
            rowsPerSecond = rows_per_second(&throughput)?,
            partitions = partitions(warehouse.get_item(&item.relation_name)?, &item.kafka_lag)?,
        )?;
        states.set_item(item.source.getattr("key")?.getattr("name")?, state)?;
    }
    Ok(states)
}

/// Read the warehouse once and assemble the complete live overlay.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, database, kafka_lag_reader = None, warehouse_health_reader = None))]
pub(crate) fn build_state_payload<'py>(
    py: Python<'py>,
    analysis: Any<'py>,
    connection: Any<'py>,
    database: String,
    kafka_lag_reader: Option<Any<'py>>,
    warehouse_health_reader: Option<Any<'py>>,
) -> PyResult<Any<'py>> {
    let reader: Any<'py> = kafka_lag_reader.unwrap_or_else(|| none(py));
    if !reader.is_none() {
        for source in sources(&analysis)? {
            kafka_lag(&analysis, &source, &database, &reader)?;
        }
    }
    let captured = connection.call_method0("capture_warehouse_timestamp")?;
    let captured_at: String = captured.extract()?;
    let health_reader: Any<'py> = match warehouse_health_reader {
        Some(health_reader) if health_reader.is_truthy()? => health_reader,
        _ => attr(py, "sqltide.dev_server.classes.warehouse_health_reader", "WarehouseHealthReader")?.call0()?,
    };
    let health = health_reader.call_method(
        "read",
        (),
        Some(&py_kwargs!(py;
            connection = &connection,
            database = &database,
            measured_at = &captured,
            managed_source_names = managed_kafka_relation_names(&analysis)?,
            include_dead_letters = has_quarantine_model(&analysis)?,
        )?),
    )?;
    let catalog = connection.call_method1("load_catalog", (&database,))?;
    let bindings = active_bindings(&connection, &database)?;
    let stats = relation_stats(&connection, &database, &bindings)?;
    let lineage: Vec<String> = lineage_relation_names(&catalog)?;
    let mut source_relations: Vec<String> = Vec::new();
    for source in sources(&analysis)? {
        let relation_name: String = relation_name_of(&analysis, &source.getattr("key")?)?.extract()?;
        if lineage.contains(&relation_name) {
            source_relations.push(relation_name);
        }
    }
    let warehouse = warehouse_partitions(&connection, &database, source_relations.clone())?;
    let other_relations: Vec<String> = lineage.into_iter().filter(|name| !source_relations.contains(name)).collect();
    let extents = extents(&connection, &database, other_relations)?;
    source_extents(&warehouse, &extents)?;
    let baselines = fingerprint_baselines(&analysis, &connection, &database)?;
    let mut model_relation_names: Vec<String> = Vec::new();
    for model in analysis.getattr("compiled_project")?.getattr("models")?.try_iter()? {
        let model = model?;
        if is_table_model(&model)? {
            model_relation_names.push(relation_name_of(&analysis, &model.getattr("key")?)?.extract()?);
        }
    }
    let activity = read_model_activity(
        py,
        connection.clone(),
        &database,
        model_relation_names,
        &captured_at,
        Some(bindings.clone().into_any()),
        ACTIVITY_WINDOW_SECONDS,
    )?;
    let adapter_name = connection.getattr("adapter_identity")?.getattr("name")?;
    let database_value = database.as_str().into_pyobject(py)?.into_any();
    Ok(py_kwargs!(py;
        capturedAt = &captured,
        warehouseHealth = warehouse_health_payload(&health, &captured, adapter_name, &database_value)?,
        models = model_states(&analysis, &catalog, &baselines, &stats, &extents, &captured_at, &activity)?,
        sources = source_states(&analysis, &connection, &database, &stats, &extents, &captured_at, &reader, &warehouse)?,
    )?
    .into_any())
}

fn broker_topic_item<'py>(topic: &Any<'py>, broker_list: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = topic.py();
    py_kwargs!(py;
        name = topic.getattr("name")?,
        brokerList = broker_list,
        partitions = topic.getattr("partition_count")?,
        replicationFactor = topic.getattr("replication_factor")?,
        internal = topic.getattr("internal")?,
        sources = PyList::empty(py),
        lagMessages = none(py),
        retainedRows = none(py),
        retainedBytes = none(py),
    )
}

/// `(source_name, relation_name, kafka)` for every managed Kafka source.
fn managed_topic_sources<'py>(analysis: &Any<'py>) -> PyResult<Vec<(Any<'py>, Any<'py>, Any<'py>)>> {
    let mut entries: Vec<(Any<'py>, Any<'py>, Any<'py>)> = Vec::new();
    for source in sources(analysis)? {
        if !is_kafka_landing(&source)? {
            continue;
        }
        let key = source.getattr("key")?;
        let Some(managed) = managed_source_of(analysis, &key)? else {
            continue;
        };
        let name = key.getattr("name")?;
        let relation_name = relation_name_of(analysis, &key)?;
        entries.push((name, relation_name, kafka_settings(&source, &managed)?));
    }
    Ok(entries)
}

fn retained_stats<'py>(connection: &Any<'py>, database: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let stats: Bound<'py, PyDict> = PyDict::new(py);
    if connection.is_none() || database.is_none() {
        return Ok(stats);
    }
    for row in named_rows(connection, build_relation_stats_query(&database.extract::<String>()?))? {
        let entry = py_kwargs!(py;
            rows = int(&row.get_item("total_rows")?)?,
            bytes = int(&row.get_item("total_bytes")?)?,
        )?;
        stats.set_item(row.get_item("name")?.str()?, entry)?;
    }
    Ok(stats)
}

/// Merge broker topic inventories with managed-source lag and retained stats.
#[pyfunction]
#[pyo3(signature = (*, analysis, connection, database, topic_reader, kafka_lag_reader))]
pub(crate) fn build_topics_payload<'py>(
    py: Python<'py>,
    analysis: Any<'py>,
    connection: Any<'py>,
    database: Any<'py>,
    topic_reader: Any<'py>,
    kafka_lag_reader: Any<'py>,
) -> PyResult<Any<'py>> {
    let managed = managed_topic_sources(&analysis)?;
    if managed.is_empty() {
        return Ok(py_kwargs!(py;
            available = false,
            reason = NO_MANAGED_SOURCES_REASON,
            pendingBrokers = PyList::empty(py),
            topics = PyList::empty(py),
        )?
        .into_any());
    }
    let snapshots: Bound<'py, PyDict> = PyDict::new(py);
    for (_, _, kafka) in &managed {
        let broker_list = kafka.getattr("broker_list")?;
        if !snapshots.contains(&broker_list)? {
            let snapshot = topic_reader.call_method("read", (), Some(&py_kwargs!(py; kafka = kafka)?))?;
            snapshots.set_item(broker_list, snapshot)?;
        }
    }
    let stats = retained_stats(&connection, &database)?;
    let topics: Bound<'py, PyDict> = PyDict::new(py);
    for (broker_list, snapshot) in snapshots.iter() {
        if snapshot.is_none() {
            continue;
        }
        for topic in snapshot.getattr("topics")?.try_iter()? {
            let topic = topic?;
            topics.set_item(topic.getattr("name")?, broker_topic_item(&topic, &broker_list)?)?;
        }
    }
    let lag_database = if database.is_truthy()? { database.clone() } else { "".into_pyobject(py)?.into_any() };
    for (source_name, relation_name, kafka) in &managed {
        let topic = kafka.getattr("topic")?;
        let fallback = py_kwargs!(py;
            name = &topic,
            brokerList = kafka.getattr("broker_list")?,
            partitions = none(py),
            replicationFactor = none(py),
            internal = false,
            sources = PyList::empty(py),
            lagMessages = none(py),
            retainedRows = none(py),
            retainedBytes = none(py),
        )?;
        let item = topics.as_any().call_method1("setdefault", (&topic, fallback))?;
        item.get_item("sources")?
            .call_method1("append", (py_kwargs!(py; name = source_name, relationName = relation_name)?,))?;
        let lag = kafka_lag_reader.call_method("read", (), Some(&py_kwargs!(py; kafka = kafka, database = &lag_database)?))?;
        item.set_item("lagMessages", if lag.is_none() { none(py) } else { lag.getattr("total_messages")? })?;
        let relation_stats = get(stats.as_any(), relation_name)?;
        let stat = |name: &str| -> PyResult<Any<'py>> {
            if relation_stats.is_none() { Ok(none(py)) } else { relation_stats.get_item(name) }
        };
        item.set_item("retainedRows", stat("rows")?)?;
        item.set_item("retainedBytes", stat("bytes")?)?;
    }
    let mut pending: Vec<Any<'py>> = Vec::new();
    for (broker_list, snapshot) in snapshots.iter() {
        if snapshot.is_none() {
            pending.push(broker_list);
        }
    }
    let mut ordered: Vec<Any<'py>> = Vec::new();
    for name in builtin(py, "sorted")?.call1((&topics,))?.try_iter()? {
        ordered.push(topics.as_any().get_item(name?)?);
    }
    Ok(py_kwargs!(py;
        available = true,
        reason = none(py),
        pendingBrokers = builtin(py, "sorted")?.call1((PyList::new(py, pending)?,))?,
        topics = PyList::new(py, ordered)?,
    )?
    .into_any())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_relation_stats_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_parts_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_extents_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_throughputs_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_partitions_query, module)?)?;
    module.add_function(wrap_pyfunction!(choose_throughput_window, module)?)?;
    module.add_function(wrap_pyfunction!(apply_bound_relation_stats, module)?)?;
    module.add_function(wrap_pyfunction!(build_state_payload, module)?)?;
    module.add_function(wrap_pyfunction!(build_topics_payload, module)?)
}

