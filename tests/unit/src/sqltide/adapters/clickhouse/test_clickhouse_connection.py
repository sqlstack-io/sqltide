import time
from typing import cast
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from sqltide._native import load_clickhouse_warehouse_health
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import (
    AdapterAuthenticationError,
    AdapterRelationNotFoundError,
    AdapterResultError,
    AdapterWarehouseError,
)
from sqltide.adapter.models import (
    AdapterQueryCancellation,
    AdapterQueryResult,
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterReplayOffsetRange,
    AdapterStatementProgress,
    AdapterWarehouseDisk,
    AdapterWarehouseHealth,
    AdapterWarehouseStuckKafkaConsumer,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapters.clickhouse.classes.clickhouse_connection import ClickHouseConnection
from sqltide.adapters.clickhouse.constants import (
    CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS,
)
from sqltide.adapters.clickhouse.types import RawClickHouseClient
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    CatalogFailureTestCase,
    CatalogInspectionTestCase,
    CatalogRetryTestCase,
    ClickHouseDropLimitTestCase,
    ClickHouseOptionalHealthFailureTestCase,
    ClickHousePublishCapabilitiesTestCase,
    ClickHouseQueryCancellationTestCase,
    ClickHouseReplayFrontierTestCase,
    ClickHouseStatementProgressTestCase,
    ClickHouseWarehouseHealthTestCase,
    ClickHouseWorkflowCorrelationTestCase,
    ConnectionQueryNormalizationTestCase,
    ConnectionTranslationTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    FailingRawClickHouseClient,
    FakeRawClickHouseQueryResult,
    SequencedRawClickHouseClient,
    StubRawClickHouseClient,
    build_catalog_column_result,
    build_catalog_relation_result,
    build_catalog_timezone_result,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseDropLimitTestCase(
            description="effective ClickHouse drop limit is exposed in bytes",
            setting_value=80_000_000_000,
            server_default_value=50_000_000_000,
            expected_limit=80_000_000_000,
            expected_server_default=50_000_000_000,
        ),
        ClickHouseDropLimitTestCase(
            description="zero ClickHouse drop limit is exposed as unlimited",
            setting_value=0,
            server_default_value=0,
            expected_limit=None,
            expected_server_default=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_drop_limit_when_reading_then_returns_effective_bytes(
    test_case: ClickHouseDropLimitTestCase,
) -> None:
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            FakeRawClickHouseQueryResult(
                column_names=["value"],
                result_rows=[[test_case.setting_value]],
            ),
            FakeRawClickHouseQueryResult(
                column_names=["default"],
                result_rows=[[test_case.server_default_value]],
            ),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    assert connection.load_relation_drop_size_limit() == test_case.expected_limit
    assert connection.load_relation_drop_size_server_default() == test_case.expected_server_default
    assert raw_client.statements == [
        "SELECT value FROM system.settings WHERE name = 'max_table_size_to_drop'",
        "SELECT default FROM system.settings WHERE name = 'max_table_size_to_drop'",
    ]


@pytest.mark.parametrize(
    "test_case",
    [
        ConnectionTranslationTestCase(
            description="translates a workflow mutation relation failure",
            driver_error=DatabaseError(
                "Code: 60. DB::Exception: Table analytics.tbl__orders does not exist. "
                "(UNKNOWN_TABLE)"
            ),
            expected_error_type=AdapterRelationNotFoundError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_failing_driver_when_executing_workflow_sql_then_it_raises_neutral_equivalent(
    test_case: ConnectionTranslationTestCase,
) -> None:
    connection: ClickHouseConnection = ClickHouseConnection(
        cast(RawClickHouseClient, FailingRawClickHouseClient(test_case.driver_error))
    )

    with pytest.raises(AdapterWarehouseError) as error_info:
        connection.execute_workflow_sql("DROP TABLE analytics.tbl__orders")

    assert type(error_info.value) is test_case.expected_error_type


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHousePublishCapabilitiesTestCase(
            description="reports per-relation but not graph-atomic ClickHouse publish",
            expected_stable_logical_bindings=True,
            expected_per_relation_atomic_replace=True,
            expected_graph_atomic_publish=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_connection_when_reading_publish_capabilities_then_guarantees_are_exact(
    test_case: ClickHousePublishCapabilitiesTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(column_names=[], result_rows=[])
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    assert (
        connection.capabilities.stable_logical_bindings
        is test_case.expected_stable_logical_bindings
    )
    assert (
        connection.capabilities.per_relation_atomic_replace
        is test_case.expected_per_relation_atomic_replace
    )
    assert connection.capabilities.graph_atomic_publish is test_case.expected_graph_atomic_publish


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseWarehouseHealthTestCase(
            description="decodes bounded health and inclusive capacity thresholds",
            disk_rows=(
                ("critical", "/critical/", "Local", 0, 100, 6, 5, 0),
                ("warning", "/warning/", "Local", 0, 100, 16, 15, 0),
                ("healthy", "/healthy/", "Local", 0, 100, 20, 16, 0),
            ),
            metric_rows=(
                ("FilesystemMainPathTotalINodes", 1000),
                ("FilesystemMainPathAvailableINodes", 400),
                ("MemoryResident", 300),
                ("OSMemoryTotal", 2000),
                ("CGroupMemoryUsed", 500),
                ("CGroupMemoryTotal", 1000),
            ),
            expected_disk_statuses=("critical", "warning", "healthy"),
            expected_total_bytes=(100, 100, 100),
            expected_availability="available",
            expected_status="critical",
            expected_memory_basis="cgroup",
            expected_table_name="tbl__orders",
            expected_query_count=6,
            managed_source_names=("kafka__orders", "kafka__payments"),
            consumer_row=(1, ["kafka__orders"], 1, 1, [("kafka__orders", 7200, 10, "cast failed")]),
            expected_configured_tables=2,
            expected_materialized_tables=1,
            expected_materialized_table_names=("kafka__orders",),
            expected_polling_tables=1,
            expected_exception_tables=1,
            expected_stuck_consumers=(
                AdapterWarehouseStuckKafkaConsumer(
                    table="kafka__orders",
                    stuck_seconds=7200,
                    recent_errors=10,
                    latest_error="cast failed",
                ),
            ),
            expected_consumer_query_fragment="table IN ('kafka__orders', 'kafka__payments')",
        ),
        ClickHouseWarehouseHealthTestCase(
            description="does not infer overall health from inodes when disk capacity is unknown",
            disk_rows=(
                (
                    "remote",
                    "s3://warehouse/",
                    "ObjectStorage",
                    0,
                    2**64 - 1,
                    2**64 - 1,
                    2**64 - 1,
                    0,
                ),
            ),
            metric_rows=(
                ("FilesystemMainPathTotalINodes", 1000),
                ("FilesystemMainPathAvailableINodes", 50),
                ("MemoryResident", 300),
                ("OSMemoryTotal", 2000),
                ("CGroupMemoryUsed", 500),
                ("CGroupMemoryTotal", 1000),
            ),
            expected_disk_statuses=("unknown",),
            expected_total_bytes=(None,),
            expected_availability="partial",
            expected_status="unknown",
            expected_memory_basis="cgroup",
            expected_table_name="tbl__orders",
            expected_query_count=6,
            managed_source_names=("kafka__orders",),
            consumer_row=(1, ["kafka__orders"], 1, 0, []),
            expected_configured_tables=1,
            expected_materialized_tables=1,
            expected_materialized_table_names=("kafka__orders",),
            expected_polling_tables=1,
            expected_exception_tables=0,
            expected_consumer_query_fragment="table IN ('kafka__orders')",
        ),
        ClickHouseWarehouseHealthTestCase(
            description="broken disk dominates a healthy configured disk",
            disk_rows=(
                ("broken", "/broken/", "Local", 1, 0, 0, 0, 0),
                ("healthy", "/healthy/", "Local", 0, 100, 50, 50, 0),
            ),
            metric_rows=(
                ("FilesystemMainPathTotalINodes", 1000),
                ("FilesystemMainPathAvailableINodes", 900),
                ("MemoryResident", 300),
                ("OSMemoryTotal", 2000),
                ("CGroupMemoryUsed", 500),
                ("CGroupMemoryTotal", 1000),
            ),
            expected_disk_statuses=("critical", "healthy"),
            expected_total_bytes=(0, 100),
            expected_availability="available",
            expected_status="critical",
            expected_memory_basis="cgroup",
            expected_table_name="tbl__orders",
            expected_query_count=6,
            managed_source_names=("kafka__orders",),
            consumer_row=(1, ["kafka__orders"], 1, 0, []),
            expected_configured_tables=1,
            expected_materialized_tables=1,
            expected_materialized_table_names=("kafka__orders",),
            expected_polling_tables=1,
            expected_exception_tables=0,
            expected_consumer_query_fragment="table IN ('kafka__orders')",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_system_metrics_when_reading_health_then_snapshot_is_truthful_and_bounded(
    test_case: ClickHouseWarehouseHealthTestCase,
) -> None:
    query_results: list[FakeRawClickHouseQueryResult] = [
        FakeRawClickHouseQueryResult(
            column_names=[
                "name",
                "path",
                "type",
                "is_broken",
                "total_space",
                "free_space",
                "unreserved_space",
                "keep_free_space",
            ],
            result_rows=[list(row) for row in test_case.disk_rows],
        ),
        FakeRawClickHouseQueryResult(
            column_names=["version", "uptime_seconds"],
            result_rows=[["25.8.1.1", 86400]],
        ),
        FakeRawClickHouseQueryResult(
            column_names=["metric", "value"],
            result_rows=[list(row) for row in test_case.metric_rows],
        ),
        FakeRawClickHouseQueryResult(
            column_names=["active_queries", "active_merges", "incomplete_mutations"],
            result_rows=[[2, 1, 3]],
        ),
        FakeRawClickHouseQueryResult(
            column_names=[
                "materialized_tables",
                "materialized_table_names",
                "polling_tables",
                "exception_tables",
                "stuck_consumers",
            ],
            result_rows=[list(test_case.consumer_row)],
        ),
    ]
    query_results.append(
        FakeRawClickHouseQueryResult(
            column_names=["table", "rows", "bytes_on_disk", "active_parts"],
            result_rows=[["tbl__orders", 500000, 64000000, 12]],
        )
    )
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(tuple(query_results))
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    health: AdapterWarehouseHealth = connection.load_warehouse_health(
        database="analytics\\o'",
        managed_source_names=test_case.managed_source_names,
    )

    assert tuple(str(disk.status) for disk in health.disks) == test_case.expected_disk_statuses
    assert tuple(disk.total_bytes for disk in health.disks) == test_case.expected_total_bytes
    assert str(health.availability) == test_case.expected_availability
    assert str(health.status) == test_case.expected_status
    assert health.memory is not None
    assert health.memory.basis == test_case.expected_memory_basis
    assert health.memory.pressure_fraction == 0.5
    assert health.activity is not None
    assert health.activity.active_queries == 2
    assert health.tables is not None
    assert health.tables[0].name == test_case.expected_table_name
    assert health.kafka_consumers is not None
    assert health.kafka_consumers.configured_tables == test_case.expected_configured_tables
    assert health.kafka_consumers.materialized_tables == test_case.expected_materialized_tables
    assert (
        health.kafka_consumers.materialized_table_names
        == test_case.expected_materialized_table_names
    )
    assert health.kafka_consumers.polling_tables == test_case.expected_polling_tables
    assert health.kafka_consumers.exception_tables == test_case.expected_exception_tables
    assert health.kafka_consumers.stuck_consumers == test_case.expected_stuck_consumers
    assert health.capacity_warning_fraction == 0.15
    assert len(raw_client.statements) == test_case.expected_query_count
    assert "query_id != currentQueryID()" in raw_client.statements[3]
    assert "database =" not in raw_client.statements[3]
    table_query_index: int = test_case.expected_query_count - 1
    assert "database = 'analytics\\\\o'''" in raw_client.statements[table_query_index]
    assert "LIMIT 10" in raw_client.statements[table_query_index]
    consumer_query: str = raw_client.statements[table_query_index - 1]
    assert "system.kafka_consumers" in consumer_query
    assert "system.tables" in consumer_query
    assert "engine = 'Kafka'" in consumer_query
    assert test_case.expected_consumer_query_fragment in consumer_query


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseOptionalHealthFailureTestCase(
            description="failed project footprint remains unavailable rather than empty",
            expected_warning="Project table footprint is unavailable.",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_footprint_failure_when_reading_health_then_table_evidence_is_unavailable(
    test_case: ClickHouseOptionalHealthFailureTestCase,
) -> None:
    connection: MagicMock = MagicMock(spec=AdapterConnection)
    connection.query_many.side_effect = [
        (
            AdapterWarehouseDisk(
                name="default",
                path="/data/",
                disk_type="Local",
                total_bytes=100,
                free_bytes=50,
                unreserved_bytes=50,
                keep_free_bytes=0,
                status="healthy",
            ),
        ),
        (
            {"metric": "FilesystemMainPathTotalINodes", "value": 100},
            {"metric": "FilesystemMainPathAvailableINodes", "value": 50},
        ),
        AdapterWarehouseError("table probe failed"),
    ]
    connection.query_one.side_effect = [
        {"version": "25.8.1.1", "uptime_seconds": 100},
        {"active_queries": 0, "active_merges": 0, "incomplete_mutations": 0},
    ]

    health: AdapterWarehouseHealth = load_clickhouse_warehouse_health(
        connection=cast(AdapterConnection, connection), database="analytics"
    )

    assert str(health.availability) == "partial"
    assert health.tables is None
    assert test_case.expected_warning in health.warnings


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogInspectionTestCase(
            description="decodes a complete immutable catalog with three fixed queries",
            expected_timezone="America/New_York",
            expected_relation_names=frozenset({"tbl__orders", "tbl__orders__dep_a"}),
            expected_query_count=3,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_system_rows_when_loading_catalog_then_snapshot_is_complete(
    test_case: CatalogInspectionTestCase,
) -> None:
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            FakeRawClickHouseQueryResult(
                column_names=["timezone()"],
                result_rows=[["America/New_York"]],
            ),
            FakeRawClickHouseQueryResult(
                column_names=[
                    "name",
                    "engine",
                    "sorting_key",
                    "partition_key",
                    "create_table_query",
                    "as_select",
                    "uuid",
                ],
                result_rows=[
                    [
                        "tbl__orders",
                        "View",
                        "",
                        "",
                        "CREATE VIEW analytics.tbl__orders AS "
                        "SELECT * FROM analytics.tbl__orders__dep_a",
                        "SELECT * FROM analytics.tbl__orders__dep_a",
                        "00000000-0000-0000-0000-000000000001",
                    ],
                    [
                        "tbl__orders__dep_a",
                        "ReplacingMergeTree",
                        "order_id, updated_at",
                        "toYYYYMM(updated_at)",
                        "CREATE TABLE analytics.tbl__orders__dep_a "
                        "(order_id String, updated_at DateTime64(3)) "
                        "ENGINE = ReplacingMergeTree(updated_at) "
                        "PARTITION BY toYYYYMM(updated_at) "
                        "ORDER BY (order_id, updated_at) "
                        "TTL updated_at + INTERVAL 30 DAY "
                        "SETTINGS index_granularity = 8192",
                        "",
                        "00000000-0000-0000-0000-000000000002",
                    ],
                ],
            ),
            FakeRawClickHouseQueryResult(
                column_names=["table", "name", "type", "default_expression"],
                result_rows=[
                    ["tbl__orders__dep_a", "order_id", "String", ""],
                    ["tbl__orders__dep_a", "updated_at", "DateTime64(3)", "now64(3)"],
                ],
            ),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    catalog: CatalogSnapshot = connection.load_catalog("analytics")
    relation: CatalogRelation | None = catalog.relation("tbl__orders__dep_a")
    binding: CatalogRelation | None = catalog.relation("tbl__orders")

    assert catalog.identity.adapter.name == "clickhouse"
    assert catalog.identity.database == "analytics"
    assert catalog.warehouse_timezone == test_case.expected_timezone
    assert catalog.relation_names() == test_case.expected_relation_names
    assert relation is not None
    assert binding is not None
    assert relation.engine == "ReplacingMergeTree"
    assert relation.storage_engine == "ReplacingMergeTree(updated_at)"
    assert relation.order_by == ("order_id", "updated_at")
    assert relation.partition_by == "toYYYYMM(updated_at)"
    assert relation.ttl == "updated_at + INTERVAL '30' DAY"
    assert relation.settings == (("index_granularity", "8192"),)
    assert relation.columns[1].default_expression == "now64(3)"
    assert binding.stable_binding_name == "tbl__orders__dep_a"
    assert binding.source_relation_names == ("tbl__orders__dep_a",)
    assert binding.source_relation_name == "tbl__orders__dep_a"
    assert binding.query_sql == "SELECT * FROM analytics.tbl__orders__dep_a"
    assert len(raw_client.statements) == test_case.expected_query_count


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogRetryTestCase(
            description="transient incomplete catalog is reloaded as one fresh snapshot",
            expected_query_count=5,
            expected_delays=(CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS,),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transient_empty_ddl_when_loading_catalog_then_retries_complete_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    test_case: CatalogRetryTestCase,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            build_catalog_timezone_result(),
            build_catalog_relation_result(create_table_query=""),
            build_catalog_timezone_result(),
            build_catalog_relation_result(
                create_table_query=(
                    "CREATE TABLE analytics.tbl__orders (order_id UInt64) "
                    "ENGINE = MergeTree ORDER BY order_id"
                )
            ),
            build_catalog_column_result(),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    result: CatalogSnapshot = connection.load_catalog("analytics")

    assert result.relation_names() == frozenset({"tbl__orders"})
    assert len(raw_client.statements) == test_case.expected_query_count
    assert delays == list(test_case.expected_delays)


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogFailureTestCase(
            description="persistent incomplete catalog fails after bounded retries",
            expected_query_count=6,
            expected_delays=(
                CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS,
                CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS * 2,
            ),
            expected_error=(
                "catalog for database 'analytics' remained incomplete after 3 attempts; "
                "missing CREATE statements for: tbl__orders"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_persistent_empty_ddl_when_loading_catalog_then_fails_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
    test_case: CatalogFailureTestCase,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            build_catalog_timezone_result(),
            build_catalog_relation_result(create_table_query=""),
            build_catalog_timezone_result(),
            build_catalog_relation_result(create_table_query=""),
            build_catalog_timezone_result(),
            build_catalog_relation_result(create_table_query=""),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    with pytest.raises(AdapterResultError, match=test_case.expected_error):
        connection.load_catalog("analytics")

    assert len(raw_client.statements) == test_case.expected_query_count
    assert delays == list(test_case.expected_delays)


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogFailureTestCase(
            description="nonempty malformed catalog SQL remains a strict failure",
            expected_query_count=3,
            expected_delays=(),
            expected_error="catalog SQL could not be parsed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_nonempty_invalid_ddl_when_loading_catalog_then_fails_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    test_case: CatalogFailureTestCase,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            build_catalog_timezone_result(),
            build_catalog_relation_result(create_table_query=";"),
            build_catalog_column_result(),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    with pytest.raises(AdapterResultError, match=test_case.expected_error):
        connection.load_catalog("analytics")

    assert len(raw_client.statements) == test_case.expected_query_count
    assert delays == list(test_case.expected_delays)


@pytest.mark.parametrize(
    "test_case",
    [
        ConnectionQueryNormalizationTestCase(
            description="normalizes driver sequences into immutable neutral results",
            raw_column_names=["deployment_id", "status"],
            raw_result_rows=[["dep_1", "open"], ["dep_2", "failed"]],
            expected_column_names=("deployment_id", "status"),
            expected_rows=(("dep_1", "open"), ("dep_2", "failed")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_driver_rows_when_querying_through_the_adapter_then_it_returns_neutral_tuples(
    test_case: ConnectionQueryNormalizationTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(
            column_names=test_case.raw_column_names,
            result_rows=test_case.raw_result_rows,
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    result: AdapterQueryResult = connection.query("SELECT deployment_id, status FROM deployments")

    assert result.column_names == test_case.expected_column_names
    assert result.rows == test_case.expected_rows


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseWorkflowCorrelationTestCase(
            description="correlates workflow queries and mutations with ClickHouse query IDs",
            expected_query_ids=("query-123", "query-456"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_workflow_query_id_when_executing_then_clickhouse_parameter_correlates_request(
    test_case: ClickHouseWorkflowCorrelationTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(column_names=[], result_rows=[])
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    connection.execute_workflow_query(statement="SELECT 1", query_id="query-123")
    connection.execute_workflow_mutation(statement="DROP TABLE test", query_id="query-456")

    assert raw_client.query_settings == [
        {"query_id": test_case.expected_query_ids[0]},
        {"query_id": test_case.expected_query_ids[1]},
    ]


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseStatementProgressTestCase(
            description="normalizes active ClickHouse process telemetry",
            expected_progress=AdapterStatementProgress(
                elapsed_seconds=12.5,
                read_rows=1000,
                read_bytes=2048,
                total_rows_approx=5000,
                memory_usage_bytes=4096,
                settings=(("max_threads", "1"),),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_clickhouse_process_when_loading_progress_then_telemetry_is_normalized(
    test_case: ClickHouseStatementProgressTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(
            column_names=[
                "elapsed",
                "read_rows",
                "read_bytes",
                "total_rows_approx",
                "memory_usage",
                "settings",
            ],
            result_rows=[[12.5, 1000, 2048, 5000, 4096, {"max_threads": "1"}]],
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    progress: AdapterStatementProgress | None = connection.load_statement_progress(
        query_id="query-123"
    )

    assert progress == test_case.expected_progress


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseQueryCancellationTestCase(
            description="exact owned query is killed and confirmed absent",
            query_id="query-'owned",
            expected_cancellation=AdapterQueryCancellation(
                query_id="query-'owned",
                supported=True,
                query_found=True,
                termination_confirmed=True,
            ),
            expected_kill_fragment="KILL QUERY WHERE query_id = 'query-''owned' SYNC",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_owned_query_when_cancelling_then_clickhouse_kills_exact_id_and_confirms_absence(
    test_case: ClickHouseQueryCancellationTestCase,
) -> None:
    raw_client: SequencedRawClickHouseClient = SequencedRawClickHouseClient(
        (
            FakeRawClickHouseQueryResult(
                column_names=["kill_status", "query_id"],
                result_rows=[["finished", test_case.query_id]],
            ),
            FakeRawClickHouseQueryResult(
                column_names=[
                    "elapsed",
                    "read_rows",
                    "read_bytes",
                    "total_rows_approx",
                    "memory_usage",
                    "settings",
                ],
                result_rows=[],
            ),
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    cancellation: AdapterQueryCancellation = connection.cancel_workflow_query(
        query_id=test_case.query_id
    )

    assert cancellation == test_case.expected_cancellation
    assert test_case.expected_kill_fragment in raw_client.statements[0]
    assert "system.processes" in raw_client.statements[1]


@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseReplayFrontierTestCase(
            description="target offsets are observed for captured replay partitions",
            request=AdapterReplayOffsetProgressRequest(
                database="analytics",
                relation="tbl__orders",
                partition_column="_replay_partition",
                offset_column="_replay_offset",
                ranges=(
                    AdapterReplayOffsetRange(partition=2, lower_offset=300, upper_offset=500),
                    AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=200),
                ),
            ),
            expected_frontiers=(
                AdapterReplayOffsetFrontier(partition=0, completed_offset=150),
                AdapterReplayOffsetFrontier(partition=2, completed_offset=450),
            ),
            expected_query_fragments=("FROM `analytics`.`tbl__orders`", "IN (0, 2)"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_replay_ranges_when_loading_frontiers_then_target_offsets_are_observed(
    test_case: ClickHouseReplayFrontierTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(
            column_names=["partition", "completed_offset"],
            result_rows=[[0, 150], [2, 450]],
        )
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))

    frontiers: tuple[AdapterReplayOffsetFrontier, ...] = connection.load_replay_offset_frontiers(
        query_id="query-123", request=test_case.request
    )
    cached_frontiers: tuple[AdapterReplayOffsetFrontier, ...] = (
        connection.load_replay_offset_frontiers(query_id="query-123", request=test_case.request)
    )

    assert frontiers == test_case.expected_frontiers
    assert cached_frontiers == frontiers
    assert len(raw_client.queries) == 1
    assert all(
        fragment in raw_client.queries[-1] for fragment in test_case.expected_query_fragments
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ConnectionTranslationTestCase(
            description="translates a query relation failure",
            driver_error=DatabaseError(
                "Code: 60. DB::Exception: Table analytics.tbl__orders does not exist. "
                "(UNKNOWN_TABLE)"
            ),
            expected_error_type=AdapterRelationNotFoundError,
        ),
        ConnectionTranslationTestCase(
            description="translates a query authentication failure",
            driver_error=OperationalError(
                "Code: 516. DB::Exception: Authentication failed. (AUTHENTICATION_FAILED)"
            ),
            expected_error_type=AdapterAuthenticationError,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failing_driver_when_querying_then_no_driver_exception_escapes(
    test_case: ConnectionTranslationTestCase,
) -> None:
    connection: ClickHouseConnection = ClickHouseConnection(
        cast(RawClickHouseClient, FailingRawClickHouseClient(test_case.driver_error))
    )

    with pytest.raises(AdapterWarehouseError) as error_info:
        connection.query("SELECT 1")

    assert type(error_info.value) is test_case.expected_error_type
    assert not isinstance(error_info.value, DatabaseError | OperationalError)


@pytest.mark.parametrize(
    "test_case",
    [
        ConnectionTranslationTestCase(
            description="translates a close failure",
            driver_error=DatabaseError(
                "Code: 60. DB::Exception: Connection target disappeared. (UNKNOWN_TABLE)"
            ),
            expected_error_type=AdapterRelationNotFoundError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_failing_driver_when_closing_then_it_raises_the_neutral_equivalent(
    test_case: ConnectionTranslationTestCase,
) -> None:
    connection: ClickHouseConnection = ClickHouseConnection(
        cast(RawClickHouseClient, FailingRawClickHouseClient(test_case.driver_error))
    )

    with pytest.raises(AdapterWarehouseError) as error_info:
        connection.close()

    assert type(error_info.value) is test_case.expected_error_type
