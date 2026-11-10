//! Cached, non-blocking broker reads and retained warehouse health for the development UI.
//!
//! The kafka-python-ng clients stay in the Python collectors, which only open, reuse and close
//! them and return plain broker data; client configuration, client keys, offset math, snapshot
//! building and caching live here.

use std::sync::Mutex;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PyString, PyTuple};
use sqltide_clickhouse::resources::database_scoped_consumer_group;

use super::support::{attr, is_instance, unpack};
use crate::handling::{while_handling, within};
use crate::py_kwargs;

const DEV_MODELS: &str = "sqltide.dev_server.models";
const KAFKA_REQUEST_TIMEOUT_MS: i64 = 5_000;
const KAFKA_SECURITY_PROTOCOL_CONFIG_NAME: &str = "security_protocol";
const TRANSLATED_SETTINGS: [(&str, &str); 4] = [
    (
        "kafka_security_protocol",
        KAFKA_SECURITY_PROTOCOL_CONFIG_NAME,
    ),
    ("kafka_sasl_mechanism", "sasl_mechanism"),
    ("kafka_sasl_username", "sasl_plain_username"),
    ("kafka_sasl_password", "sasl_plain_password"),
];
const WAREHOUSE_HEALTH_CACHE_SECONDS: f64 = 15.0;
const HEALTH_UNAVAILABLE_WARNING: &str = "Warehouse diagnostics are unavailable.";

type Any<'py> = Bound<'py, PyAny>;

/// Translate resolved managed-source Kafka settings into kafka-python config.
#[pyfunction]
#[pyo3(signature = (*, kafka))]
pub(crate) fn build_kafka_client_config<'py>(kafka: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = kafka.py();
    let settings: Any<'py> = settings_or_empty(kafka)?;
    let config: Bound<'py, PyDict> = PyDict::new(py);
    let servers: Bound<'py, PyList> = PyList::empty(py);
    for item in kafka
        .getattr("broker_list")?
        .call_method1("split", (",",))?
        .try_iter()?
    {
        servers.append(item?.call_method0("strip")?)?;
    }
    config.set_item("bootstrap_servers", servers)?;
    config.set_item("request_timeout_ms", KAFKA_REQUEST_TIMEOUT_MS)?;
    config.set_item("api_version_auto_timeout_ms", KAFKA_REQUEST_TIMEOUT_MS)?;
    for (source_name, target_name) in TRANSLATED_SETTINGS {
        let value: Any<'py> = settings.call_method1("get", (source_name,))?;
        if value.is_none() {
            continue;
        }
        if target_name == KAFKA_SECURITY_PROTOCOL_CONFIG_NAME {
            config.set_item(target_name, value.call_method0("upper")?)?;
        } else {
            config.set_item(target_name, value)?;
        }
    }
    Ok(config)
}

/// `kafka.settings or {}`.
fn settings_or_empty<'py>(kafka: &Any<'py>) -> PyResult<Any<'py>> {
    let settings: Any<'py> = kafka.getattr("settings")?;
    if settings.is_truthy()? {
        Ok(settings)
    } else {
        Ok(PyDict::new(kafka.py()).into_any())
    }
}

/// Identify sources that can safely share authenticated Kafka clients.
#[pyfunction]
#[pyo3(signature = (*, kafka))]
pub(crate) fn kafka_client_key<'py>(kafka: &Any<'py>) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = kafka.py();
    let items: Any<'py> = settings_or_empty(kafka)?.call_method0("items")?;
    let sorted: Any<'py> = attr(py, "builtins", "sorted")?.call1((items,))?;
    PyTuple::new(
        py,
        [
            kafka.getattr("broker_list")?,
            PyTuple::new(py, sorted.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any(),
        ],
    )
}

