from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier

import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError, AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterConnectionConfig,
    AdapterCurrentQualityNode,
    AdapterDeploymentInventory,
    AdapterManifest,
    AdapterManifestResource,
    AdapterManifestSnapshot,
    AdapterMetadataState,
    AdapterNodeResultRecord,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.integration.src.sqltide.adapters.clickhouse._test_types import (
    LatestNodeStatusIntegrationTestCase,
    LegacyNodeResultsSchemaTestCase,
    LegacyPublicationMigrationTestCase,
    ManifestHistoryIntegrationTestCase,
    MetadataMigrationIntegrationTestCase,
    StreamBuildMetadataRenameTestCase,
)
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    build_invocation_record,
    create_streambuild_metadata,
    execute_rendered_statements,
    expected_metadata_table_state,
    integer_rows,
    metadata_table_state,
    run_metadata_migration,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LegacyNodeResultsSchemaTestCase(
            description="legacy node-result shape fails before migration writes",
            expected_error_fragment="incompatible pre-0.11 schema",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_legacy_node_results_table_when_migrating_then_reset_instruction_is_explicit(
    test_case: LegacyNodeResultsSchemaTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}._sqltide_node_results ("
        "result_id String, invocation_id String, node_kind String, node_identity String, "
        "definition_fingerprint String) ENGINE = MergeTree ORDER BY result_id"
    )

    with pytest.raises(AdapterWarehouseError, match=test_case.expected_error_fragment):
        managed_clickhouse_client.render_migrate_metadata_state(clickhouse_database)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MetadataMigrationIntegrationTestCase(
            description="empty metadata state migrates repeatedly without duplicate versions",
            expected_table_names=(
                "_sqltide_dead_letters",
                "_sqltide_direct_fingerprints",
                "_sqltide_invocations",
                "_sqltide_manifests",
                "_sqltide_node_results",
                "_sqltide_run_events",
                "_sqltide_run_statements",
                "_sqltide_schema_versions",
                "_sqltide_sensor_checkpoints",
                "_sqltide_sensor_leases",
                "_sqltide_sensor_overrides",
                "_sqltide_sensor_steps",
                "_sqltide_sensor_ticks",
                "_sqltide_virtual_deployments",
                "_sqltide_virtual_object_state",
                "_sqltide_virtual_publications",
                "_sqltide_virtual_replay_boundaries",
            ),
            expected_version_rows=((8,),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_database_when_migrating_metadata_repeatedly_then_schema_is_idempotent(
    test_case: MetadataMigrationIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        execute_rendered_statements(
            client=clickhouse_client,
            statements=connection.render_migrate_metadata_state(clickhouse_database),
        )
        execute_rendered_statements(
            client=clickhouse_client,
            statements=connection.render_migrate_metadata_state(clickhouse_database),
        )
    finally:
        connection.close()

    table_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name LIKE '\\_sqltide%' ORDER BY name"
    ).result_rows
    version_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT version FROM {clickhouse_database}._sqltide_schema_versions ORDER BY version"
    ).result_rows

    assert tuple(str(row[0]) for row in table_rows) == test_case.expected_table_names
    assert integer_rows(version_rows) == test_case.expected_version_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ManifestHistoryIntegrationTestCase(
            description="complete manifest rows load newest first",
            expected_manifest_ids=("manifest-2", "manifest-1"),
            expected_latest_revision="revision-2",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_manifests_when_loading_history_then_complete_rows_are_newest_first(
    test_case: ManifestHistoryIntegrationTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    execute_rendered_statements(
        client=clickhouse_client,
        statements=managed_clickhouse_client.render_migrate_metadata_state(clickhouse_database),
    )
    resource: AdapterManifestResource = AdapterManifestResource(
        pipeline_name="orders",
        logical_type="model",
        logical_name="orders",
        resource_role="model_table",
        resource_database=clickhouse_database,
        resource_name="tbl__orders",
        resource_kind="table",
    )
    older: AdapterManifest = AdapterManifest(
        manifest_id="manifest-1",
        invocation_id="invocation-1",
        project_identity="/projects/orders",
        target_name="uat",
        target_database=clickhouse_database,
        is_production=False,
        project_revision=None,
        manifest_fingerprint="fingerprint-1",
        manifest_version=1,
        pipelines=("orders",),
        resources=(resource,),
        tool_version="0.37.0",
        published_at="2026-08-28 12:00:00.123456",
    )
    newer: AdapterManifest = AdapterManifest(
        manifest_id="manifest-2",
        invocation_id="invocation-2",
        project_identity=older.project_identity,
        target_name=older.target_name,
        target_database=older.target_database,
        is_production=True,
        project_revision="revision-2",
        manifest_fingerprint="fingerprint-2",
        manifest_version=older.manifest_version,
        pipelines=older.pipelines,
        resources=older.resources,
        tool_version=older.tool_version,
        published_at=older.published_at,
    )
    for manifest in (older, newer):
        execute_rendered_statements(
            client=clickhouse_client,
            statements=managed_clickhouse_client.render_manifest_publication(
                database=clickhouse_database, manifest=manifest
            ),
        )

    snapshot: AdapterManifestSnapshot = managed_clickhouse_client.load_manifests(
        database=clickhouse_database,
        project_identity=older.project_identity,
        target_name=older.target_name,
        target_database=older.target_database,
    )

    assert snapshot.status == "available"
    assert tuple(manifest.manifest_id for manifest in snapshot.manifests) == (
        test_case.expected_manifest_ids
    )
    assert snapshot.manifests[0].project_revision == test_case.expected_latest_revision
    assert snapshot.manifests[0].resources == (resource,)
    assert snapshot.manifests[1].project_revision is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LegacyPublicationMigrationTestCase(
            description="v2 publication rows gain default lifecycle values",
            expected_operation="promote",
            expected_publication_id="publication-1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_v2_publication_rows_when_migrating_then_lifecycle_defaults_are_preserved(
    test_case: LegacyPublicationMigrationTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}._sqltide_virtual_publications ("
        "publication_id String, deployment_id String, logical_database_name String, "
        "logical_view_name String, physical_database_name String, physical_relation_name String, "
        "published_at DateTime64(3, 'UTC')) ENGINE = MergeTree "
        "ORDER BY (publication_id, logical_database_name, logical_view_name)"
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}._sqltide_virtual_publications VALUES "
        "('publication-1', 'deployment-1', 'analytics', 'orders', 'analytics', "
        "'orders__deployment-1', '2026-08-08 12:00:00.000')"
    )

    before: AdapterDeploymentInventory = managed_clickhouse_client.load_deployment_inventory(
        clickhouse_database
    )
    execute_rendered_statements(
        client=clickhouse_client,
        statements=managed_clickhouse_client.render_migrate_metadata_state(clickhouse_database),
    )
    after: AdapterDeploymentInventory = managed_clickhouse_client.load_deployment_inventory(
        clickhouse_database
    )

    assert before.publish_events[0].operation == test_case.expected_operation
    assert before.publish_events[0].previous_deployment_id is None
    assert after.publish_events[0].operation == test_case.expected_operation
    assert after.publish_events[0].previous_deployment_id is None
    assert after.publish_events[0].publication_id == test_case.expected_publication_id


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MetadataMigrationIntegrationTestCase(
            description="concurrent metadata migrations converge on one logical version",
            expected_table_names=(
                "_sqltide_dead_letters",
                "_sqltide_direct_fingerprints",
                "_sqltide_invocations",
                "_sqltide_manifests",
                "_sqltide_node_results",
                "_sqltide_run_events",
                "_sqltide_run_statements",
                "_sqltide_schema_versions",
                "_sqltide_sensor_checkpoints",
                "_sqltide_sensor_leases",
                "_sqltide_sensor_overrides",
                "_sqltide_sensor_steps",
                "_sqltide_sensor_ticks",
                "_sqltide_virtual_deployments",
                "_sqltide_virtual_object_state",
                "_sqltide_virtual_publications",
                "_sqltide_virtual_replay_boundaries",
            ),
            expected_version_rows=((8,),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_database_when_migrating_metadata_concurrently_then_attempts_are_safe(
    test_case: MetadataMigrationIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    start_barrier: Barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: tuple[Future[None], ...] = (
            executor.submit(
                run_metadata_migration,
                connection_settings=clickhouse_connection_settings,
                database=clickhouse_database,
                start_barrier=start_barrier,
            ),
            executor.submit(
                run_metadata_migration,
                connection_settings=clickhouse_connection_settings,
                database=clickhouse_database,
                start_barrier=start_barrier,
            ),
        )
        _ = tuple(future.result() for future in futures)

    table_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name LIKE '\\_sqltide%' ORDER BY name"
    ).result_rows
    version_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT DISTINCT version FROM "
        f"{clickhouse_database}._sqltide_schema_versions ORDER BY version"
    ).result_rows

    assert tuple(str(row[0]) for row in table_rows) == test_case.expected_table_names
    assert integer_rows(version_rows) == test_case.expected_version_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LatestNodeStatusIntegrationTestCase(
            description="latest deterministic results classify current stale and never-run nodes",
            expected_status_rows=(
                ("current audit", "failed"),
                ("never audit", "never_run"),
                ("stale test", "definition_changed"),
            ),
            expected_drift_rows=(
                ("current audit", ()),
                ("never audit", ()),
                ("stale test", ("definition_changed",)),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_manifest_nodes_and_result_history_when_querying_then_ui_states_are_classified(
    test_case: LatestNodeStatusIntegrationTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    execute_rendered_statements(
        client=clickhouse_client,
        statements=managed_clickhouse_client.render_migrate_metadata_state(clickhouse_database),
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = (
        AdapterNodeResultRecord(
            result_id="result-a",
            invocation_id="inv-1",
            node_kind="audit",
            node_name="current audit",
            binding_key="current-binding",
            definition_fingerprint="current-fingerprint",
            execution_fingerprint="current-execution",
            target_identity=clickhouse_database,
            trigger="manual",
            scheduled_for=None,
            cadence_seconds=None,
            warmup_seconds=0,
            status="passed",
            severity="error",
            failure_count=0,
            completed_at="2026-08-02 12:00:00.000",
            payload_json="{}",
            error_message=None,
        ),
        AdapterNodeResultRecord(
            result_id="result-z",
            invocation_id="inv-2",
            node_kind="audit",
            node_name="current audit",
            binding_key="current-binding",
            definition_fingerprint="current-fingerprint",
            execution_fingerprint="current-execution",
            target_identity=clickhouse_database,
            trigger="build",
            scheduled_for=None,
            cadence_seconds=None,
            warmup_seconds=0,
            status="failed",
            severity="error",
            failure_count=2,
            completed_at="2026-08-02 12:00:00.000",
            payload_json="{}",
            error_message=None,
        ),
        AdapterNodeResultRecord(
            result_id="result-stale",
            invocation_id="inv-3",
            node_kind="test",
            node_name="stale test",
            binding_key="stale-binding",
            definition_fingerprint="old-fingerprint",
            execution_fingerprint="stale-execution",
            target_identity=clickhouse_database,
            trigger="manual",
            scheduled_for=None,
            cadence_seconds=None,
            warmup_seconds=0,
            status="passed",
            severity=None,
            failure_count=0,
            completed_at="2026-08-02 12:01:00.000",
            payload_json="{}",
            error_message=None,
        ),
        AdapterNodeResultRecord(
            result_id="result-newer-other-fingerprint",
            invocation_id="inv-4",
            node_kind="audit",
            node_name="current audit",
            binding_key="current-binding",
            definition_fingerprint="other-fingerprint",
            execution_fingerprint="current-execution",
            target_identity=clickhouse_database,
            trigger="manual",
            scheduled_for=None,
            cadence_seconds=None,
            warmup_seconds=0,
            status="passed",
            severity="error",
            failure_count=0,
            completed_at="2026-08-02 12:02:00.000",
            payload_json="{}",
            error_message=None,
        ),
        AdapterNodeResultRecord(
            result_id="result-other-project",
            invocation_id="inv-other-project",
            node_kind="audit",
            node_name="current audit",
            binding_key="current-binding",
            definition_fingerprint="current-fingerprint",
            execution_fingerprint="current-execution",
            target_identity=clickhouse_database,
            trigger="deployment",
            scheduled_for=None,
            cadence_seconds=None,
            warmup_seconds=0,
            status="passed",
            severity="error",
            failure_count=0,
            completed_at="2026-08-02 12:03:00.000",
            payload_json="{}",
            error_message=None,
        ),
    )
    project_identity: str = "/project/current"
    state: AdapterMetadataState = AdapterMetadataState(
        object_states=(),
        deployments=(),
        deployment_watermarks=(),
        publish_events=(),
        invocations=(
            build_invocation_record(
                invocation_id="inv-1",
                project_identity=project_identity,
                target_identity=clickhouse_database,
                completed_at="2026-08-02 12:00:00.000",
            ),
            build_invocation_record(
                invocation_id="inv-2",
                project_identity=project_identity,
                target_identity=clickhouse_database,
                completed_at="2026-08-02 12:00:00.000",
            ),
            build_invocation_record(
                invocation_id="inv-3",
                project_identity=project_identity,
                target_identity=clickhouse_database,
                completed_at="2026-08-02 12:01:00.000",
            ),
            build_invocation_record(
                invocation_id="inv-4",
                project_identity=project_identity,
                target_identity=clickhouse_database,
                completed_at="2026-08-02 12:02:00.000",
            ),
            build_invocation_record(
                invocation_id="inv-other-project",
                project_identity="/project/other",
                target_identity=clickhouse_database,
                completed_at="2026-08-02 12:03:00.000",
            ),
        ),
        node_results=node_results,
    )
    execute_rendered_statements(
        client=clickhouse_client,
        statements=managed_clickhouse_client.render_persist_metadata_state(
            database=clickhouse_database, state=state
        ),
    )
    query: str = managed_clickhouse_client.render_latest_node_status_query(
        database=clickhouse_database,
        project_identity=project_identity,
        target_identity=clickhouse_database,
        nodes=(
            AdapterCurrentQualityNode(
                "audit",
                "current audit",
                "current-binding",
                "current-fingerprint",
                "current-execution",
            ),
            AdapterCurrentQualityNode(
                "audit", "never audit", "never-binding", "never-fingerprint", "never-execution"
            ),
            AdapterCurrentQualityNode(
                "test", "stale test", "stale-binding", "new-fingerprint", "stale-execution"
            ),
        ),
    )
    rows: Sequence[Sequence[object]] = clickhouse_client.query(query).result_rows

    assert tuple((str(row[1]), str(row[7])) for row in rows) == test_case.expected_status_rows
    assert tuple((str(row[1]), tuple(row[8])) for row in rows) == test_case.expected_drift_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        StreamBuildMetadataRenameTestCase(
            description="StreamBuild metadata is renamed with its rows, and again is a no-op",
            extra_tables=(),
            expected_error_fragment=None,
            expected_table_prefix="_sqltide_",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_streambuild_metadata_when_renaming_then_tables_keep_their_rows(
    test_case: StreamBuildMetadataRenameTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    metadata_tables: tuple[str, ...] = create_streambuild_metadata(
        connection=managed_clickhouse_client,
        client=clickhouse_client,
        database=clickhouse_database,
        extra_tables=test_case.extra_tables,
    )

    managed_clickhouse_client.rename_streambuild_metadata(clickhouse_database)
    managed_clickhouse_client.rename_streambuild_metadata(clickhouse_database)

    assert metadata_table_state(
        client=clickhouse_client,
        database=clickhouse_database,
        table_prefix=test_case.expected_table_prefix,
        extra_tables=test_case.extra_tables,
    ) == expected_metadata_table_state(
        metadata_tables=metadata_tables, table_prefix=test_case.expected_table_prefix
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        StreamBuildMetadataRenameTestCase(
            description="a held StreamBuild mutation lock leaves every table in place",
            extra_tables=("_streambuild_target_mutation_lock",),
            expected_error_fragment="mutation-locked by a StreamBuild run",
            expected_table_prefix="_streambuild_",
        ),
        StreamBuildMetadataRenameTestCase(
            description="metadata already present under both names is never merged",
            extra_tables=("_sqltide_manifests",),
            expected_error_fragment="contains both StreamBuild and SQLTide metadata",
            expected_table_prefix="_streambuild_",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_streambuild_metadata_when_renaming_then_nothing_moves(
    test_case: StreamBuildMetadataRenameTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    metadata_tables: tuple[str, ...] = create_streambuild_metadata(
        connection=managed_clickhouse_client,
        client=clickhouse_client,
        database=clickhouse_database,
        extra_tables=test_case.extra_tables,
    )

    with pytest.raises(AdapterError, match=test_case.expected_error_fragment):
        managed_clickhouse_client.rename_streambuild_metadata(clickhouse_database)

    assert metadata_table_state(
        client=clickhouse_client,
        database=clickhouse_database,
        table_prefix=test_case.expected_table_prefix,
        extra_tables=test_case.extra_tables,
    ) == expected_metadata_table_state(
        metadata_tables=metadata_tables, table_prefix=test_case.expected_table_prefix
    )
