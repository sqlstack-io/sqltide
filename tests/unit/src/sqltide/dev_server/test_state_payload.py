import json
from pathlib import Path

import pytest

from sqltide._native import DirectModelFingerprint, create_dev_app
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterWarehouseActivity,
    AdapterWarehouseDisk,
    AdapterWarehouseHealth,
    AdapterWarehouseKafkaConsumers,
    AdapterWarehouseMemory,
    AdapterWarehouseTable,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import CompiledModel
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server._helpers.payloads.state_payload import (
    build_partitions_query,
    build_throughputs_query,
)
from sqltide.dev_server.classes.dev_server_state import DevServerState
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    ModelDriftDifferencePayloadTestCase,
    ModelDriftUnavailablePayloadTestCase,
    SourceObservationBatchTestCase,
    StateFieldTestCase,
    UnconfiguredFreshnessTestCase,
    ViewFreshnessTestCase,
    WarehouseHealthPayloadTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_compile_callable,
    build_fake_state_connection,
    changed_storage_identity,
    write_dev_server_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        SourceObservationBatchTestCase(
            description="many sources compile into two batched observation statements",
            relation_count=89,
            expected_statement_count=2,
            expected_union_count=88,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_many_sources_when_building_observation_queries_then_fanout_stays_constant(
    test_case: SourceObservationBatchTestCase,
) -> None:
    relation_names: tuple[str, ...] = tuple(
        f"raw__source_{index}" for index in range(test_case.relation_count)
    )
    windows: tuple[tuple[str, int, int], ...] = tuple(
        (relation_name, 3600, 60) for relation_name in relation_names
    )

    statements: tuple[str, str] = (
        build_throughputs_query(database="analytics", windows=windows),
        build_partitions_query(database="analytics", relation_names=relation_names),
    )

    assert len(statements) == test_case.expected_statement_count
    for statement in statements:
        assert statement.count(" UNION ALL ") == test_case.expected_union_count
        for relation_name in relation_names:
            assert f"'{relation_name}' AS relation" in statement


@pytest.mark.parametrize(
    "test_case",
    [
        StateFieldTestCase(
            description="unavailable baseline does not report drift",
            fingerprint_status="unavailable",
            definition_hash_builder=lambda model: DirectModelFingerprint.query_hash(model.query),
            identity_metadata_builder=lambda identity: json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=(),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
        StateFieldTestCase(
            description="absent baseline reports missing fingerprint drift",
            fingerprint_status="absent",
            definition_hash_builder=lambda model: DirectModelFingerprint.query_hash(model.query),
            identity_metadata_builder=lambda identity: json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=("missing",),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
        StateFieldTestCase(
            description="matching baseline does not report drift",
            fingerprint_status="available",
            definition_hash_builder=lambda model: DirectModelFingerprint.query_hash(model.query),
            identity_metadata_builder=lambda identity: json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=(),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
        StateFieldTestCase(
            description="changed query reports query drift",
            fingerprint_status="available",
            definition_hash_builder=lambda _model: DirectModelFingerprint.query_hash(
                "previous query"
            ),
            identity_metadata_builder=lambda identity: json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=("query",),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
        StateFieldTestCase(
            description="changed MODEL storage reports storage drift",
            fingerprint_status="available",
            definition_hash_builder=lambda model: DirectModelFingerprint.query_hash(model.query),
            identity_metadata_builder=changed_storage_identity,
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=("storage",),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
        StateFieldTestCase(
            description="baseline with null storage reports storage drift",
            fingerprint_status="available",
            definition_hash_builder=lambda model: DirectModelFingerprint.query_hash(model.query),
            identity_metadata_builder=lambda identity: json.dumps(
                {**identity, "storage": None},
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_source_freshness="fresh",
            expected_model_freshness="lagging",
            expected_model_lag_seconds=7200.0,
            expected_drift_reasons=("storage",),
            expected_source_rows_per_second=0.083,
            expected_partition_max_offset=91822,
            expected_bucket_count=60,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_warehouse_reads_when_reading_state_then_assembles_expected_overlay(
    test_case: StateFieldTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()
    model: CompiledModel = analysis.compiled_project.models[0]
    desired_identity: dict[str, object] = DirectModelFingerprint.identity(
        model=model,
    )
    baseline: AdapterDirectFingerprintRecord = AdapterDirectFingerprintRecord(
        fingerprint_id="fingerprint",
        logical_model_identity=f"analytics.{model.key.name}",
        definition_sql=model.query,
        definition_hash=test_case.definition_hash_builder(model),
        identity_metadata=test_case.identity_metadata_builder(desired_identity),
        workflow_id="workflow",
        tool_version="test",
    )
    fingerprints: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
        status=test_case.fingerprint_status,
        baselines=(baseline,),
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(fingerprints=fingerprints),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    payload: dict = response.json()

    assert response.status_code == 200
    assert set(payload) == {"capturedAt", "warehouseHealth", "models", "sources"}
    assert isinstance(payload["capturedAt"], str)
    assert payload["warehouseHealth"]["availability"] == "unavailable"
    assert payload["warehouseHealth"]["status"] == "unknown"
    assert payload["warehouseHealth"]["adapter"] == "clickhouse"
    assert payload["warehouseHealth"]["database"] == "analytics"
    assert set(payload["models"]) == {"orders_clean"}
    assert set(payload["sources"]) == {"orders"}
    model: dict = payload["models"]["orders_clean"]
    assert model["freshness"] == test_case.expected_model_freshness
    assert model["lagSeconds"] == test_case.expected_model_lag_seconds
    assert model["activity"]["state"] == "idle"
    assert model["activity"]["source"] == "system_parts"
    assert model["activity"]["approximate"] is True
    assert tuple(sorted(model["driftReasons"])) == test_case.expected_drift_reasons
    assert model["drift"] is bool(test_case.expected_drift_reasons)
    semantic_drift: dict = model["semanticDrift"]
    assert semantic_drift["query"]["status"] == "unavailable"
    assert semantic_drift["query"]["unifiedDiff"] is None
    source: dict = payload["sources"]["orders"]
    assert source["rows"] == 1000
    assert source["oldestEventAt"] == "2026-08-01 00:00:00.000"
    assert source["newestEventAt"] == "2026-08-03 11:59:58.000"
    assert source["freshness"] == test_case.expected_source_freshness
    assert source["lastArrivalSeconds"] == 2.0
    assert source["kafkaLagMessages"] is None
    assert source["rowsPerSecond"] == test_case.expected_source_rows_per_second
    assert source["partitions"][0]["maxOffset"] == test_case.expected_partition_max_offset
    assert len(source["throughput"]["buckets"]) == test_case.expected_bucket_count
    assert sum(source["throughput"]["buckets"]) == 300


@pytest.mark.parametrize(
    "test_case",
    [
        ModelDriftUnavailablePayloadTestCase(
            description=(
                "missing live relation reports unavailable comparison without fabricated diffs"
            ),
            expected_message=(
                "Relation tbl__orders_clean is missing; no live comparison is possible."
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_live_relation_when_reading_state_then_semantic_drift_is_unavailable(
    test_case: ModelDriftUnavailablePayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    empty_catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=(),
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(catalog=empty_catalog),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    drift: dict = response.json()["models"]["orders_clean"]["semanticDrift"]

    assert response.status_code == 200
    assert drift["status"] == "unavailable"
    assert drift["message"] == test_case.expected_message
    assert drift["liveDdl"] is None
    assert drift["schema"]["changes"] == []
    assert drift["query"]["unifiedDiff"] is None


@pytest.mark.parametrize(
    "test_case",
    [
        ModelDriftDifferencePayloadTestCase(
            description="changed live query and engine produce grouped semantic drift evidence",
            expected_query_fragment="--- live warehouse",
            expected_physical_field="Engine",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_changed_live_relation_when_reading_state_then_semantic_diffs_are_explicit(
    test_case: ModelDriftDifferencePayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()
    model: CompiledModel = analysis.compiled_project.models[0]
    live_columns: tuple[CatalogColumn, ...] = tuple(
        CatalogColumn(name=column.name, type=column.type) for column in model.output_columns
    )
    changed_catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="tbl__orders_clean",
                engine="TinyLog",
                columns=live_columns,
                full_engine="TinyLog",
                definition_sql="CREATE TABLE analytics.tbl__orders_clean ENGINE = TinyLog",
            ),
            CatalogRelation(
                name="mv__orders_clean",
                engine="MaterializedView",
                columns=(),
                definition_sql="CREATE MATERIALIZED VIEW analytics.mv__orders_clean AS SELECT 2",
                query_sql="SELECT 2 AS changed",
            ),
        ),
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(catalog=changed_catalog),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    drift: dict = response.json()["models"]["orders_clean"]["semanticDrift"]
    physical_changes: list[dict] = drift["physicalConfiguration"]["changes"]
    physical_by_field: dict[str, dict] = {
        str(change["field"]): change for change in physical_changes
    }
    engine_change: dict = physical_by_field[test_case.expected_physical_field]

    assert response.status_code == 200
    assert drift["status"] == "drift"
    assert test_case.expected_query_fragment in drift["query"]["unifiedDiff"]
    assert "+++ current compiled" in drift["query"]["unifiedDiff"]
    assert engine_change["live"] == "tinylog"
    assert engine_change["status"] == "drift"
    assert "mv__orders_clean" in drift["liveDdl"]


@pytest.mark.parametrize(
    "test_case",
    [
        UnconfiguredFreshnessTestCase(
            description="missing policy leaves source and model freshness unconfigured",
            expected_source_freshness=None,
            expected_model_freshness=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_freshness_policy_when_reading_state_then_freshness_is_unconfigured(
    test_case: UnconfiguredFreshnessTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    source_path: Path = tmp_path / "sources" / "orders.yml"
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace(
            "    freshness:\n      warn_after: 1h\n      error_after: 4h\n",
            "",
        ),
        encoding="utf-8",
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    payload: dict = response.json()

    assert response.status_code == 200
    assert payload["sources"]["orders"]["freshness"] == test_case.expected_source_freshness
    assert payload["models"]["orders_clean"]["freshness"] == test_case.expected_model_freshness


@pytest.mark.parametrize(
    "test_case",
    [
        ViewFreshnessTestCase(
            description="query-only view has no physical freshness measurements",
            expected_freshness=None,
            expected_lag_seconds=None,
            expected_newest_row_at=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_query_only_view_when_reading_state_then_physical_freshness_is_unmeasured(
    test_case: ViewFreshnessTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    (tmp_path / "pipelines" / "order_events" / "orders_summary.sql").write_text(
        "MODEL (\n  kind view,\n);\n\n"
        'SELECT order_id::String AS order_id FROM __ref("orders_clean")\n',
        encoding="utf-8",
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    assert response.status_code == 200, response.text
    view: dict = response.json()["models"]["orders_summary"]
    assert view["freshness"] == test_case.expected_freshness
    assert view["lagSeconds"] == test_case.expected_lag_seconds
    assert view["newestRowAt"] == test_case.expected_newest_row_at


@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthPayloadTestCase(
            description="available diagnostics retain units labels and project table footprint",
            expected_status="warning",
            expected_disk_status="warning",
            expected_memory_basis="server_rss_host",
            expected_table_name="tbl__orders_clean",
            expected_polling_tables=1,
            expected_capacity_warning_fraction=0.15,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_available_warehouse_health_when_reading_state_then_payload_is_truthful(
    test_case: WarehouseHealthPayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    health: AdapterWarehouseHealth = AdapterWarehouseHealth(
        availability="available",
        status=test_case.expected_status,
        version="25.8.1.1",
        uptime_seconds=86400,
        disks=(
            AdapterWarehouseDisk(
                name="default",
                path="/data/clickhouse/",
                disk_type="Local",
                total_bytes=1000,
                free_bytes=160,
                unreserved_bytes=150,
                keep_free_bytes=10,
                status=test_case.expected_disk_status,
            ),
        ),
        inode_total=1000,
        inode_free=500,
        inode_status="healthy",
        memory=AdapterWarehouseMemory(
            resident_bytes=300,
            host_total_bytes=2000,
            cgroup_used_bytes=None,
            cgroup_limit_bytes=None,
            basis=test_case.expected_memory_basis,
            pressure_fraction=None,
        ),
        activity=AdapterWarehouseActivity(
            active_queries=2,
            active_merges=1,
            incomplete_mutations=0,
        ),
        tables=(
            AdapterWarehouseTable(
                name=test_case.expected_table_name,
                rows=900,
                bytes_on_disk=2048,
                active_parts=2,
            ),
        ),
        collection_duration_ms=4,
        kafka_consumers=AdapterWarehouseKafkaConsumers(
            configured_tables=2,
            materialized_tables=1,
            materialized_table_names=("kafka__orders",),
            polling_tables=test_case.expected_polling_tables,
            exception_tables=0,
        ),
        capacity_warning_fraction=test_case.expected_capacity_warning_fraction,
        capacity_critical_fraction=0.05,
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(warehouse_health=health),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get("/api/state")
    payload: dict = response.json()["warehouseHealth"]

    assert response.status_code == 200
    assert payload["status"] == test_case.expected_status
    assert payload["database"] == "analytics"
    assert payload["disks"][0]["status"] == test_case.expected_disk_status
    assert payload["memory"]["basis"] == test_case.expected_memory_basis
    assert payload["memory"]["pressureFraction"] is None
    assert payload["tables"][0]["name"] == test_case.expected_table_name
    assert payload["activity"] == {
        "activeQueries": 2,
        "activeMerges": 1,
        "incompleteMutations": 0,
    }
    assert payload["kafkaConsumers"] == {
        "configuredTables": 2,
        "materializedTables": 1,
        "materializedTableNames": ["kafka__orders"],
        "pollingTables": test_case.expected_polling_tables,
        "exceptionTables": 0,
        "stuckConsumers": [],
    }
    assert payload["capacityWarningFraction"] == test_case.expected_capacity_warning_fraction