/// Build exact lag, leaving totals unknown when a non-empty partition has no commit.
#[pyfunction]
#[pyo3(signature = (*, partition_ids, committed_offsets, end_offsets))]
pub(crate) fn build_kafka_lag_snapshot<'py>(
    partition_ids: &Any<'py>,
    committed_offsets: &Any<'py>,
    end_offsets: &Any<'py>,
) -> PyResult<Any<'py>> {
    let py: Python<'py> = partition_ids.py();
    let snapshot_class: Any<'py> = attr(py, DEV_MODELS, "KafkaLagSnapshot")?;
    if !partition_ids.is_truthy()? {
        return snapshot_class.call(
            (),
            Some(&py_kwargs!(py; total_messages = py.None(), partitions = PyTuple::empty(py))?),
        );
    }
    let partition_class: Any<'py> = attr(py, DEV_MODELS, "KafkaPartitionLag")?;
    let zero: Any<'py> = 0_i64.into_pyobject(py)?.into_any();
    let mut total_messages: Any<'py> = zero.clone();
    let mut complete: bool = true;
    let mut partitions: Vec<Any<'py>> = Vec::new();
    for partition in attr(py, "builtins", "sorted")?
        .call1((partition_ids,))?
        .try_iter()?
    {
        let partition: Any<'py> = partition?;
        let end_offset: Any<'py> = end_offsets.get_item(&partition)?;
        let committed_offset: Any<'py> = committed_offsets.call_method1("get", (&partition,))?;
        let lag_messages: Any<'py> = if committed_offset.is_none() && end_offset.eq(0)? {
            zero.clone()
        } else if committed_offset.is_none() {
            complete = false;
            py.None().into_bound(py)
        } else {
            // `max(0, end_offset - committed_offset)`.
            let difference: Any<'py> = end_offset.sub(&committed_offset)?;
            if difference.gt(&zero)? {
                difference
            } else {
                zero.clone()
            }
        };
        if !lag_messages.is_none() {
            total_messages = total_messages.add(&lag_messages)?;
        }
        partitions.push(partition_class.call(
            (),
            Some(&py_kwargs!(py;
                partition = &partition,
                committed_offset = &committed_offset,
                end_offset = &end_offset,
                lag_messages = &lag_messages,
            )?),
        )?);
    }
    let total: Any<'py> = if complete {
        total_messages
    } else {
        py.None().into_bound(py)
    };
    snapshot_class.call(
        (),
        Some(&py_kwargs!(py; total_messages = total, partitions = PyTuple::new(py, partitions)?)?),
    )
}

fn is_sequence(value: &Any<'_>) -> bool {
    value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>()
}

/// Map raw kafka-python describe_topics metadata into the topic inventory.
#[pyfunction]
#[pyo3(signature = (*, metadata))]
pub(crate) fn build_kafka_topics_snapshot<'py>(metadata: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = metadata.py();
    let mapping: Any<'py> = attr(py, "collections.abc", "Mapping")?;
    let topic_class: Any<'py> = attr(py, DEV_MODELS, "KafkaTopicInfo")?;
    let topics: Bound<'py, PyList> = PyList::empty(py);
    for entry in metadata.try_iter()? {
        let entry: Any<'py> = entry?;
        let partitions: Any<'py> = entry.call_method1("get", ("partitions", PyTuple::empty(py)))?;
        let mut partition_list: Vec<Any<'py>> = Vec::new();
        if is_sequence(&partitions) {
            for partition in partitions.try_iter()? {
                let partition: Any<'py> = partition?;
                if partition.is_instance(&mapping)? {
                    partition_list.push(partition);
                }
            }
        }
        let mut replication_factor: usize = 0;
        for partition in &partition_list {
            let replicas: Any<'py> = partition.call_method1("get", ("replicas",))?;
            if is_sequence(&replicas) {
                replication_factor = replication_factor.max(replicas.len()?);
            }
        }
        topics.append(topic_class.call(
            (),
            Some(&py_kwargs!(py;
                name = entry.call_method1("get", ("topic", ""))?.str()?,
                partition_count = partition_list.len(),
                replication_factor = replication_factor,
                internal = entry.call_method1("get", ("is_internal", false))?.is_truthy()?,
            )?),
        )?)?;
    }
    let by_name: Any<'py> = attr(py, "operator", "attrgetter")?.call1(("name",))?;
    let sorted: Any<'py> =
        attr(py, "builtins", "sorted")?.call((topics,), Some(&py_kwargs!(py; key = by_name)?))?;
    let ordered: Bound<'py, PyTuple> =
        PyTuple::new(py, sorted.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
    attr(py, DEV_MODELS, "KafkaTopicsSnapshot")?.call((), Some(&py_kwargs!(py; topics = ordered)?))
}

