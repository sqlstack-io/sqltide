"""Assemble the /api/state and /api/topics live overlays from warehouse reads."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import cast

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterManagedSource,
    AdapterWarehouseHealth,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapter.types import AdapterOptionalStateStatus
from sqltide.compiler.compile.models import CompiledSource, CompiledTableModel
from sqltide.compiler.discovery.constants import SECONDS_BY_DURATION_UNIT
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    KafkaSettings,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.classes.direct_model_fingerprint import DirectModelFingerprint
from sqltide.dev_server._helpers.payloads.activity_payload import read_model_activity
from sqltide.dev_server._helpers.payloads.model_drift_payload import model_drift_payload
from sqltide.dev_server.classes.kafka_lag_reader import KafkaLagReader
from sqltide.dev_server.classes.kafka_topic_reader import KafkaTopicReader
from sqltide.dev_server.classes.warehouse_health_reader import WarehouseHealthReader
from sqltide.dev_server.constants import THROUGHPUT_WINDOW_LADDER
from sqltide.dev_server.models import (
    KafkaLagSnapshot,
    KafkaPartitionLag,
    KafkaTopicInfo,
    KafkaTopicsSnapshot,
)
from sqltide.dev_server.types import Freshness

_LANDED_AT_COLUMN: str = "_replay_landed_at"

_NO_MANAGED_SOURCES_REASON: str = (
    "no managed Kafka sources define broker connections; the dev server has no "
    "credentials to inspect a cluster"
)


@dataclass(frozen=True)
class _ManagedTopicSource:
    """One managed source resolved to its broker connection and raw relation."""

    source_name: str
    relation_name: str
    kafka: KafkaSettings


@dataclass(frozen=True)
class _SourceStateInput:
    """Warehouse and broker observations needed to assemble one source state."""

    source: CompiledSource
    relation_name: str
    extent: dict[str, object]
    newest: str | None
    age: float | None
    kafka_lag: KafkaLagSnapshot | None


@dataclass(frozen=True)
class _WarehousePartitionObservation:
    """One retained source partition observation from the warehouse."""

    partition: int
    max_offset: int
    oldest: str
    newest: str
    rows: int


def build_state_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    kafka_lag_reader: KafkaLagReader | None = None,
    warehouse_health_reader: WarehouseHealthReader | None = None,
) -> dict[str, object]:
    """Read the warehouse once and assemble the complete live overlay."""

    if kafka_lag_reader is not None:
        for source in analysis.compiled_project.sources:
            _kafka_lag(
                analysis=analysis,
                source=source,
                database=database,
                reader=kafka_lag_reader,
            )
    captured_at: str = connection.capture_warehouse_timestamp()
    health_reader: WarehouseHealthReader = warehouse_health_reader or WarehouseHealthReader()
    managed_source_names: tuple[str, ...] = _managed_kafka_relation_names(analysis)
    warehouse_health: AdapterWarehouseHealth = health_reader.read(
        connection=connection,
        database=database,
        measured_at=captured_at,
        managed_source_names=managed_source_names,
    )
    catalog: CatalogSnapshot = connection.load_catalog(database)
    active_bindings: tuple[tuple[str, str], ...] = _active_bindings(
        connection=connection, database=database
    )
    stats: dict[str, dict[str, int]] = _relation_stats(
        connection=connection,
        database=database,
        active_bindings=active_bindings,
    )
    lineage_relations: tuple[str, ...] = _lineage_relation_names(catalog)
    source_relations: tuple[str, ...] = tuple(
        analysis.realized_project.relation_name_by_logical_key[source.key]
        for source in analysis.compiled_project.sources
        if analysis.realized_project.relation_name_by_logical_key[source.key] in lineage_relations
    )
    warehouse_partitions: dict[str, dict[int, _WarehousePartitionObservation]] = (
        _warehouse_partitions(
            connection=connection,
            database=database,
            relation_names=source_relations,
        )
    )
    source_relation_set: frozenset[str] = frozenset(source_relations)
    extents: dict[str, dict[str, object]] = _extents(
        connection=connection,
        database=database,
        relation_names=tuple(
            relation_name
            for relation_name in lineage_relations
            if relation_name not in source_relation_set
        ),
    )
    extents.update(_source_extents(warehouse_partitions=warehouse_partitions))
    baselines: dict[str, AdapterDirectFingerprintRecord] | None = _fingerprint_baselines(
        analysis=analysis,
        connection=connection,
        database=database,
    )
    model_relation_names: tuple[str, ...] = tuple(
        analysis.realized_project.relation_name_by_logical_key[model.key]
        for model in analysis.compiled_project.models
        if isinstance(model, CompiledTableModel)
    )
    activity_by_relation: dict[str, dict[str, object]] = read_model_activity(
        connection=connection,
        database=database,
        relation_names=model_relation_names,
        captured_at=captured_at,
        active_bindings=active_bindings,
    )
    return {
        "capturedAt": captured_at,
        "warehouseHealth": _warehouse_health_payload(
            health=warehouse_health,
            captured_at=captured_at,
            adapter_name=connection.adapter_identity.name,
            database=database,
        ),
        "models": _model_states(
            analysis=analysis,
            catalog=catalog,
            baselines=baselines,
            stats=stats,
            extents=extents,
            captured_at=captured_at,
            activity_by_relation=activity_by_relation,
        ),
        "sources": _source_states(
            analysis=analysis,
            connection=connection,
            database=database,
            stats=stats,
            extents=extents,
            captured_at=captured_at,
            kafka_lag_reader=kafka_lag_reader,
            warehouse_partitions=warehouse_partitions,
        ),
    }


def _warehouse_health_payload(
    *, health: AdapterWarehouseHealth, captured_at: str, adapter_name: str, database: str
) -> dict[str, object]:
    memory: dict[str, object] | None = (
        None
        if health.memory is None
        else {
            "residentBytes": health.memory.resident_bytes,
            "hostTotalBytes": health.memory.host_total_bytes,
            "cgroupUsedBytes": health.memory.cgroup_used_bytes,
            "cgroupLimitBytes": health.memory.cgroup_limit_bytes,
            "basis": health.memory.basis,
            "pressureFraction": health.memory.pressure_fraction,
        }
    )
    activity: dict[str, int | None] | None = (
        None
        if health.activity is None
        else {
            "activeQueries": health.activity.active_queries,
            "activeMerges": health.activity.active_merges,
            "incompleteMutations": health.activity.incomplete_mutations,
        }
    )
    kafka_consumers: dict[str, object] | None = (
        None
        if health.kafka_consumers is None
        else {
            "configuredTables": health.kafka_consumers.configured_tables,
            "materializedTables": health.kafka_consumers.materialized_tables,
            "materializedTableNames": list(health.kafka_consumers.materialized_table_names),
            "pollingTables": health.kafka_consumers.polling_tables,
            "exceptionTables": health.kafka_consumers.exception_tables,
            "stuckConsumers": [
                {
                    "table": consumer.table,
                    "stuckSeconds": consumer.stuck_seconds,
                    "recentErrors": consumer.recent_errors,
                    "latestError": consumer.latest_error,
                }
                for consumer in health.kafka_consumers.stuck_consumers
            ],
        }
    )
    return {
        "availability": str(health.availability),
        "status": str(health.status),
        "adapter": adapter_name,
        "database": database,
        "version": health.version,
        "uptimeSeconds": health.uptime_seconds,
        "measuredAt": health.measured_at or captured_at,
        "collectionDurationMs": health.collection_duration_ms,
        "stale": health.stale,
        "warnings": list(health.warnings),
        "capacityWarningFraction": health.capacity_warning_fraction,
        "capacityCriticalFraction": health.capacity_critical_fraction,
        "disks": [
            {
                "name": disk.name,
                "path": disk.path,
                "type": disk.disk_type,
                "totalBytes": disk.total_bytes,
                "freeBytes": disk.free_bytes,
                "unreservedBytes": disk.unreserved_bytes,
                "keepFreeBytes": disk.keep_free_bytes,
                "status": str(disk.status),
            }
            for disk in health.disks
        ],
        "inodes": {
            "total": health.inode_total,
            "free": health.inode_free,
            "status": str(health.inode_status),
        },
        "memory": memory,
        "activity": activity,
        "kafkaConsumers": kafka_consumers,
        "tables": (
            None
            if health.tables is None
            else [
                {
                    "name": table.name,
                    "rows": table.rows,
                    "bytesOnDisk": table.bytes_on_disk,
                    "activeParts": table.active_parts,
                }
                for table in health.tables
            ]
        ),
    }


def _managed_kafka_relation_names(analysis: CompileAnalysis) -> tuple[str, ...]:
    names: set[str] = set()
    for source in analysis.compiled_project.sources:
        if not isinstance(source.source, KafkaLandingStep):
            continue
        for resource in analysis.realized_project.resources_by_logical_key.get(source.key, ()):
            if isinstance(resource, AdapterManagedSource):
                names.add(resource.name)
    return tuple(sorted(names))


def _relation_stats(
    *,
    connection: AdapterConnection,
    database: str,
    active_bindings: tuple[tuple[str, str], ...],
) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for row in connection.query(build_relation_stats_query(database=database)).named_rows():
        stats[str(row["name"])] = {
            "rows": int(str(row["total_rows"])),
            "bytes": int(str(row["total_bytes"])),
            "parts": 0,
        }
    for row in connection.query(build_parts_query(database=database)).named_rows():
        entry: dict[str, int] | None = stats.get(str(row["table"]))
        if entry is not None:
            entry["parts"] = int(str(row["parts"]))
    return apply_bound_relation_stats(
        stats=stats,
        bindings=active_bindings,
    )


def _active_bindings(
    *, connection: AdapterConnection, database: str
) -> tuple[tuple[str, str], ...]:
    """Logical-to-physical pairs, empty for adapters without stable bindings."""

    if not connection.capabilities.stable_logical_bindings:
        return ()
    inspected: InspectedManagedTableState = connection.inspect_managed_table_state(database)
    return tuple(
        (binding.logical_name, binding.physical_name) for binding in inspected.active_bindings
    )


def apply_bound_relation_stats(
    *, stats: dict[str, dict[str, int]], bindings: tuple[tuple[str, str], ...]
) -> dict[str, dict[str, int]]:
    """Measure a stable logical view by the deployment relation it is bound to."""

    logical_name: str
    physical_name: str
    for logical_name, physical_name in bindings:
        measured: dict[str, int] | None = stats.get(physical_name)
        if measured is not None:
            stats[logical_name] = dict(measured)
    return stats


def _lineage_relation_names(catalog: CatalogSnapshot) -> tuple[str, ...]:
    names: list[str] = []
    for relation in catalog.relations:
        column_names: frozenset[str] = frozenset(column.name for column in relation.columns)
        if _LANDED_AT_COLUMN in column_names:
            names.append(relation.name)
    return tuple(names)


def _extents(
    *,
    connection: AdapterConnection,
    database: str,
    relation_names: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    if not relation_names:
        return {}
    extents: dict[str, dict[str, object]] = {}
    query: str = build_extents_query(database=database, relation_names=relation_names)
    for row in connection.query(query).named_rows():
        extents[str(row["relation"])] = {
            "oldest": str(row["oldest"]),
            "newest": str(row["newest"]),
            "rows": int(str(row["rows"])),
        }
    return extents


def _source_extents(
    *,
    warehouse_partitions: dict[str, dict[int, _WarehousePartitionObservation]],
) -> dict[str, dict[str, object]]:
    extents: dict[str, dict[str, object]] = {}
    for relation_name, observations_by_partition in warehouse_partitions.items():
        observations: tuple[_WarehousePartitionObservation, ...] = tuple(
            observations_by_partition.values()
        )
        if observations:
            extents[relation_name] = {
                "oldest": min(observation.oldest for observation in observations),
                "newest": max(observation.newest for observation in observations),
                "rows": sum(observation.rows for observation in observations),
            }
    return extents


def _model_states(
    *,
    analysis: CompileAnalysis,
    catalog: CatalogSnapshot,
    baselines: dict[str, AdapterDirectFingerprintRecord] | None,
    stats: dict[str, dict[str, int]],
    extents: dict[str, dict[str, object]],
    captured_at: str,
    activity_by_relation: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    policy_by_model: dict[str, SourceFreshnessPolicy | None] = _policies_by_model(analysis)
    direct_model_names: set[str] = set()
    for pipeline in analysis.compiled_project.pipelines:
        if PipelineMode(pipeline.pipeline.mode) == PipelineMode.DIRECT:
            direct_model_names.update(model.key.name for model in pipeline.models)
    states: dict[str, dict[str, object]] = {}
    for model in analysis.compiled_project.models:
        is_table: bool = isinstance(model, CompiledTableModel)
        relation_name: str = analysis.realized_project.relation_name_by_logical_key[model.key]
        relation_stats: dict[str, int] = stats.get(relation_name, {}) if is_table else {}
        extent: dict[str, object] = extents.get(relation_name, {}) if is_table else {}
        newest: str | None = _optional_str(extent.get("newest"))
        drift_reasons: tuple[str, ...] = (
            DirectModelFingerprint.drift_reasons(
                model=model,
                baseline=baselines.get(model.key.name),
            )
            if baselines is not None and model.key.name in direct_model_names
            else ()
        )
        states[model.key.name] = {
            "relationName": relation_name,
            "rows": relation_stats.get("rows"),
            "diskBytes": relation_stats.get("bytes"),
            "parts": relation_stats.get("parts"),
            "oldestRowAt": _optional_str(extent.get("oldest")),
            "newestRowAt": newest,
            "lagSeconds": _age_seconds(newest=newest, captured_at=captured_at),
            "freshness": _freshness(
                newest=newest,
                captured_at=captured_at,
                policy=policy_by_model.get(model.key.name) if is_table else None,
            ),
            "activity": activity_by_relation.get(relation_name),
            "drift": bool(drift_reasons),
            "driftReasons": list(drift_reasons),
            "semanticDrift": model_drift_payload(
                analysis=analysis,
                model=model,
                catalog=catalog,
            ),
        }
    return states


def _source_states(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    stats: dict[str, dict[str, int]],
    extents: dict[str, dict[str, object]],
    captured_at: str,
    kafka_lag_reader: KafkaLagReader | None,
    warehouse_partitions: dict[str, dict[int, _WarehousePartitionObservation]],
) -> dict[str, dict[str, object]]:
    inputs: list[_SourceStateInput] = []
    for source in analysis.compiled_project.sources:
        relation_name: str = analysis.realized_project.relation_name_by_logical_key[source.key]
        extent: dict[str, object] = extents.get(relation_name, {})
        newest: str | None = _optional_str(extent.get("newest"))
        age: float | None = _age_seconds(newest=newest, captured_at=captured_at)
        kafka_lag: KafkaLagSnapshot | None = _kafka_lag(
            analysis=analysis,
            source=source,
            database=database,
            reader=kafka_lag_reader,
        )
        inputs.append(
            _SourceStateInput(
                source=source,
                relation_name=relation_name,
                extent=extent,
                newest=newest,
                age=age,
                kafka_lag=kafka_lag,
            )
        )
    newest_ages: dict[str, float | None] = {
        item.relation_name: item.age for item in inputs if item.relation_name in extents
    }
    throughputs: dict[str, dict[str, object]] = _throughputs(
        connection=connection,
        database=database,
        newest_ages=newest_ages,
    )
    states: dict[str, dict[str, object]] = {}
    for item in inputs:
        throughput: dict[str, object] | None = throughputs.get(item.relation_name)
        states[item.source.key.name] = {
            "relationName": item.relation_name,
            "rows": stats.get(item.relation_name, {}).get("rows"),
            "oldestEventAt": _optional_str(item.extent.get("oldest")),
            "newestEventAt": item.newest,
            "lastArrivalSeconds": item.age,
            "kafkaLagMessages": (None if item.kafka_lag is None else item.kafka_lag.total_messages),
            "freshness": _freshness(
                newest=item.newest,
                captured_at=captured_at,
                policy=item.source.source.freshness,
            ),
            "throughput": throughput,
            "rowsPerSecond": _rows_per_second(throughput),
            "partitions": _partitions(
                warehouse_partitions=warehouse_partitions.get(item.relation_name),
                kafka_lag=item.kafka_lag,
            ),
        }
    return states


def _policies_by_model(
    analysis: CompileAnalysis,
) -> dict[str, SourceFreshnessPolicy | None]:
    policies: dict[str, SourceFreshnessPolicy | None] = {}
    for pipeline in analysis.compiled_project.pipelines:
        source_policy: SourceFreshnessPolicy | None = (
            None if pipeline.source is None else pipeline.source.source.freshness
        )
        for model in pipeline.models:
            policies[model.key.name] = source_policy
    return policies


def _throughputs(
    *,
    connection: AdapterConnection,
    database: str,
    newest_ages: dict[str, float | None],
) -> dict[str, dict[str, object]]:
    if not newest_ages:
        return {}
    windows: tuple[tuple[str, int, int], ...] = tuple(
        (relation_name, *choose_throughput_window(newest_age_seconds=age))
        for relation_name, age in newest_ages.items()
    )
    query: str = build_throughputs_query(
        database=database,
        windows=windows,
    )
    counts_by_relation: dict[str, dict[int, int]] = {}
    for row in connection.query(query).named_rows():
        counts_by_relation.setdefault(str(row["relation"]), {})[int(str(row["bucket"]))] = int(
            str(row["rows"])
        )
    return {
        relation_name: {
            "windowSeconds": window_seconds,
            "bucketSeconds": bucket_seconds,
            "buckets": _zero_filled_buckets(
                counts_by_bucket=counts_by_relation.get(relation_name, {}),
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
            ),
        }
        for relation_name, window_seconds, bucket_seconds in windows
    }


def _zero_filled_buckets(
    *,
    counts_by_bucket: dict[int, int],
    window_seconds: int,
    bucket_seconds: int,
) -> list[int]:
    if not counts_by_bucket:
        return [0] * (window_seconds // bucket_seconds)
    newest_bucket: int = max(counts_by_bucket)
    bucket_count: int = window_seconds // bucket_seconds
    start: int = newest_bucket - (bucket_count - 1) * bucket_seconds
    return [
        counts_by_bucket.get(start + index * bucket_seconds, 0) for index in range(bucket_count)
    ]


def _rows_per_second(throughput: dict[str, object] | None) -> float | None:
    if throughput is None:
        return None
    buckets: object = throughput.get("buckets")
    window: object = throughput.get("windowSeconds")
    if not isinstance(buckets, list) or not isinstance(window, int) or window == 0:
        return None
    return round(float(sum(buckets)) / float(window), 3)


def _partitions(
    *,
    warehouse_partitions: dict[int, _WarehousePartitionObservation] | None,
    kafka_lag: KafkaLagSnapshot | None,
) -> list[dict[str, object]] | None:
    if warehouse_partitions is None and kafka_lag is None:
        return None
    lag_by_partition: dict[int, KafkaPartitionLag] = {
        item.partition: item for item in (() if kafka_lag is None else kafka_lag.partitions)
    }
    partitions_by_id: dict[int, dict[str, object]] = {}
    for observation in (warehouse_partitions or {}).values():
        broker_lag: KafkaPartitionLag | None = lag_by_partition.get(observation.partition)
        partitions_by_id[observation.partition] = {
            "partition": observation.partition,
            "maxOffset": observation.max_offset,
            "newestEventAt": observation.newest,
            "committedOffset": None if broker_lag is None else broker_lag.committed_offset,
            "endOffset": None if broker_lag is None else broker_lag.end_offset,
            "kafkaLagMessages": None if broker_lag is None else broker_lag.lag_messages,
        }
    for partition, broker_lag in lag_by_partition.items():
        partitions_by_id.setdefault(
            partition,
            {
                "partition": partition,
                "maxOffset": None,
                "newestEventAt": None,
                "committedOffset": broker_lag.committed_offset,
                "endOffset": broker_lag.end_offset,
                "kafkaLagMessages": broker_lag.lag_messages,
            },
        )
    return [partitions_by_id[partition] for partition in sorted(partitions_by_id)]


def _warehouse_partitions(
    *,
    connection: AdapterConnection,
    database: str,
    relation_names: tuple[str, ...],
) -> dict[str, dict[int, _WarehousePartitionObservation]]:
    if not relation_names:
        return {}
    observations: dict[str, dict[int, _WarehousePartitionObservation]] = {
        relation_name: {} for relation_name in relation_names
    }
    query: str = build_partitions_query(database=database, relation_names=relation_names)
    for row in connection.query(query).named_rows():
        relation_name: str = str(row["relation"])
        partition: int = int(str(row["partition"]))
        observations[relation_name][partition] = _WarehousePartitionObservation(
            partition=partition,
            max_offset=int(str(row["max_offset"])),
            oldest=str(row["oldest"]),
            newest=str(row["newest"]),
            rows=int(str(row["rows"])),
        )
    return observations


def _kafka_lag(
    *,
    analysis: CompileAnalysis,
    source: CompiledSource,
    database: str,
    reader: KafkaLagReader | None,
) -> KafkaLagSnapshot | None:
    if not isinstance(source.source, KafkaLandingStep) or reader is None:
        return None
    managed_source: AdapterManagedSource | None = next(
        (
            resource
            for resource in analysis.realized_project.resources_by_logical_key.get(source.key, ())
            if isinstance(resource, AdapterManagedSource)
        ),
        None,
    )
    if managed_source is None:
        return None
    kafka: KafkaSettings = replace(
        source.source.kafka,
        broker_list=managed_source.broker_list,
        topic=managed_source.topic,
        consumer_group=managed_source.consumer_group,
        format=managed_source.format,
        settings=dict(managed_source.settings) or None,
    )
    return reader.read(kafka=kafka, database=database)


def _fingerprint_baselines(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
) -> dict[str, AdapterDirectFingerprintRecord] | None:
    identities: tuple[str, ...] = tuple(
        f"{database}.{model.key.name}" for model in analysis.compiled_project.models
    )
    snapshot: AdapterDirectFingerprintSnapshot = connection.load_direct_fingerprints(
        database=database,
        logical_model_identities=identities,
    )
    if snapshot.status != AdapterOptionalStateStatus.AVAILABLE:
        return None if snapshot.status == AdapterOptionalStateStatus.UNAVAILABLE else {}
    prefix: str = f"{database}."
    return {
        record.logical_model_identity.removeprefix(prefix): record for record in snapshot.baselines
    }


def _freshness(
    *,
    newest: str | None,
    captured_at: str,
    policy: SourceFreshnessPolicy | None,
) -> str | None:
    if policy is None:
        return None
    age: float | None = _age_seconds(newest=newest, captured_at=captured_at)
    if age is None:
        return str(Freshness.STALLED)
    error_after: float | None = _policy_seconds(policy.error_after)
    if error_after is not None and age > error_after:
        return str(Freshness.STALLED)
    warn_after: float | None = _policy_seconds(policy.warn_after)
    if warn_after is not None and age > warn_after:
        return str(Freshness.LAGGING)
    return str(Freshness.FRESH)


def _policy_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    return float(value[:-1]) * SECONDS_BY_DURATION_UNIT[value[-1]]


def _age_seconds(*, newest: str | None, captured_at: str) -> float | None:
    if newest is None:
        return None
    newest_at: datetime.datetime | None = _parse_warehouse_timestamp(newest)
    now_at: datetime.datetime | None = _parse_warehouse_timestamp(captured_at)
    if newest_at is None or now_at is None:
        return None
    return round((now_at - newest_at).total_seconds(), 3)


def _parse_warehouse_timestamp(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def build_relation_stats_query(*, database: str) -> str:
    """Approximate rows and bytes for every relation in one scan of system.tables."""

    return (
        "SELECT name, coalesce(total_rows, 0) AS total_rows, "
        "coalesce(total_bytes, 0) AS total_bytes "
        f"FROM system.tables WHERE database = '{database}'"
    )


def build_parts_query(*, database: str) -> str:
    """Active part counts per relation in one scan of system.parts."""

    return (
        "SELECT table, count() AS parts "
        f"FROM system.parts WHERE database = '{database}' AND active GROUP BY table"
    )


def build_extents_query(*, database: str, relation_names: tuple[str, ...]) -> str:
    """Oldest and newest landed event per lineage-bearing relation, batched."""

    selects: list[str] = [
        (
            f"SELECT '{name}' AS relation, "
            "toString(min(_replay_landed_at)) AS oldest, "
            "toString(max(_replay_landed_at)) AS newest, "
            "count() AS rows "
            f"FROM `{database}`.`{name}` HAVING count() > 0"
        )
        for name in relation_names
    ]
    return " UNION ALL ".join(selects)


def build_throughputs_query(
    *,
    database: str,
    windows: tuple[tuple[str, int, int], ...],
) -> str:
    """Landed-event counts for every source in one warehouse round trip."""

    selects: list[str] = [
        (
            f"SELECT '{relation_name}' AS relation, "
            "toUnixTimestamp(toStartOfInterval("
            f"_replay_landed_at, INTERVAL {bucket_seconds} SECOND)) AS bucket, "
            "count() AS rows "
            f"FROM `{database}`.`{relation_name}` "
            f"WHERE _replay_landed_at >= now64(3) - INTERVAL {window_seconds} SECOND "
            "GROUP BY bucket"
        )
        for relation_name, window_seconds, bucket_seconds in windows
    ]
    return " UNION ALL ".join(selects) + " ORDER BY relation, bucket"


def build_partitions_query(*, database: str, relation_names: tuple[str, ...]) -> str:
    """Per-partition landed offsets for every source in one warehouse round trip."""

    selects: list[str] = [
        (
            f"SELECT '{relation_name}' AS relation, _replay_partition AS partition, "
            "max(_replay_offset) AS max_offset, "
            "toString(min(_replay_landed_at)) AS oldest, "
            "toString(max(_replay_landed_at)) AS newest, count() AS rows "
            f"FROM `{database}`.`{relation_name}` GROUP BY partition"
        )
        for relation_name in relation_names
    ]
    return " UNION ALL ".join(selects) + " ORDER BY relation, partition"


def choose_throughput_window(*, newest_age_seconds: float | None) -> tuple[int, int]:
    """Pick the smallest ladder rung whose window still contains the newest event."""

    if newest_age_seconds is None:
        return THROUGHPUT_WINDOW_LADDER[-1]
    for window_seconds, bucket_seconds in THROUGHPUT_WINDOW_LADDER:
        if newest_age_seconds < window_seconds:
            return (window_seconds, bucket_seconds)
    return THROUGHPUT_WINDOW_LADDER[-1]


def build_topics_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection | None,
    database: str | None,
    topic_reader: KafkaTopicReader,
    kafka_lag_reader: KafkaLagReader,
) -> dict[str, object]:
    """Merge broker topic inventories with managed-source lag and retained stats."""

    managed: tuple[_ManagedTopicSource, ...] = _managed_topic_sources(analysis)
    if not managed:
        return {
            "available": False,
            "reason": _NO_MANAGED_SOURCES_REASON,
            "pendingBrokers": [],
            "topics": [],
        }
    snapshots: dict[str, KafkaTopicsSnapshot | None] = {}
    for entry in managed:
        if entry.kafka.broker_list not in snapshots:
            snapshots[entry.kafka.broker_list] = topic_reader.read(kafka=entry.kafka)
    stats: dict[str, dict[str, int]] = _retained_stats(connection=connection, database=database)
    topics: dict[str, dict[str, object]] = {}
    for broker_list, snapshot in snapshots.items():
        for topic in () if snapshot is None else snapshot.topics:
            topics[topic.name] = _broker_topic_item(topic=topic, broker_list=broker_list)
    for entry in managed:
        item: dict[str, object] = topics.setdefault(
            entry.kafka.topic,
            {
                "name": entry.kafka.topic,
                "brokerList": entry.kafka.broker_list,
                "partitions": None,
                "replicationFactor": None,
                "internal": False,
                "sources": [],
                "lagMessages": None,
                "retainedRows": None,
                "retainedBytes": None,
            },
        )
        sources: list[dict[str, object]] = cast("list[dict[str, object]]", item["sources"])
        sources.append({"name": entry.source_name, "relationName": entry.relation_name})
        lag: KafkaLagSnapshot | None = kafka_lag_reader.read(
            kafka=entry.kafka, database=database or ""
        )
        item["lagMessages"] = None if lag is None else lag.total_messages
        relation_stats: dict[str, int] | None = stats.get(entry.relation_name)
        item["retainedRows"] = None if relation_stats is None else relation_stats["rows"]
        item["retainedBytes"] = None if relation_stats is None else relation_stats["bytes"]
    return {
        "available": True,
        "reason": None,
        "pendingBrokers": sorted(
            broker_list for broker_list, snapshot in snapshots.items() if snapshot is None
        ),
        "topics": [topics[name] for name in sorted(topics)],
    }


def _broker_topic_item(*, topic: KafkaTopicInfo, broker_list: str) -> dict[str, object]:
    return {
        "name": topic.name,
        "brokerList": broker_list,
        "partitions": topic.partition_count,
        "replicationFactor": topic.replication_factor,
        "internal": topic.internal,
        "sources": [],
        "lagMessages": None,
        "retainedRows": None,
        "retainedBytes": None,
    }


def _managed_topic_sources(analysis: CompileAnalysis) -> tuple[_ManagedTopicSource, ...]:
    entries: list[_ManagedTopicSource] = []
    for source in analysis.compiled_project.sources:
        if not isinstance(source.source, KafkaLandingStep):
            continue
        managed_source: AdapterManagedSource | None = next(
            (
                resource
                for resource in analysis.realized_project.resources_by_logical_key.get(
                    source.key, ()
                )
                if isinstance(resource, AdapterManagedSource)
            ),
            None,
        )
        if managed_source is None:
            continue
        entries.append(
            _ManagedTopicSource(
                source_name=source.key.name,
                relation_name=analysis.realized_project.relation_name_by_logical_key[source.key],
                kafka=replace(
                    source.source.kafka,
                    broker_list=managed_source.broker_list,
                    topic=managed_source.topic,
                    consumer_group=managed_source.consumer_group,
                    format=managed_source.format,
                    settings=dict(managed_source.settings) or None,
                ),
            )
        )
    return tuple(entries)


def _retained_stats(
    *, connection: AdapterConnection | None, database: str | None
) -> dict[str, dict[str, int]]:
    if connection is None or database is None:
        return {}
    stats: dict[str, dict[str, int]] = {}
    for row in connection.query(build_relation_stats_query(database=database)).named_rows():
        stats[str(row["name"])] = {
            "rows": int(str(row["total_rows"])),
            "bytes": int(str(row["total_bytes"])),
        }
    return stats
