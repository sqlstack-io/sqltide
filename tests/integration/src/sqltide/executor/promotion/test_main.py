from collections.abc import Sequence

import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.executor.backfill.models import BackfillExecutionResult
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest, PublishResult
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
    render_create_view_ddl,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill.helpers import (
    build_raw_orders_row,
    build_scalar_replay_compiled_pipeline,
    build_scalar_replay_request,
    build_scalar_target_insert_select_sql,
    execute_backfill,
    require_managed_source,
    require_model_resources,
)
from tests.integration.src.sqltide.executor.promotion._test_types import (
    ExecutePublishIntegrationTestCase,
    MissingPublishDeploymentIntegrationTestCase,
    PublishAfterDeletedActiveViewIntegrationTestCase,
    PublishMissingStagedTableIntegrationTestCase,
    PublishWithoutMetadataIntegrationTestCase,
    ResolvePublishDeploymentIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.promotion.helpers import (
    prepare_publish_resolution_scenario,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingPublishDeploymentIntegrationTestCase(
            description="reports no staged deployment when inventory is empty",
            expected_error_fragment="No staged deployment candidates are available for publish",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_inventory_when_resolving_publish_without_deployment_id_then_it_raises(
    test_case: MissingPublishDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    try:
        with pytest.raises(ValueError, match=test_case.expected_error_fragment):
            execute_publish(
                request=PublishRequest(
                    deployment_id=None,
                    metadata_database=clickhouse_database,
                    default_database=clickhouse_database,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecutePublishIntegrationTestCase(
            description="publishes greenfield stable view for scalar replay deployment",
            replay_lineage_mode="timestamp",
            deployment_id="20260409T180000Z_ab12cd",
            created_at="2026-04-09 18:00:00.123",
            boundary_time="2026-04-09 18:00:00.000",
            expected_view_name="tbl__orders_enriched",
            expected_target_table_name="tbl__orders_enriched__20260409T180000Z_ab12cd",
            expected_published_order_ids=("historical-order",),
            expected_full_layout=(
                ("_sqltide_dead_letters", "MergeTree"),
                ("_sqltide_direct_fingerprints", "MergeTree"),
                ("_sqltide_invocations", "MergeTree"),
                ("_sqltide_manifests", "MergeTree"),
                ("_sqltide_node_results", "MergeTree"),
                ("_sqltide_run_events", "MergeTree"),
                ("_sqltide_run_statements", "ReplacingMergeTree"),
                ("_sqltide_schema_versions", "MergeTree"),
                ("_sqltide_sensor_checkpoints", "MergeTree"),
                ("_sqltide_sensor_leases", "MergeTree"),
                ("_sqltide_sensor_overrides", "MergeTree"),
                ("_sqltide_sensor_steps", "MergeTree"),
                ("_sqltide_sensor_ticks", "MergeTree"),
                ("_sqltide_virtual_deployments", "MergeTree"),
                ("_sqltide_virtual_object_state", "MergeTree"),
                ("_sqltide_virtual_publications", "MergeTree"),
                ("_sqltide_virtual_replay_boundaries", "MergeTree"),
                ("kafka__orders", "Kafka"),
                ("mv__orders", "MaterializedView"),
                ("mv__orders_enriched__20260409T180000Z_ab12cd", "MaterializedView"),
                ("raw__orders", "MergeTree"),
                ("tbl__orders_enriched", "View"),
                ("tbl__orders_enriched__20260409T180000Z_ab12cd", "MergeTree"),
            ),
            expected_publish_history_rows=(
                (
                    "20260409T180000Z_ab12cd",
                    '["tbl__orders_enriched"]',
                ),
            ),
            expected_per_relation_atomic_replace=True,
            expected_graph_atomic_publish=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_greenfield_staged_deployment_when_publishing_then_it_creates_stable_view(
    test_case: ExecutePublishIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline(
        test_case.replay_lineage_mode
    )
    assert test_case.deployment_id is not None
    deployment_id: str = test_case.deployment_id
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 17:59:59.000",
                _replay_landed_at="2026-04-09 17:59:59.000",
            ),
            build_raw_orders_row(
                kafka_key="live-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 18:00:01.000",
                _replay_landed_at="2026-04-09 18:00:01.000",
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        backfill_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode=test_case.replay_lineage_mode,
            ),
            client=managed_client,
        )
        publish_result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    system_view_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name = '{test_case.expected_view_name}'"
    ).result_rows
    published_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id FROM "
        f"{clickhouse_database}.{test_case.expected_view_name} "
        "ORDER BY order_id"
    ).result_rows
    full_layout_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine FROM system.tables "
        f"WHERE database = '{clickhouse_database}' ORDER BY name"
    ).result_rows
    publish_history_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT deployment_id, toJSONString(groupArray(logical_view_name)) FROM "
        f"{clickhouse_database}._sqltide_virtual_publications "
        "GROUP BY deployment_id, publication_id, published_at "
        "ORDER BY deployment_id, published_at"
    ).result_rows

    assert backfill_result.bootstrap.deployment_id == deployment_id
    assert publish_result.deployment_id == deployment_id
    assert (
        publish_result.per_relation_atomic_replace is test_case.expected_per_relation_atomic_replace
    )
    assert publish_result.graph_atomic_publish is test_case.expected_graph_atomic_publish
    assert publish_result.published_views[0].view_name == test_case.expected_view_name
    assert (
        publish_result.published_views[0].target_table_name == test_case.expected_target_table_name
    )
    assert system_view_rows == [(test_case.expected_view_name, "View")]
    assert published_rows == [(order_id,) for order_id in test_case.expected_published_order_ids]
    assert full_layout_rows == list(test_case.expected_full_layout)
    assert publish_history_rows == list(test_case.expected_publish_history_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ResolvePublishDeploymentIntegrationTestCase(
            description="auto resolves latest staged deployment newer than active view target",
            create_active_view=True,
            first_deployment_id="20260409T221000Z_ab12cd",
            second_deployment_id="20260409T221500Z_cd34ef",
            expected_resolved_deployment_id="20260409T221500Z_cd34ef",
            expected_error_fragment=None,
            expected_target_table_name="tbl__orders_enriched__20260409T221500Z_cd34ef",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_active_view_when_resolving_publish_without_deployment_id_then_it_selects_latest(
    test_case: ResolvePublishDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = prepare_publish_resolution_scenario(
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        first_deployment_id=test_case.first_deployment_id,
        second_deployment_id=test_case.second_deployment_id,
        create_active_view=test_case.create_active_view,
    )

    try:
        result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=None,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    assert test_case.expected_target_table_name is not None
    assert result.deployment_id == test_case.expected_resolved_deployment_id
    assert result.published_views[0].target_table_name == test_case.expected_target_table_name
    assert result.per_relation_atomic_replace is True
    assert result.graph_atomic_publish is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ResolvePublishDeploymentIntegrationTestCase(
            description="requires explicit choice with no active view and many staged deployments",
            create_active_view=False,
            first_deployment_id="20260409T220000Z_ab12cd",
            second_deployment_id="20260409T220500Z_cd34ef",
            expected_resolved_deployment_id=None,
            expected_error_fragment="Publish deployment resolution is ambiguous",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_publish_request_without_deployment_id_when_resolution_is_ambiguous_then_it_raises(
    test_case: ResolvePublishDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = prepare_publish_resolution_scenario(
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        first_deployment_id=test_case.first_deployment_id,
        second_deployment_id=test_case.second_deployment_id,
        create_active_view=test_case.create_active_view,
    )
    try:
        with pytest.raises(ValueError, match=test_case.expected_error_fragment):
            execute_publish(
                request=PublishRequest(
                    deployment_id=None,
                    metadata_database=clickhouse_database,
                    default_database=clickhouse_database,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PublishWithoutMetadataIntegrationTestCase(
            description="publishes explicit deployment after metadata deletion using live state",
            deployment_id="20260409T223000Z_ab12cd",
            expected_deployment_id="20260409T223000Z_ab12cd",
            expected_target_table_name="tbl__orders_enriched__20260409T223000Z_ab12cd",
        ),
        PublishWithoutMetadataIntegrationTestCase(
            description="resolves publish deployment after metadata deletion using live state",
            deployment_id=None,
            expected_deployment_id="20260409T223000Z_ab12cd",
            expected_target_table_name="tbl__orders_enriched__20260409T223000Z_ab12cd",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_publish_metadata_when_publishing_then_it_uses_live_clickhouse_state(
    test_case: PublishWithoutMetadataIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 22:29:59.000",
                _replay_landed_at="2026-04-09 22:29:59.000",
            ),
            build_raw_orders_row(
                kafka_key="live-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 22:30:01.000",
                _replay_landed_at="2026-04-09 22:30:01.000",
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id="20260409T223000Z_ab12cd",
                created_at="2026-04-09 22:30:00.123",
                boundary_time="2026-04-09 22:30:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_replay_boundaries"
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_deployments"
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_object_state"
        )
        result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    published_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {clickhouse_database}.tbl__orders_enriched ORDER BY order_id"
    ).result_rows

    assert result.deployment_id == test_case.expected_deployment_id
    assert result.published_views[0].target_table_name == test_case.expected_target_table_name
    assert published_rows == [("historical-order",)]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PublishMissingStagedTableIntegrationTestCase(
            description="fails when the staged table was deleted before publish",
            deployment_id="20260409T224000Z_ab12cd",
            expected_error_fragment="is missing staged relations",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_staged_table_when_publishing_then_it_fails_conservatively(
    test_case: PublishMissingStagedTableIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 22:39:59.000",
                _replay_landed_at="2026-04-09 22:39:59.000",
            )
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at="2026-04-09 22:40:00.123",
                boundary_time="2026-04-09 22:40:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            f"DROP TABLE {clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id}"
        )
        with pytest.raises(ValueError, match=test_case.expected_error_fragment):
            execute_publish(
                request=PublishRequest(
                    deployment_id=test_case.deployment_id,
                    metadata_database=clickhouse_database,
                    default_database=clickhouse_database,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PublishAfterDeletedActiveViewIntegrationTestCase(
            description=(
                "publishes explicit staged deployment after the active stable view was deleted"
            ),
            active_deployment_id="20260409T223500Z_prev01",
            staged_deployment_id="20260409T224500Z_ab12cd",
            expected_target_table_name="tbl__orders_enriched__20260409T224500Z_ab12cd",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_active_view_when_publishing_then_it_recreates_stable_view(
    test_case: PublishAfterDeletedActiveViewIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            (f"{clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id}"),
            1,
        )
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name="tbl__orders_enriched",
            target_table_name=(f"tbl__orders_enriched__{test_case.active_deployment_id}"),
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 22:44:59.000",
                _replay_landed_at="2026-04-09 22:44:59.000",
            )
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    clickhouse_client.command(
        "INSERT INTO "
        f"{clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id} "
        + build_scalar_target_insert_select_sql(
            replay_lineage_mode="timestamp",
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.tbl__orders_enriched__{test_case.staged_deployment_id}",
            1,
        )
    )
    clickhouse_client.command(
        "INSERT INTO "
        f"{clickhouse_database}.tbl__orders_enriched__{test_case.staged_deployment_id} "
        + build_scalar_target_insert_select_sql(
            replay_lineage_mode="timestamp",
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    clickhouse_client.command(f"DROP VIEW {clickhouse_database}.tbl__orders_enriched")
    try:
        publish_result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=test_case.staged_deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    system_view_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine, as_select FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name = 'tbl__orders_enriched'"
    ).result_rows

    assert publish_result.deployment_id == test_case.staged_deployment_id
    assert (
        publish_result.published_views[0].target_table_name == test_case.expected_target_table_name
    )
    assert system_view_rows[0][0] == "tbl__orders_enriched"
    assert system_view_rows[0][1] == "View"
    assert str(system_view_rows[0][2]).endswith(
        f"FROM {clickhouse_database}.{test_case.expected_target_table_name}"
    )