/// One cache of broker snapshots refreshed on a worker pool: reads return the cached value at
/// once and schedule a refresh when it is stale; a failed refresh keeps the previous value.
struct RefreshCache {
    clock: Py<PyAny>,
    lock: Py<PyAny>,
    entries: Py<PyDict>,
    refreshing: Py<PySet>,
    executor: Py<PyAny>,
    collector: Py<PyAny>,
    retained_collector: Option<Py<PyAny>>,
    cache_seconds: f64,
    failure_retry_seconds: f64,
    max_entries: usize,
    max_pending: usize,
}

struct CachePolicy {
    cache_seconds: f64,
    failure_retry_seconds: f64,
    max_entries: usize,
    max_pending: usize,
    max_workers: usize,
    thread_name_prefix: &'static str,
    collector_module: &'static str,
    collector_class: &'static str,
}

impl RefreshCache {
    fn new(
        py: Python<'_>,
        clock: Option<Any<'_>>,
        collector: Option<Any<'_>>,
        policy: CachePolicy,
    ) -> PyResult<Self> {
        let clock: Any<'_> = match clock {
            Some(clock) => clock,
            None => attr(py, "time", "monotonic")?,
        };
        let (collector, retained_collector) = match collector {
            Some(collector) => (collector.unbind(), None),
            None => {
                let retained: Any<'_> =
                    attr(py, policy.collector_module, policy.collector_class)?.call0()?;
                (retained.clone().unbind(), Some(retained.unbind()))
            }
        };
        let executor: Any<'_> = attr(py, "concurrent.futures", "ThreadPoolExecutor")?.call(
            (),
            Some(&py_kwargs!(py; max_workers = policy.max_workers, thread_name_prefix = policy.thread_name_prefix)?),
        )?;
        Ok(Self {
            clock: clock.unbind(),
            lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
            entries: PyDict::new(py).unbind(),
            refreshing: PySet::empty(py)?.unbind(),
            executor: executor.unbind(),
            collector,
            retained_collector,
            cache_seconds: policy.cache_seconds,
            failure_retry_seconds: policy.failure_retry_seconds,
            max_entries: policy.max_entries,
            max_pending: policy.max_pending,
        })
    }

    /// The cached snapshot for `key`, scheduling `refresh` with `arguments` when it is stale.
    fn read<'py>(
        &self,
        py: Python<'py>,
        key: &Any<'py>,
        refresh: Any<'py>,
        arguments: Bound<'py, PyDict>,
    ) -> PyResult<Any<'py>> {
        let now: Any<'py> = self.clock.bind(py).call0()?;
        within(self.lock.bind(py), || {
            let entry: Option<Any<'py>> = self.entries.bind(py).get_item(key)?;
            if let Some(entry) = &entry
                && now.lt(entry.get_item(0)?)?
            {
                return entry.get_item(1);
            }
            let refreshing: &Bound<'py, PySet> = self.refreshing.bind(py);
            if !refreshing.contains(key)? && refreshing.len() < self.max_pending {
                refreshing.add(key)?;
                self.executor
                    .bind(py)
                    .call_method("submit", (refresh,), Some(&arguments))?;
            }
            match entry {
                None => Ok(py.None().into_bound(py)),
                Some(entry) => entry.get_item(1),
            }
        })
    }

    /// Record one refresh: `Some(snapshot)` when it succeeded, `None` when it raised.
    fn store<'py>(
        &self,
        py: Python<'py>,
        key: &Any<'py>,
        snapshot: Option<Any<'py>>,
    ) -> PyResult<()> {
        within(self.lock.bind(py), || {
            let entries: &Bound<'py, PyDict> = self.entries.bind(py);
            let previous: Option<Any<'py>> = entries.get_item(key)?;
            if entries.len() >= self.max_entries && !entries.contains(key)? {
                // `min(cache, key=expiry)`: the first entry with the smallest expiry.
                let mut oldest: Option<(Any<'py>, Any<'py>)> = None;
                for (candidate, value) in entries.iter() {
                    let expiry: Any<'py> = value.get_item(0)?;
                    let replace: bool = match &oldest {
                        None => true,
                        Some((_, best)) => expiry.lt(best)?,
                    };
                    if replace {
                        oldest = Some((candidate, expiry));
                    }
                }
                if let Some((oldest_key, _)) = oldest {
                    entries.call_method1("pop", (oldest_key,))?;
                }
            }
            let now: Any<'py> = self.clock.bind(py).call0()?;
            let succeeded: bool = snapshot.is_some();
            let expiry: Any<'py> = now.add(if succeeded {
                self.cache_seconds
            } else {
                self.failure_retry_seconds
            })?;
            let value: Any<'py> = match (snapshot, previous) {
                (Some(snapshot), _) => snapshot,
                (None, None) => py.None().into_bound(py),
                (None, Some(previous)) => previous.get_item(1)?,
            };
            entries.set_item(key, PyTuple::new(py, [expiry, value])?)?;
            self.refreshing.bind(py).discard(key)?;
            Ok(())
        })
    }

    /// `try: snapshot = collect() except Exception: failed`, then record it.
    fn refresh<'py>(
        &self,
        py: Python<'py>,
        key: &Any<'py>,
        collect: impl FnOnce() -> PyResult<Any<'py>>,
    ) -> PyResult<()> {
        let snapshot: Option<Any<'py>> = match collect() {
            Ok(snapshot) => Some(snapshot),
            Err(error) if error.is_instance_of::<PyException>(py) => None,
            Err(error) => return Err(error),
        };
        self.store(py, key, snapshot)
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.executor.bind(py).call_method(
            "shutdown",
            (),
            Some(&py_kwargs!(py; wait = true, cancel_futures = true)?),
        )?;
        if let Some(collector) = &self.retained_collector {
            collector.bind(py).call_method0("close")?;
        }
        Ok(())
    }
}

