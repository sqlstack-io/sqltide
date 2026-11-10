from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import load_actual_state
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline, DesiredState
from sqltide.compiler.planner.models import ActualMaterializedView, ActualState
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest
from sqltide.executor.reconcile.constants import RECONCILE_DEPLOYMENT_ID_PREFIX
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.compiler.planner._test_types import (
    LoadActualStateIntegrationTestCase,
    LoadActualStateMixedRootsIntegrationTestCase,
    LoadActualStateWithConflictingMetadataIntegrationTestCase,
    LoadActualStateWithLatestObjectStateIntegrationTestCase,
    LoadActualStateWithoutMetadataIntegrationTestCase,
)
from tests.integration.src.sqltide.compiler.planner.helpers import (
    ACTUAL_STATE_SETUP_STEPS,
    MIXED_ROOT_SETUP_STEPS,
    realize_compiled_pipelines,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill.helpers import (
    build_named_scalar_replay_compiled_pipeline,
    build_raw_orders_row,
    build_scalar_replay_compiled_pipeline,
    build_scalar_replay_request,
    execute_backfill,
    require_managed_source,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LoadActualStateIntegrationTestCase(
            description="loads greenfield state when no stable view exists",
            setup_steps=("direct_raw_landing", "target_table"),
            expected_actual_object_names=("kafka__orders", "mv__orders", "raw__orders"),
            expected_error_fragment=None,
        ),
        LoadActualStateIntegrationTestCase(
            description="loads active managed objects when stable view points at deployment table",
            setup_steps=(
                "direct_raw_landing",
                "target_table",
                "physical_candidates",
                "candidate_materialized_view",
                "stable_view",
            ),
            expected_actual_object_names=(
                "kafka__orders",
                "mv__orders",
                "mv__orders_enriched",
                "raw__orders",
                "tbl__orders_enriched",
            ),
            expected_error_fragment=None,
        ),
        LoadActualStateIntegrationTestCase(
            description=(
                "ignores deployment-suffixed raw landing physicals without a stable raw interface"
            ),
            setup_steps=(
                "suffixed_raw_landing",
                "target_table",
                "physical_candidates",
                "stable_view",
            ),
            expected_actual_object_names=(
                "kafka__orders",
                "mv__orders_enriched",
                "tbl__orders_enriched",
            ),
            expected_error_fragment=None,
        ),
        LoadActualStateIntegrationTestCase(
            description="loads no active managed objects when view is missing but candidates exist",
            setup_steps=(
                "direct_raw_landing",
                "target_table",
                "physical_candidates",
                "candidate_materialized_view",
            ),
            expected_actual_object_names=("kafka__orders", "mv__orders", "raw__orders"),
            expected_error_fragment=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_state_when_loading_actual_state_then_it_returns_expected_result(
    test_case: LoadActualStateIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    desired_state: DesiredState = realize_compiled_pipelines((compiled_pipeline,)).desired_state
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    step_name: str
    for step_name in test_case.setup_steps:
        ACTUAL_STATE_SETUP_STEPS[step_name](
            clickhouse_client=clickhouse_client,
            clickhouse_database=clickhouse_database,
            compiled_pipeline=compiled_pipeline,
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
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    assert (
        tuple(object_.name for object_ in actual_state.objects)
        == test_case.expected_actual_object_names
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LoadActualStateWithConflictingMetadataIntegrationTestCase(
            description=(
                "loads active state from the stable view even when deployment metadata "
                "points elsewhere"
            ),
            expected_actual_object_names=(
                "kafka__orders",
                "mv__orders",
                "mv__orders_enriched",
                "raw__orders",
                "tbl__orders_enriched",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_conflicting_metadata_when_loading_actual_state_then_live_view_binding_wins(
    test_case: LoadActualStateWithConflictingMetadataIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    desired_state: DesiredState = realize_compiled_pipelines((compiled_pipeline,)).desired_state
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
                _replay_timestamp="2026-04-09 18:59:59.000",
                _replay_landed_at="2026-04-09 18:59:59.000",
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
                deployment_id="20260409T233000Z_ab12cd",
                created_at="2026-04-09 23:30:00.123",
                boundary_time="2026-04-09 23:30:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id="20260409T233000Z_ab12cd",
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            "INSERT INTO "
            f"{clickhouse_database}._sqltide_virtual_deployments "
            "(deployment_id, workflow_fingerprint, replay_lineage_mode, boundary_time, "
            "created_at, tool_version) VALUES "
            "('20260409T233500Z_wrong00', 'wrong', '_replay_timestamp', "
            "CAST('2026-04-09 23:35:00.000' AS DateTime64(3)), "
            "CAST('2026-04-09 23:35:00.123' AS DateTime64(3)), 'integration')"
        )
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    assert (
        tuple(object_.name for object_ in actual_state.objects)
        == test_case.expected_actual_object_names
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LoadActualStateMixedRootsIntegrationTestCase(
            description=(
                "loads active objects only for the healthy root when another root is missing "
                "its stable view"
            ),
            setup_steps=(
                "orders_candidates",
                "customers_candidates",
                "orders_active_view",
            ),
            expected_actual_object_names=(
                "kafka__customers",
                "kafka__orders",
                "mv__customers",
                "mv__orders",
                "mv__orders_enriched",
                "raw__customers",
                "raw__orders",
                "tbl__orders_enriched",
            ),
        ),
        LoadActualStateMixedRootsIntegrationTestCase(
            description=(
                "loads active objects only for the healthy root when another root has an "
                "invalid active view"
            ),
            setup_steps=(
                "orders_candidates",
                "orders_active_view",
                "customers_invalid_view",
            ),
            expected_actual_object_names=(
                "kafka__customers",
                "kafka__orders",
                "mv__customers",
                "mv__orders",
                "mv__orders_enriched",
                "raw__customers",
                "raw__orders",
                "tbl__orders_enriched",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_root_clickhouse_state_when_loading_then_it_preserves_per_root_state(
    test_case: LoadActualStateMixedRootsIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    orders_pipeline: CompiledPipeline = build_named_scalar_replay_compiled_pipeline(
        replay_lineage_mode="timestamp",
        pipeline_name="orders_pipeline",
        source_name="orders",
        transform_name="orders_enriched",
        topic="source.orders.created",
    )
    customers_pipeline: CompiledPipeline = build_named_scalar_replay_compiled_pipeline(
        replay_lineage_mode="timestamp",
        pipeline_name="customers_pipeline",
        source_name="customers",
        transform_name="customers_enriched",
        topic="source.customers.created",
    )
    desired_state: DesiredState = realize_compiled_pipelines(
        (orders_pipeline, customers_pipeline)
    ).desired_state
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in (orders_pipeline, customers_pipeline):
        clickhouse_client.command(
            render_create_kafka_table_ddl(
                table=require_managed_source(compiled_pipeline).kafka_table,
                database=clickhouse_database,
            )
        )
        clickhouse_client.command(
            render_create_table_ddl(
                table=require_managed_source(compiled_pipeline).raw_table,
                database=clickhouse_database,
            )
        )
        clickhouse_client.command(
            render_create_materialized_view_ddl(
                materialized_view=require_managed_source(compiled_pipeline).materialized_view,
                database=clickhouse_database,
            )
        )
    step_name: str
    for step_name in test_case.setup_steps:
        MIXED_ROOT_SETUP_STEPS[step_name](
            clickhouse_client=clickhouse_client, clickhouse_database=clickhouse_database
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
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    assert (
        tuple(object_.name for object_ in actual_state.objects)
        == test_case.expected_actual_object_names
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LoadActualStateWithoutMetadataIntegrationTestCase(
            description="loads active state after deleting all metadata tables",
            dropped_metadata_tables=(
                "_sqltide_virtual_object_state",
                "_sqltide_virtual_deployments",
                "_sqltide_virtual_replay_boundaries",
            ),
            expected_actual_object_names=(
                "kafka__orders",
                "mv__orders",
                "mv__orders_enriched",
                "raw__orders",
                "tbl__orders_enriched",
            ),
        ),
        LoadActualStateWithoutMetadataIntegrationTestCase(
            description="loads active state after deleting object state metadata only",
            dropped_metadata_tables=("_sqltide_virtual_object_state",),
            expected_actual_object_names=(
                "kafka__orders",
                "mv__orders",
                "mv__orders_enriched",
                "raw__orders",
                "tbl__orders_enriched",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_published_state_when_metadata_is_deleted_then_load_actual_state_uses_live_clickhouse(
    test_case: LoadActualStateWithoutMetadataIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    desired_state: DesiredState = realize_compiled_pipelines((compiled_pipeline,)).desired_state
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
                _replay_timestamp="2026-04-09 18:59:59.000",
                _replay_landed_at="2026-04-09 18:59:59.000",
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
                deployment_id="20260409T225000Z_ab12cd",
                created_at="2026-04-09 22:50:00.123",
                boundary_time="2026-04-09 22:50:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id="20260409T225000Z_ab12cd",
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        metadata_table_name: str
        for metadata_table_name in test_case.dropped_metadata_tables:
            clickhouse_client.command(f"DROP TABLE {clickhouse_database}.{metadata_table_name}")
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    assert (
        tuple(object_.name for object_ in actual_state.objects)
        == test_case.expected_actual_object_names
    )


RECONCILE_OVERRIDE_QUERY: str = (
    "SELECT CAST(kafka_key AS String) AS order_id, "
    "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp, "
    "CAST(kafka_topic AS String) AS kafka_topic FROM raw__orders"
)

ACTIVE_BASELINE_QUERY: str = (
    "SELECT CAST(kafka_key AS String) AS order_id, "
    "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp "
    "FROM raw__orders"
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        LoadActualStateWithLatestObjectStateIntegrationTestCase(
            description="reconcile object-state overrides active deployment query baseline",
            latest_record_deployment_id="manual_reconcile_observation",
            latest_record_state_kind="reconcile",
            latest_record_query=RECONCILE_OVERRIDE_QUERY,
            expected_materialized_view_query=RECONCILE_OVERRIDE_QUERY,
        ),
        LoadActualStateWithLatestObjectStateIntegrationTestCase(
            description=(
                "non-reconcile latest object-state does not override active deployment query "
                "baseline"
            ),
            latest_record_deployment_id=(
                f"{RECONCILE_DEPLOYMENT_ID_PREFIX}20260409T225500Z_newdep0"
            ),
            latest_record_state_kind="deployment",
            latest_record_query=RECONCILE_OVERRIDE_QUERY,
            expected_materialized_view_query=ACTIVE_BASELINE_QUERY,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_latest_object_state_record_when_loading_then_only_reconcile_overrides_active_query(
    test_case: LoadActualStateWithLatestObjectStateIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    desired_state: DesiredState = realize_compiled_pipelines((compiled_pipeline,)).desired_state
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
                _replay_timestamp="2026-04-09 18:59:59.000",
                _replay_landed_at="2026-04-09 18:59:59.000",
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
                deployment_id="20260409T225000Z_ab12cd",
                created_at="2026-04-09 22:50:00.123",
                boundary_time="2026-04-09 22:50:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id="20260409T225000Z_ab12cd",
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}._sqltide_virtual_object_state",
            data=[
                (
                    test_case.latest_record_deployment_id,
                    "observation-latest",
                    test_case.latest_record_state_kind,
                    None,
                    None,
                    "materialized_view",
                    "mv__orders_enriched",
                    None,
                    "mv__orders_enriched",
                    None,
                    "orders_enriched",
                    False,
                    "reconcile_fp",
                    test_case.latest_record_query,
                    "2099-04-09 22:55:00.123",
                )
            ],
            column_names=[
                "state_id",
                "observation_id",
                "state_kind",
                "deployment_id",
                "logical_database_name",
                "logical_object_type",
                "logical_object_name",
                "physical_database_name",
                "physical_relation_name",
                "logical_model_database",
                "logical_model_name",
                "is_selected_root",
                "object_fingerprint",
                "canonical_query",
                "observed_at",
            ],
        )
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    actual_object_by_name: dict[str, object] = {
        object_.name: object_ for object_ in actual_state.objects
    }
    actual_materialized_view_query: str = cast(
        ActualMaterializedView,
        actual_object_by_name["mv__orders_enriched"],
    ).query

    assert actual_materialized_view_query == test_case.expected_materialized_view_query