/// Return cached lag immediately and refresh stale values on a daemon thread.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct KafkaLagReader {
    cache: RefreshCache,
}

#[pymethods]
impl KafkaLagReader {
    #[new]
    #[pyo3(signature = (*, clock = None, collector = None))]
    fn new(py: Python<'_>, clock: Option<Any<'_>>, collector: Option<Any<'_>>) -> PyResult<Self> {
        let policy = CachePolicy {
            cache_seconds: 60.0,
            failure_retry_seconds: 10.0,
            max_entries: 128,
            max_pending: 32,
            max_workers: 4,
            thread_name_prefix: "sqltide-kafka-lag",
            collector_module: "sqltide.dev_server.classes.kafka_lag_collector",
            collector_class: "KafkaLagCollector",
        };
        Ok(Self {
            cache: RefreshCache::new(py, clock, collector, policy)?,
        })
    }

    /// Return current cached lag and schedule a non-blocking refresh when needed.
    #[pyo3(signature = (*, kafka, database))]
    fn read<'py>(
        slf: &Bound<'py, Self>,
        kafka: &Any<'py>,
        database: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let key: Bound<'py, PyTuple> = PyTuple::new(
            py,
            [
                kafka_client_key(kafka)?.into_any(),
                kafka.getattr("topic")?,
                kafka.getattr("consumer_group")?,
                database.clone(),
            ],
        )?;
        let arguments = py_kwargs!(py; key = &key, kafka = kafka, database = database)?;
        slf.get()
            .cache
            .read(py, key.as_any(), slf.getattr("_refresh")?, arguments)
    }

    /// Finish in-flight refreshes and close retained broker connections.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.cache.close(py)
    }

    #[pyo3(name = "_refresh", signature = (*, key, kafka, database))]
    fn refresh<'py>(&self, key: &Any<'py>, kafka: &Any<'py>, database: &Any<'py>) -> PyResult<()> {
        let py: Python<'py> = key.py();
        self.cache
            .refresh(py, key, || self.collect(kafka, database))
    }
}

impl KafkaLagReader {
    /// Read one topic's committed and end offsets through the collector and derive exact lag.
    fn collect<'py>(&self, kafka: &Any<'py>, database: &Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = kafka.py();
        let consumer_group: Any<'py> = kafka.getattr("consumer_group")?;
        if consumer_group.is_none() {
            let empty =
                py_kwargs!(py; total_messages = py.None(), partitions = PyTuple::empty(py))?;
            return attr(py, DEV_MODELS, "KafkaLagSnapshot")?.call((), Some(&empty));
        }
        let key: Bound<'py, PyTuple> = kafka_client_key(kafka)?;
        let config: Bound<'py, PyDict> = build_kafka_client_config(kafka)?;
        let topic: Any<'py> = kafka.getattr("topic")?;
        let scoped: String = database_scoped_consumer_group(
            &consumer_group.extract::<String>()?,
            &database.extract::<String>()?,
        );
        let raw: Any<'py> = self.cache.collector.bind(py).call(
            (),
            Some(&py_kwargs!(py; key = &key, config = &config, topic = &topic, consumer_group = scoped)?),
        )?;
        let parts: Vec<Any<'py>> = unpack(&raw, 3)?;
        let int: Any<'py> = attr(py, "builtins", "int")?;
        let committed_offsets: Bound<'py, PyDict> = PyDict::new(py);
        for item in parts[0].try_iter()? {
            let [committed_topic, partition, offset]: [Any<'py>; 3] = unpack(&item?, 3)?
                .try_into()
                .expect("three unpacked values");
            if committed_topic.eq(&topic)? && !offset.is_none() {
                let offset: Any<'py> = int.call1((offset,))?;
                if offset.ge(0)? {
                    committed_offsets.set_item(partition, offset)?;
                }
            }
        }
        let end_offsets: Bound<'py, PyDict> = PyDict::new(py);
        for item in parts[2].try_iter()? {
            let [partition, offset]: [Any<'py>; 2] =
                unpack(&item?, 2)?.try_into().expect("two unpacked values");
            end_offsets.set_item(partition, int.call1((offset,))?)?;
        }
        let partition_ids: Bound<'py, PyFrozenSet> =
            PyFrozenSet::new(py, parts[1].try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
        build_kafka_lag_snapshot(
            partition_ids.as_any(),
            committed_offsets.as_any(),
            end_offsets.as_any(),
        )
    }
}

/// Return cached topic inventories immediately and refresh stale entries.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct KafkaTopicReader {
    cache: RefreshCache,
}

#[pymethods]
impl KafkaTopicReader {
    #[new]
    #[pyo3(signature = (*, clock = None, collector = None))]
    fn new(py: Python<'_>, clock: Option<Any<'_>>, collector: Option<Any<'_>>) -> PyResult<Self> {
        let policy = CachePolicy {
            cache_seconds: 600.0,
            failure_retry_seconds: 600.0,
            max_entries: 32,
            max_pending: usize::MAX,
            max_workers: 2,
            thread_name_prefix: "sqltide-kafka-topics",
            collector_module: "sqltide.dev_server.classes.kafka_topic_collector",
            collector_class: "KafkaTopicCollector",
        };
        Ok(Self {
            cache: RefreshCache::new(py, clock, collector, policy)?,
        })
    }

    /// Return the cached inventory and schedule a non-blocking refresh when stale.
    #[pyo3(signature = (*, kafka))]
    fn read<'py>(slf: &Bound<'py, Self>, kafka: &Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let key: Bound<'py, PyTuple> = kafka_client_key(kafka)?;
        let arguments = py_kwargs!(py; key = &key, kafka = kafka)?;
        slf.get()
            .cache
            .read(py, key.as_any(), slf.getattr("_refresh")?, arguments)
    }

    /// Finish in-flight refreshes and close retained broker connections.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.cache.close(py)
    }

    #[pyo3(name = "_refresh", signature = (*, key, kafka))]
    fn refresh<'py>(&self, key: &Any<'py>, kafka: &Any<'py>) -> PyResult<()> {
        let py: Python<'py> = key.py();
        self.cache.refresh(py, key, || {
            let config: Bound<'py, PyDict> = build_kafka_client_config(kafka)?;
            let metadata: Any<'py> = self
                .cache
                .collector
                .bind(py)
                .call((), Some(&py_kwargs!(py; key = key, config = config)?))?;
            build_kafka_topics_snapshot(&metadata)
        })
    }
}

/// Preserve the last usable warehouse health snapshot while marking failed refreshes stale.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct WarehouseHealthReader {
    clock: Py<PyAny>,
    cache_seconds: Py<PyAny>,
    last_usable_by_target: Py<PyDict>,
}

impl WarehouseHealthReader {
    fn create(
        py: Python<'_>,
        clock: Option<Any<'_>>,
        cache_seconds: Option<Any<'_>>,
    ) -> PyResult<Self> {
        let clock: Any<'_> = match clock {
            Some(clock) => clock,
            None => attr(py, "time", "monotonic")?,
        };
        let cache_seconds: Any<'_> = match cache_seconds {
            Some(seconds) => seconds,
            None => WAREHOUSE_HEALTH_CACHE_SECONDS.into_pyobject(py)?.into_any(),
        };
        Ok(Self {
            clock: clock.unbind(),
            cache_seconds: cache_seconds.unbind(),
            last_usable_by_target: PyDict::new(py).unbind(),
        })
    }
}

#[pymethods]
impl WarehouseHealthReader {
    #[new]
    #[pyo3(signature = (*, clock = None, cache_seconds = None))]
    fn new(
        py: Python<'_>,
        clock: Option<Any<'_>>,
        cache_seconds: Option<Any<'_>>,
    ) -> PyResult<Self> {
        Self::create(py, clock, cache_seconds)
    }

    /// Read diagnostics and retain explicit failure semantics.
    #[pyo3(signature = (*, connection, database, measured_at, managed_source_names = None, include_dead_letters = None))]
    fn read<'py>(
        &self,
        connection: &Any<'py>,
        database: &Any<'py>,
        measured_at: &Any<'py>,
        managed_source_names: Option<Any<'py>>,
        include_dead_letters: Option<Any<'py>>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = connection.py();
        let managed_source_names: Any<'py> =
            managed_source_names.unwrap_or_else(|| PyTuple::empty(py).into_any());
        let include_dead_letters: Any<'py> = include_dead_letters
            .unwrap_or_else(|| pyo3::types::PyBool::new(py, false).to_owned().into_any());
        let target: Bound<'py, PyTuple> = PyTuple::new(
            py,
            [
                connection.getattr("adapter_identity")?.getattr("name")?,
                database.clone(),
                managed_source_names.clone(),
                include_dead_letters.clone(),
            ],
        )?;
        let now: Any<'py> = self.clock.bind(py).call0()?;
        let cache: &Bound<'py, PyDict> = self.last_usable_by_target.bind(py);
        let cached: Option<Any<'py>> = cache.get_item(&target)?;
        if let Some(cached) = &cached
            && now
                .sub(cached.get_item(0)?)?
                .lt(self.cache_seconds.bind(py))?
        {
            return cached.get_item(1);
        }
        let loaded: PyResult<Any<'py>> = connection.call_method(
            "load_warehouse_health",
            (),
            Some(&py_kwargs!(py;
                database = database,
                managed_source_names = &managed_source_names,
                include_dead_letters = &include_dead_letters,
            )?),
        );
        let current: Any<'py> = match loaded {
            Ok(current) => current,
            Err(error) if error.is_instance_of::<PyException>(py) => unavailable_health(py)?,
            Err(error) => return Err(error),
        };
        let replace: Any<'py> = attr(py, "dataclasses", "replace")?;
        let unavailable: Any<'py> = attr(
            py,
            "sqltide.adapter.types",
            "AdapterWarehouseHealthAvailability",
        )?
        .getattr("UNAVAILABLE")?;
        if !current.getattr("availability")?.is(&unavailable) {
            let measured: Any<'py> = replace.call(
                (&current,),
                Some(&py_kwargs!(py; measured_at = measured_at)?),
            )?;
            cache.set_item(&target, PyTuple::new(py, [now, measured.clone()])?)?;
            return Ok(measured);
        }
        let Some(cached) = cached else {
            return replace.call(
                (&current,),
                Some(&py_kwargs!(py; measured_at = measured_at)?),
            );
        };
        let partial: Any<'py> = attr(
            py,
            "sqltide.adapter.types",
            "AdapterWarehouseHealthAvailability",
        )?
        .getattr("PARTIAL")?;
        replace.call(
            (cached.get_item(1)?,),
            Some(&py_kwargs!(py; availability = partial, warnings = current.getattr("warnings")?, stale = true)?),
        )
    }
}

/// The diagnostic snapshot reported when the warehouse could not be read at all.
fn unavailable_health(py: Python<'_>) -> PyResult<Any<'_>> {
    let none = py.None();
    attr(py, "sqltide.adapter.models", "AdapterWarehouseHealth")?.call(
        (),
        Some(&py_kwargs!(py;
            availability = "unavailable",
            status = "unknown",
            version = &none,
            uptime_seconds = &none,
            disks = PyTuple::empty(py),
            inode_total = &none,
            inode_free = &none,
            inode_status = "unknown",
            memory = &none,
            activity = &none,
            tables = &none,
            collection_duration_ms = 0,
            warnings = PyTuple::new(py, [PyString::new(py, HEALTH_UNAVAILABLE_WARNING)])?,
        )?),
    )
}

/// Own one private connection so overlay refreshes never take the shared query lock.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct OverlayReader {
    connection_factory: Py<PyAny>,
    kafka_lag_reader: Py<PyAny>,
    connection: Mutex<Option<Py<PyAny>>>,
    warehouse_health_reader: Py<WarehouseHealthReader>,
}

impl OverlayReader {
    fn current(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.connection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|connection| connection.clone_ref(py))
    }
}

#[pymethods]
impl OverlayReader {
    #[new]
    #[pyo3(signature = (*, connection_factory, kafka_lag_reader))]
    fn new(
        py: Python<'_>,
        connection_factory: Any<'_>,
        kafka_lag_reader: Any<'_>,
    ) -> PyResult<Self> {
        Ok(Self {
            connection_factory: connection_factory.unbind(),
            kafka_lag_reader: kafka_lag_reader.unbind(),
            connection: Mutex::new(None),
            warehouse_health_reader: Py::new(py, WarehouseHealthReader::create(py, None, None)?)?,
        })
    }

    /// Close the private connection so the snapshot releases it on shutdown.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let connection: Option<Py<PyAny>> = self
            .connection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(connection) = connection {
            connection.bind(py).call_method0("close")?;
        }
        Ok(())
    }

    /// Read the overlay, discarding the connection so the next read reconnects.
    #[pyo3(signature = (*, analysis, database))]
    fn read<'py>(&self, analysis: &Any<'py>, database: &Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = analysis.py();
        if self.current(py).is_none() {
            let opened: Py<PyAny> = self.connection_factory.bind(py).call0()?.unbind();
            *self
                .connection
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(opened);
        }
        let connection: Option<Py<PyAny>> = self.current(py);
        let result: PyResult<Any<'py>> = attr(py, "sqltide._native", "build_state_payload")?.call(
            (),
            Some(&py_kwargs!(py;
                analysis = analysis,
                connection = connection,
                database = database,
                kafka_lag_reader = &self.kafka_lag_reader,
                warehouse_health_reader = &self.warehouse_health_reader,
            )?),
        );
        match result {
            Err(error)
                if is_instance(py, &error, "sqltide.adapter.exceptions", "AdapterError")? =>
            {
                if let Err(closing) = self.close(py) {
                    return Err(while_handling(py, closing, &error));
                }
                Err(error)
            }
            other => other,
        }
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<KafkaLagReader>()?;
    module.add_class::<KafkaTopicReader>()?;
    module.add_class::<WarehouseHealthReader>()?;
    module.add_class::<OverlayReader>()?;
    module.add_function(wrap_pyfunction!(build_kafka_client_config, module)?)?;
    module.add_function(wrap_pyfunction!(kafka_client_key, module)?)?;
    module.add_function(wrap_pyfunction!(build_kafka_lag_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(build_kafka_topics_snapshot, module)?)
}
