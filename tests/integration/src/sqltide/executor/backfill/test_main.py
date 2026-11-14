from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_publish
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline, DesiredState
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.constants import (
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
)
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    RebuildSubtree,
)
from sqltide.compiler.planner.types import RebuildExecutionMode
from sqltide.executor.backfill.models import (
    BackfillBootstrapRequest,
    BackfillBootstrapResult,
    BackfillExecutionResult,
    RootBackfillReport,
)
from sqltide.executor.promotion.models import PublishRequest
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.models import WarehouseStatement
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
    render_create_view_ddl,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill._test_types import (
    BackfillAfterDeletedStagedTableIntegrationTestCase,
    BoundedPreservationMatrixScenarioResult,
    ExecuteAggregateBoundedOffsetReplayIntegrationTestCase,
    ExecuteAggregateOffsetReplayIntegrationTestCase,
    ExecuteBackfillBootstrapIntegrationTestCase,
    ExecuteBackfillOffsetReplayIntegrationTestCase,
    ExecuteBackfillScalarReplayIntegrationTestCase,
    ExecuteBoundedPreservationMatrixIntegrationTestCase,
    ExecuteBoundedReplayReportingIntegrationTestCase,
    ExecuteExternalSourceCursorReplayIntegrationTestCase,
    ExecuteExternalSourceOffsetReplayIntegrationTestCase,
    ExecuteMixedRootBackfillReportingIntegrationTestCase,
    ExecuteMultipleBackfillsIntegrationTestCase,
    ExecutePublishedReferenceJoinBackfillIntegrationTestCase,
    ExecuteReferenceJoinBackfillIntegrationTestCase,
    ExecuteRepeatedPublishedBackfillIntegrationTestCase,
    ExecuteSeededBoundedOffsetReplayIntegrationTestCase,
    ExecuteSeededBoundedScalarReplayIntegrationTestCase,
    ExecuteStartTimeReplayIntegrationTestCase,
    ExecuteUnseededBoundedOffsetReplayIntegrationTestCase,
    ExecuteUnseededBoundedScalarReplayIntegrationTestCase,
    ManagedSourceResources,
    MissingCursorStartTimeIntegrationTestCase,
    MissingOffsetReplayCutoffIntegrationTestCase,
    MissingScalarReplayCutoffIntegrationTestCase,
    NewSourcePostBoundaryArrivalIntegrationTestCase,
    PersistWatermarksWithoutMetadataTableIntegrationTestCase,
    ResolveAggregateUnsupportedReplayBehaviorIntegrationTestCase,
    StartTimeReplayScenarioResult,
)
from tests.integration.src.sqltide.executor.backfill.helpers import (
    assert_external_cursor_start_time_boundary,
    build_aggregate_offset_replay_compiled_pipeline,
    build_aggregate_offset_replay_request,
    build_backfill_bootstrap_request,
    build_changed_aggregate_offset_replay_compiled_pipeline,
    build_changed_offset_replay_compiled_pipeline,
    build_changed_scalar_replay_compiled_pipeline,
    build_compiled_pipeline,
    build_desired_state,
    build_external_source_aggregate_offset_replay_compiled_pipeline,
    build_external_source_aggregate_offset_replay_request,
    build_external_source_cursor_replay_compiled_pipeline,
    build_external_source_cursor_replay_request,
    build_external_source_offset_replay_compiled_pipeline,
    build_external_source_offset_replay_request,
    build_external_source_orders_row,
    build_named_scalar_replay_compiled_pipeline,
    build_offset_replay_compiled_pipeline,
    build_offset_replay_request,
    build_raw_orders_row,
    build_reference_join_compiled_pipeline,
    build_reference_join_region_lookup_only_compiled_pipeline,
    build_reference_join_region_lookup_only_replay_request,
    build_reference_join_replay_request,
    build_scalar_replay_compiled_pipeline,
    build_scalar_replay_request,
    execute_backfill,
    execute_backfill_bootstrap,
    prepare_live_landing_objects,
    require_managed_source,
    require_model_resources,
    run_bounded_preservation_matrix_scenario,
    run_start_time_replay_scenario,
)


class _InsertNewSourceRowBeforeAssertion:
    def __init__(self, *, client: Client, table: str, raw_row: tuple[object, ...]) -> None:
        self.client: Client = client
        self.table: str = table
        self.raw_row: tuple[object, ...] = raw_row
        self.inserted: bool = False

    def workflow_prepared(
        self, *, statements: tuple[WarehouseStatement, ...], workflow_sha256: str
    ) -> None:
        del statements, workflow_sha256

    def statement_started(self, statement: WarehouseStatement) -> str | None:
        if statement.step_id.startswith("assert_qualifying_input_") and not self.inserted:
            self.client.insert(
                table=self.table,
                data=[self.raw_row],
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
            self.inserted = True
        return None

    def statement_completed(
        self,
        *,
        statement: WarehouseStatement,
        error_message: str | None,
        written_rows: int | None,
        elapsed_ms: int,
    ) -> None:
        del statement, error_message, written_rows, elapsed_ms


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteBackfillBootstrapIntegrationTestCase(
            description="creates metadata tables persists deployment and creates shadow objects",
            deployment_id="20260409T120000Z_ab12cd",
            created_at="2026-04-09 12:00:00.123",
            precreate_live_landing_objects=True,
            expected_live_kafka_table_name="kafka__orders",
            expected_shadow_table_name="tbl__orders_enriched__20260409T120000Z_ab12cd",
            expected_shadow_materialized_view_name="mv__orders_enriched__20260409T120000Z_ab12cd",
            expected_deployment_status="backfilling",
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
                ("_sqltide_virtual_deployment_progress", "MergeTree"),
                ("_sqltide_virtual_deployments", "MergeTree"),
                ("_sqltide_virtual_object_state", "MergeTree"),
                ("_sqltide_virtual_publications", "MergeTree"),
                ("_sqltide_virtual_replay_boundaries", "MergeTree"),
                ("kafka__orders", "Kafka"),
                ("mv__orders", "MaterializedView"),
                ("mv__orders_enriched__20260409T120000Z_ab12cd", "MaterializedView"),
                ("raw__orders", "MergeTree"),
                ("tbl__orders_enriched__20260409T120000Z_ab12cd", "ReplacingMergeTree"),
            ),
        ),
        ExecuteBackfillBootstrapIntegrationTestCase(
            description=(
                "creates live kafka source table before staged landing objects in greenfield mode"
            ),
            deployment_id="20260409T120500Z_ef34gh",
            created_at="2026-04-09 12:05:00.123",
            precreate_live_landing_objects=False,
            expected_live_kafka_table_name="kafka__orders",
            expected_shadow_table_name="tbl__orders_enriched__20260409T120500Z_ef34gh",
            expected_shadow_materialized_view_name="mv__orders_enriched__20260409T120500Z_ef34gh",
            expected_deployment_status="backfilling",
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
                ("_sqltide_virtual_deployment_progress", "MergeTree"),
                ("_sqltide_virtual_deployments", "MergeTree"),
                ("_sqltide_virtual_object_state", "MergeTree"),
                ("_sqltide_virtual_publications", "MergeTree"),
                ("_sqltide_virtual_replay_boundaries", "MergeTree"),
                ("kafka__orders", "Kafka"),
                ("mv__orders", "MaterializedView"),
                ("mv__orders_enriched__20260409T120500Z_ef34gh", "MaterializedView"),
                ("raw__orders", "MergeTree"),
                ("tbl__orders_enriched__20260409T120500Z_ef34gh", "ReplacingMergeTree"),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_changed_pipeline_when_bootstrapping_then_it_creates_metadata_and_shadow_objects(
    test_case: ExecuteBackfillBootstrapIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_compiled_pipeline()
    prepare_live_landing_objects(
        precreate=test_case.precreate_live_landing_objects,
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
        execution_result: BackfillExecutionResult = execute_backfill(
            request=build_backfill_bootstrap_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    metadata_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT deployment_id, 'backfilling' FROM "
        f"{clickhouse_database}._sqltide_virtual_deployments"
    ).result_rows
    expected_object_names: tuple[str, str, str] = (
        test_case.expected_live_kafka_table_name,
        test_case.expected_shadow_table_name,
        test_case.expected_shadow_materialized_view_name,
    )
    created_object_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine FROM system.tables "
        f"WHERE database = '{clickhouse_database}' "
        f"AND name IN {expected_object_names} "
        "ORDER BY name"
    ).result_rows
    full_layout_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine FROM system.tables "
        f"WHERE database = '{clickhouse_database}' ORDER BY name"
    ).result_rows

    assert execution_result.bootstrap.deployment_id == test_case.deployment_id
    assert execution_result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert metadata_rows == [(test_case.deployment_id, test_case.expected_deployment_status)]
    assert created_object_rows == sorted(
        [
            (test_case.expected_live_kafka_table_name, "Kafka"),
            (test_case.expected_shadow_materialized_view_name, "MaterializedView"),
            (test_case.expected_shadow_table_name, "ReplacingMergeTree"),
        ],
        key=lambda row: str(row[0]),
    )
    assert full_layout_rows == list(test_case.expected_full_layout)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed offset seeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed offset unseeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed timestamp seeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed timestamp unseeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed landed-at seeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.LANDED_AT,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="managed landed-at unseeded bounded replay",
            source_ownership="managed",
            replay_lineage_mode=ReplayLineageMode.LANDED_AT,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted offset seeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted offset unseeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted timestamp seeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted timestamp unseeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted cursor seeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.CURSOR,
            requested_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(
                ("frontier-order", ""),
                ("historical-order", ""),
                ("tail-order", "changed"),
            ),
        ),
        ExecuteBoundedPreservationMatrixIntegrationTestCase(
            description="adopted cursor unseeded bounded replay",
            source_ownership="adopted",
            replay_lineage_mode=ReplayLineageMode.CURSOR,
            requested_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_shadow_rows=(("tail-order", "changed"),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_bounded_preservation_pair_when_replaying_then_it_preserves_seed_policy(
    test_case: ExecuteBoundedPreservationMatrixIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    result: BoundedPreservationMatrixScenarioResult = run_bounded_preservation_matrix_scenario(
        test_case=test_case,
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
    )

    assert result.execution_mode == test_case.expected_execution_mode
    assert result.shadow_rows == test_case.expected_shadow_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteBackfillScalarReplayIntegrationTestCase(
            description="replays historical rows for kafka timestamp mode and persists watermarks",
            replay_lineage_mode="timestamp",
            deployment_id="20260409T130000Z_ab12cd",
            created_at="2026-04-09 13:00:00.123",
            boundary_time="2026-04-09 13:00:00.000",
            expected_boundary_key="_replay_timestamp",
            expected_shadow_table_name="tbl__orders_enriched__20260409T130000Z_ab12cd",
            historical_raw_rows=(
                build_raw_orders_row(
                    kafka_key="historical-order",
                    _replay_partition=0,
                    _replay_offset=1,
                    _replay_timestamp="2026-04-09 12:59:59.000",
                    _replay_landed_at="2026-04-09 12:59:59.100",
                ),
            ),
            live_raw_rows=(
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=2,
                    _replay_timestamp="2026-04-09 13:00:01.000",
                    _replay_landed_at="2026-04-09 13:00:01.100",
                ),
            ),
            expected_shadow_order_ids=("historical-order", "live-order"),
            expected_replay_written_rows=(1,),
        ),
        ExecuteBackfillScalarReplayIntegrationTestCase(
            description="replays historical rows for kafka landed at mode and persists watermarks",
            replay_lineage_mode="landed_at",
            deployment_id="20260409T140000Z_ab12cd",
            created_at="2026-04-09 14:00:00.123",
            boundary_time="2026-04-09 14:00:00.000",
            expected_boundary_key="_replay_landed_at",
            expected_shadow_table_name="tbl__orders_enriched__20260409T140000Z_ab12cd",
            historical_raw_rows=(
                build_raw_orders_row(
                    kafka_key="historical-order",
                    _replay_partition=0,
                    _replay_offset=1,
                    _replay_timestamp="2026-04-09 13:59:58.000",
                    _replay_landed_at="2026-04-09 13:59:59.000",
                ),
            ),
            live_raw_rows=(
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=2,
                    _replay_timestamp="2026-04-09 14:00:01.000",
                    _replay_landed_at="2026-04-09 14:00:01.000",
                ),
            ),
            expected_shadow_order_ids=("historical-order", "live-order"),
            expected_replay_written_rows=(1,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_scalar_replay_mode_when_executing_then_it_persists_watermarks_and_replays_rows(
    test_case: ExecuteBackfillScalarReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline(
        test_case.replay_lineage_mode
    )
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
        data=list(test_case.historical_raw_rows),
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode=test_case.replay_lineage_mode,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=list(test_case.live_raw_rows),
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
    finally:
        managed_client.close()

    watermark_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT concat('_replay_', boundary_kind), cutoff_value FROM "
        f"{clickhouse_database}._sqltide_virtual_replay_boundaries"
    ).result_rows
    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} "
        "ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert result.boundary_time == test_case.boundary_time
    assert result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert set(watermark_rows) == {(test_case.expected_boundary_key, test_case.boundary_time)}
    assert shadow_rows == [(order_id,) for order_id in test_case.expected_shadow_order_ids]
    assert tuple(replay.written_rows for replay in result.replay_results) == (
        test_case.expected_replay_written_rows
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingOffsetReplayCutoffIntegrationTestCase(
            description="rejects retained offset input entirely newer than the warehouse cutoff",
            boundary_time="2026-04-09 15:00:00.000",
            raw_rows=(
                build_raw_orders_row(
                    kafka_key="future-order",
                    _replay_partition=0,
                    _replay_offset=1,
                    _replay_timestamp="2026-04-10 15:00:01.000",
                    _replay_landed_at="2026-04-10 15:00:01.000",
                ),
            ),
            expected_error_fragment="has rows but no qualifying offsets cutoff",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_nonempty_offset_input_without_cutoff_when_replaying_then_population_fails(
    test_case: MissingOffsetReplayCutoffIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
    source_resources: ManagedSourceResources = require_managed_source(compiled_pipeline)
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=source_resources.kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=source_resources.raw_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=source_resources.materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{source_resources.raw_table.name}",
        data=list(test_case.raw_rows),
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
        with pytest.raises(WorkflowExecutionError, match=test_case.expected_error_fragment):
            execute_backfill(
                request=build_offset_replay_request(
                    database=clickhouse_database,
                    deployment_id="20260409T152000Z_missing",
                    created_at="2026-04-09 15:20:00.123",
                    boundary_time=test_case.boundary_time,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        NewSourcePostBoundaryArrivalIntegrationTestCase(
            description="continues when a new source receives its first row after the boundary",
            deployment_id="20260409T152000Z_startup",
            created_at="2026-04-09 15:20:00.123",
            boundary_time="2026-04-09 15:00:00.000",
            raw_row=build_raw_orders_row(
                kafka_key="arrived-after-boundary",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-10 15:00:01.000",
                _replay_landed_at="2026-04-10 15:00:01.000",
            ),
            expected_order_id="arrived-after-boundary",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_new_source_input_arrives_after_boundary_when_replaying_then_population_continues(
    test_case: NewSourcePostBoundaryArrivalIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
    source_resources: ManagedSourceResources = require_managed_source(compiled_pipeline)
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    emitter: _InsertNewSourceRowBeforeAssertion = _InsertNewSourceRowBeforeAssertion(
        client=clickhouse_client,
        table=f"{clickhouse_database}.{source_resources.raw_table.name}",
        raw_row=test_case.raw_row,
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
            emitter=emitter,
        )
    finally:
        managed_client.close()

    staged_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {clickhouse_database}.{target_table_name}__"
        f"{test_case.deployment_id} "
        "ORDER BY order_id"
    ).result_rows

    assert emitter.inserted
    assert tuple(replay.written_rows for replay in result.replay_results) == (0,)
    assert staged_rows == [(test_case.expected_order_id,)]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingScalarReplayCutoffIntegrationTestCase(
            description="rejects retained timestamp input entirely newer than the warehouse cutoff",
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            boundary_time="2026-04-09 16:00:00.000",
            raw_rows=(
                build_raw_orders_row(
                    kafka_key="future-timestamp-order",
                    _replay_partition=0,
                    _replay_offset=1,
                    _replay_timestamp="2026-04-10 16:00:01.000",
                    _replay_landed_at="2026-04-10 16:00:01.000",
                ),
            ),
            expected_error_fragment="has rows but no qualifying timestamp cutoff",
        ),
        MissingScalarReplayCutoffIntegrationTestCase(
            description="rejects retained landed-at input entirely newer than the warehouse cutoff",
            replay_lineage_mode=ReplayLineageMode.LANDED_AT,
            boundary_time="2026-04-09 16:00:00.000",
            raw_rows=(
                build_raw_orders_row(
                    kafka_key="future-landed-order",
                    _replay_partition=0,
                    _replay_offset=1,
                    _replay_timestamp="2026-04-10 16:00:01.000",
                    _replay_landed_at="2026-04-10 16:00:01.000",
                ),
            ),
            expected_error_fragment="has rows but no qualifying landed_at cutoff",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_nonempty_scalar_input_without_cutoff_when_replaying_then_population_fails(
    test_case: MissingScalarReplayCutoffIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline(
        test_case.replay_lineage_mode
    )
    source_resources: ManagedSourceResources = require_managed_source(compiled_pipeline)
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=source_resources.kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=source_resources.raw_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=source_resources.materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{source_resources.raw_table.name}",
        data=list(test_case.raw_rows),
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
        with pytest.raises(WorkflowExecutionError, match=test_case.expected_error_fragment):
            execute_backfill(
                request=build_scalar_replay_request(
                    database=clickhouse_database,
                    deployment_id="20260409T162000Z_missing",
                    created_at="2026-04-09 16:20:00.123",
                    boundary_time=test_case.boundary_time,
                    replay_lineage_mode=test_case.replay_lineage_mode,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteBackfillOffsetReplayIntegrationTestCase(
            description="replays kafka offsets history and persists partition watermarks",
            deployment_id="20260409T150000Z_ab12cd",
            created_at="2026-04-09 15:00:00.123",
            boundary_time="2026-04-09 15:00:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T150000Z_ab12cd",
            raw_rows=(
                build_raw_orders_row(
                    kafka_key="historical-partition-0",
                    _replay_partition=0,
                    _replay_offset=10,
                    _replay_timestamp="2026-04-09 14:59:58.000",
                    _replay_landed_at="2026-04-09 14:59:59.000",
                ),
                build_raw_orders_row(
                    kafka_key="historical-partition-1",
                    _replay_partition=1,
                    _replay_offset=20,
                    _replay_timestamp="2026-04-09 14:59:58.500",
                    _replay_landed_at="2026-04-09 14:59:59.500",
                ),
                build_raw_orders_row(
                    kafka_key="future-partition-0",
                    _replay_partition=0,
                    _replay_offset=11,
                    _replay_timestamp="2026-04-10 15:00:01.000",
                    _replay_landed_at="2026-04-10 15:00:01.000",
                ),
            ),
            live_raw_rows=(
                build_raw_orders_row(
                    kafka_key="live-partition-0",
                    _replay_partition=0,
                    _replay_offset=12,
                    _replay_timestamp="2026-04-09 15:00:02.000",
                    _replay_landed_at="2026-04-09 15:00:02.000",
                ),
            ),
            expected_watermark_rows=(
                ("_replay_partition=0", "10"),
                ("_replay_partition=1", "20"),
            ),
            expected_shadow_order_ids=(
                "historical-partition-0",
                "historical-partition-1",
                "live-partition-0",
            ),
            expected_replay_written_rows=(2,),
        ),
        ExecuteBackfillOffsetReplayIntegrationTestCase(
            description="treats empty offset cutoffs as a no-op instead of failing",
            deployment_id="20260409T151000Z_cd34ef",
            created_at="2026-04-09 15:10:00.123",
            boundary_time="2026-04-09 15:10:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T151000Z_cd34ef",
            raw_rows=(),
            live_raw_rows=(),
            expected_watermark_rows=(),
            expected_shadow_order_ids=(),
            expected_replay_written_rows=(0,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_offset_replay_mode_when_executing_then_it_persists_partition_watermarks(
    test_case: ExecuteBackfillOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
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
        data=list(test_case.raw_rows),
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=list(test_case.live_raw_rows),
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
    finally:
        managed_client.close()

    watermark_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT concat('_replay_partition=', partition_value), cutoff_value FROM "
        f"{clickhouse_database}._sqltide_virtual_replay_boundaries "
        "ORDER BY partition_value"
    ).result_rows
    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} "
        "ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert result.boundary_time == test_case.boundary_time
    assert result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert watermark_rows == list(test_case.expected_watermark_rows)
    assert shadow_rows == [(order_id,) for order_id in test_case.expected_shadow_order_ids]
    assert tuple(replay.written_rows for replay in result.replay_results) == (
        test_case.expected_replay_written_rows
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteReferenceJoinBackfillIntegrationTestCase(
            description="executes backfill for staged reference joins across managed targets",
            deployment_id="20260419T140000Z_ref123",
            created_at="2026-04-19 14:00:00.123",
            boundary_time="2026-04-19 14:00:00.000",
            expected_region_lookup_shadow_name="tbl__region_lookup__20260419T140000Z_ref123",
            expected_enriched_shadow_name="tbl__enriched_orders__20260419T140000Z_ref123",
            expected_enriched_rows=(
                ("north", "NORTH"),
                ("south", "SOUTH"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reference_join_when_executing_then_it_replays_from_staged_reference_tables(
    test_case: ExecuteReferenceJoinBackfillIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_reference_join_compiled_pipeline()
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
                kafka_key="north",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-19 13:59:58.000",
                _replay_landed_at="2026-04-19 13:59:58.000",
            ),
            build_raw_orders_row(
                kafka_key="south",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-19 13:59:59.000",
                _replay_landed_at="2026-04-19 13:59:59.000",
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_reference_join_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    region_lookup_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT region, region_display FROM "
        f"{clickhouse_database}.{test_case.expected_region_lookup_shadow_name} ORDER BY region"
    ).result_rows
    enriched_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, region_display FROM "
        f"{clickhouse_database}.{test_case.expected_enriched_shadow_name} ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert region_lookup_rows == [("north", "NORTH"), ("south", "SOUTH")]
    assert enriched_rows == list(test_case.expected_enriched_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecutePublishedReferenceJoinBackfillIntegrationTestCase(
            description="replays reference joins against an already-published active managed ref",
            initial_deployment_id="20260419T141000Z_refabc",
            changed_deployment_id="20260419T141500Z_refdef",
            created_at="2026-04-19 14:10:00.123",
            boundary_time="2026-04-19 14:10:00.000",
            expected_enriched_shadow_name="tbl__enriched_orders__20260419T141500Z_refdef",
            expected_enriched_rows=(
                ("north", "NORTH"),
                ("south", "SOUTH"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_reference_join_dependency_when_backfilling_then_it_uses_active_logical_ref(
    test_case: ExecutePublishedReferenceJoinBackfillIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    initial_compiled_pipeline: CompiledPipeline = (
        build_reference_join_region_lookup_only_compiled_pipeline()
    )
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(initial_compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(initial_compiled_pipeline).raw_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(initial_compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(initial_compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="north",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-19 14:09:58.000",
                _replay_landed_at="2026-04-19 14:09:58.000",
            ),
            build_raw_orders_row(
                kafka_key="south",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-19 14:09:59.000",
                _replay_landed_at="2026-04-19 14:09:59.000",
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
            request=build_reference_join_region_lookup_only_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.initial_deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        result: BackfillExecutionResult = execute_backfill(
            request=build_reference_join_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    active_region_lookup_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT region, region_display FROM "
        f"{clickhouse_database}.tbl__region_lookup ORDER BY region"
    ).result_rows
    enriched_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, region_display FROM "
        f"{clickhouse_database}.{test_case.expected_enriched_shadow_name} ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.changed_deployment_id
    assert active_region_lookup_rows == [("north", "NORTH"), ("south", "SOUTH")]
    assert enriched_rows == list(test_case.expected_enriched_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteExternalSourceOffsetReplayIntegrationTestCase(
            description=(
                "executes offset replay from an adopted external source table with aliased columns"
            ),
            deployment_id="20260409T151500Z_xy12za",
            created_at="2026-04-09 15:15:00.123",
            boundary_time="2026-04-09 15:15:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T151500Z_xy12za",
            source_rows=(
                build_external_source_orders_row(
                    order_id="historical-partition-0",
                    event_partition=0,
                    event_offset=10,
                    event_timestamp="2026-04-09 14:59:58.000",
                    event_landed_at="2026-04-09 14:59:59.000",
                ),
                build_external_source_orders_row(
                    order_id="historical-partition-1",
                    event_partition=1,
                    event_offset=20,
                    event_timestamp="2026-04-09 14:59:58.500",
                    event_landed_at="2026-04-09 14:59:59.500",
                ),
                build_external_source_orders_row(
                    order_id="future-partition-0",
                    event_partition=0,
                    event_offset=11,
                    event_timestamp="2026-04-10 15:00:01.000",
                    event_landed_at="2026-04-10 15:00:01.000",
                ),
            ),
            expected_watermark_rows=(
                ("_replay_partition=0", "10"),
                ("_replay_partition=1", "20"),
            ),
            expected_shadow_order_ids=("historical-partition-0", "historical-partition-1"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_external_source_offset_replay_when_executing_then_it_uses_declared_boundary_columns(
    test_case: ExecuteExternalSourceOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_external_source_offset_replay_compiled_pipeline()
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing ("
        "order_id String, "
        "event_partition Int64, "
        "event_offset Int64, "
        "event_timestamp DateTime64(3), "
        "event_landed_at DateTime64(3)"
        ") ENGINE = MergeTree() ORDER BY (order_id)"
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.orders_existing",
        data=list(test_case.source_rows),
        column_names=[
            "order_id",
            "event_partition",
            "event_offset",
            "event_timestamp",
            "event_landed_at",
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_external_source_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    watermark_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT concat('_replay_partition=', partition_value), cutoff_value FROM "
        f"{clickhouse_database}._sqltide_virtual_replay_boundaries "
        "ORDER BY partition_value"
    ).result_rows
    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id FROM "
        f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table_name}__"
        f"{test_case.deployment_id} "
        "ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert result.boundary_time == test_case.boundary_time
    assert result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert watermark_rows == list(test_case.expected_watermark_rows)
    assert shadow_rows == [(order_id,) for order_id in test_case.expected_shadow_order_ids]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingCursorStartTimeIntegrationTestCase(
            description="rejects adopted cursor input entirely before the forced start time",
            deployment_id="20260409T161000Z_no_cursor_frontier",
            created_at="2026-04-09 16:10:00.123",
            start_time="2026-04-10 16:00:00.000",
            expected_error_fragment="has rows but no qualifying cursor cutoff",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cursor_input_before_forced_start_when_replaying_then_population_fails(
    test_case: MissingCursorStartTimeIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing ("
        "order_id String, event_cursor UInt64, event_timestamp DateTime64(3)"
        ") ENGINE = MergeTree() ORDER BY (order_id)"
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.orders_existing VALUES "
        "('cursor-order-1', 1, toDateTime64('2026-04-09 16:00:01.000', 3)), "
        "('cursor-order-2', 2, toDateTime64('2026-04-09 16:00:02.000', 3))"
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
        with pytest.raises(WorkflowExecutionError, match=test_case.expected_error_fragment):
            execute_backfill(
                request=build_external_source_cursor_replay_request(
                    database=clickhouse_database,
                    deployment_id=test_case.deployment_id,
                    created_at=test_case.created_at,
                    start_time=test_case.start_time,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteExternalSourceCursorReplayIntegrationTestCase(
            description="executes cursor replay from an adopted external stream table",
            deployment_id="20260409T160000Z_cd34ef",
            created_at="2026-04-09 16:00:00.123",
            start_time=None,
            expected_shadow_order_ids=("cursor-order-1", "cursor-order-2", "cursor-order-3"),
            expected_cutoff_value="3",
        ),
        ExecuteExternalSourceCursorReplayIntegrationTestCase(
            description="executes cursor replay from a requested start time",
            deployment_id="20260409T160500Z_de45fg",
            created_at="2026-04-09 16:05:00.123",
            start_time="2026-04-09 16:00:02.000",
            expected_shadow_order_ids=("cursor-order-2", "cursor-order-3"),
            expected_cutoff_value="3",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_external_source_cursor_replay_when_executing_then_it_replays_by_cursor(
    test_case: ExecuteExternalSourceCursorReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_external_source_cursor_replay_compiled_pipeline()
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing ("
        "order_id String, "
        "event_cursor UInt64, "
        "event_timestamp DateTime64(3)"
        ") ENGINE = MergeTree() ORDER BY (order_id)"
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.orders_existing VALUES "
        "('cursor-order-1', 1, toDateTime64('2026-04-09 16:00:01.000', 3)), "
        "('cursor-order-2', 2, toDateTime64('2026-04-09 16:00:02.000', 3)), "
        "('cursor-order-3', 3, toDateTime64('2026-04-09 16:00:03.000', 3))"
    )
    assert_external_cursor_start_time_boundary(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        start_time=test_case.start_time,
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_external_source_cursor_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                start_time=test_case.start_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    watermark_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT concat('_replay_', boundary_kind), cutoff_value FROM "
        f"{clickhouse_database}._sqltide_virtual_replay_boundaries "
        "ORDER BY boundary_kind"
    ).result_rows
    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id FROM "
        f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table_name}__"
        f"{test_case.deployment_id} ORDER BY order_id"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert (
        result.bootstrap.deployment_plan.rebuild_subtrees[0].forced_start_time
        == test_case.start_time
    )
    assert set(watermark_rows) == {("_replay_cursor", test_case.expected_cutoff_value)}
    assert shadow_rows == [(order_id,) for order_id in test_case.expected_shadow_order_ids]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteAggregateOffsetReplayIntegrationTestCase(
            description="executes aggregate offset replay by filtering anchor rows before group by",
            deployment_id="20260409T152000Z_ef56gh",
            created_at="2026-04-09 15:20:00.123",
            boundary_time="2026-04-09 15:20:00.000",
            expected_shadow_table_name="tbl__hourly_order_volume__20260409T152000Z_ef56gh",
            raw_rows=(
                build_raw_orders_row(
                    kafka_key="historical-partition-0",
                    _replay_partition=0,
                    _replay_offset=10,
                    _replay_timestamp="2026-04-09 14:59:58.000",
                    _replay_landed_at="2026-04-09 14:59:59.000",
                ),
                build_raw_orders_row(
                    kafka_key="historical-partition-1",
                    _replay_partition=1,
                    _replay_offset=20,
                    _replay_timestamp="2026-04-09 14:59:58.500",
                    _replay_landed_at="2026-04-09 14:59:59.500",
                ),
                build_raw_orders_row(
                    kafka_key="future-partition-0",
                    _replay_partition=0,
                    _replay_offset=11,
                    _replay_timestamp="2026-04-10 15:00:01.000",
                    _replay_landed_at="2026-04-10 15:00:01.000",
                ),
            ),
            expected_shadow_rows=(("2026-04-09 14:00:00.000", 2),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_aggregate_offset_replay_when_executing_then_it_filters_anchor_rows_before_group_by(
    test_case: ExecuteAggregateOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_aggregate_offset_replay_compiled_pipeline()
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
        data=list(test_case.raw_rows),
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_aggregate_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT toString(event_hour), order_event_count FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} "
        "ORDER BY event_hour"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteAggregateOffsetReplayIntegrationTestCase(
            description=("executes adopted aggregate offset replay with physical boundary columns"),
            deployment_id="20260409T152500Z_fg67hi",
            created_at="2026-04-09 15:25:00.123",
            boundary_time="2026-04-09 15:25:00.000",
            expected_shadow_table_name="tbl__hourly_order_volume__20260409T152500Z_fg67hi",
            raw_rows=(
                build_external_source_orders_row(
                    order_id="historical-partition-0",
                    event_partition=0,
                    event_offset=10,
                    event_timestamp="2026-04-09 14:59:58.000",
                    event_landed_at="2026-04-09 14:59:59.000",
                ),
                build_external_source_orders_row(
                    order_id="historical-partition-1",
                    event_partition=1,
                    event_offset=20,
                    event_timestamp="2026-04-09 14:59:58.500",
                    event_landed_at="2026-04-09 14:59:59.500",
                ),
                build_external_source_orders_row(
                    order_id="future-partition-0",
                    event_partition=0,
                    event_offset=11,
                    event_timestamp="2026-04-10 15:00:01.000",
                    event_landed_at="2026-04-10 15:00:01.000",
                ),
            ),
            expected_shadow_rows=(("2026-04-09 14:00:00.000", 2),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adopted_aggregate_offset_replay_when_executing_then_it_uses_physical_columns(
    test_case: ExecuteAggregateOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = (
        build_external_source_aggregate_offset_replay_compiled_pipeline()
    )
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing ("
        "order_id String, "
        "event_partition Int64, "
        "event_offset Int64, "
        "event_timestamp DateTime64(3), "
        "event_landed_at DateTime64(3)"
        ") ENGINE = MergeTree() ORDER BY (order_id)"
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.orders_existing",
        data=list(test_case.raw_rows),
        column_names=[
            "order_id",
            "event_partition",
            "event_offset",
            "event_timestamp",
            "event_landed_at",
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
        result: BackfillExecutionResult = execute_backfill(
            request=build_external_source_aggregate_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT toString(event_hour), order_event_count FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} "
        "ORDER BY event_hour"
    ).result_rows

    assert result.bootstrap.deployment_id == test_case.deployment_id
    assert (
        require_model_resources(compiled_pipeline).target_table_name == "tbl__hourly_order_volume"
    )
    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ResolveAggregateUnsupportedReplayBehaviorIntegrationTestCase(
            description="full policy resolves unsupported aggregate start time to full rebuild",
            initial_deployment_id="20260409T160000Z_ab12cd",
            changed_deployment_id="20260409T160500Z_cd34ef",
            created_at="2026-04-09 16:00:00.123",
            initial_boundary_time="2026-04-09 15:10:00.000",
            changed_boundary_time="2026-04-09 15:15:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=1,
            bounded_replay_fallback="full",
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        ResolveAggregateUnsupportedReplayBehaviorIntegrationTestCase(
            description=(
                "window only policy resolves unsupported aggregate start time to unseeded bounded"
            ),
            initial_deployment_id="20260409T161000Z_ab12cd",
            changed_deployment_id="20260409T161500Z_cd34ef",
            created_at="2026-04-09 16:10:00.123",
            initial_boundary_time="2026-04-09 15:10:00.000",
            changed_boundary_time="2026-04-09 15:15:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=1,
            bounded_replay_fallback="bounded_without_history",
            expected_execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_aggregate_start_time_when_bootstrapping_then_it_resolves_policy_per_root(
    test_case: ResolveAggregateUnsupportedReplayBehaviorIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_aggregate_offset_replay_compiled_pipeline()
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
                _replay_offset=10,
                _replay_timestamp="2026-04-09 15:09:58.000",
                _replay_landed_at="2026-04-09 15:09:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=11,
                _replay_timestamp="2026-04-09 15:09:59.000",
                _replay_landed_at="2026-04-09 15:09:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_aggregate_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (
                build_changed_aggregate_offset_replay_compiled_pipeline(
                    bounded_replay_fallback=test_case.bounded_replay_fallback
                ),
            )
        )
        frontier_timestamp: datetime = clickhouse_client.query(
            "SELECT max(_replay_landed_at) FROM "
            f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name} "
            f"WHERE kafka_key = '{test_case.lower_bound_source_order_id}'"
        ).result_rows[0][0]
        converted_start_time: str = (
            frontier_timestamp - timedelta(milliseconds=test_case.lower_bound_offset_millis)
        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        bootstrap_result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                start_time_keys=frozenset(
                    {require_model_resources(compiled_pipeline).target_table.key}
                ),
                start_time=converted_start_time,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    subtree: RebuildSubtree = bootstrap_result.deployment_plan.rebuild_subtrees[0]

    assert subtree.execution_mode == test_case.expected_execution_mode
    assert not subtree.history_preserving_bounded_supported
    assert subtree.resolved_bounded_replay_fallback == test_case.bounded_replay_fallback


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteAggregateBoundedOffsetReplayIntegrationTestCase(
            description=(
                "aggregate start time falls back to full rebuild by default when "
                "history-preserving replay is unsupported"
            ),
            initial_deployment_id="20260409T153000Z_ab12cd",
            changed_deployment_id="20260409T153500Z_cd34ef",
            created_at="2026-04-09 15:30:00.123",
            initial_boundary_time="2026-04-09 15:10:00.000",
            changed_boundary_time="2026-04-09 15:15:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=1,
            expected_shadow_table_name="tbl__hourly_order_volume__20260409T153500Z_cd34ef",
            expected_shadow_rows=(("2026-04-09 15:00:00.000", 2),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_aggregate_start_time_when_executing_then_it_falls_back_to_full_rebuild(
    test_case: ExecuteAggregateBoundedOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_aggregate_offset_replay_compiled_pipeline()
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
                _replay_offset=10,
                _replay_timestamp="2026-04-09 15:09:58.000",
                _replay_landed_at="2026-04-09 15:09:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=11,
                _replay_timestamp="2026-04-09 15:09:59.000",
                _replay_landed_at="2026-04-09 15:09:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_aggregate_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_aggregate_offset_replay_compiled_pipeline(),)
        )
        frontier_timestamp: datetime = clickhouse_client.query(
            "SELECT max(_replay_landed_at) FROM "
            f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name} "
            f"WHERE kafka_key = '{test_case.lower_bound_source_order_id}'"
        ).result_rows[0][0]
        converted_start_time: str = (
            frontier_timestamp - timedelta(milliseconds=test_case.lower_bound_offset_millis)
        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        result: BackfillExecutionResult = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                start_time_keys=frozenset(
                    {require_model_resources(compiled_pipeline).target_table.key}
                ),
                start_time=converted_start_time,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT toString(event_hour), order_event_count FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY event_hour"
    ).result_rows

    assert (
        result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode
        == REBUILD_EXECUTION_MODE_FULL
    )
    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteAggregateBoundedOffsetReplayIntegrationTestCase(
            description=(
                "unseeded bounded aggregate offset replay skips prefix copy and replays only "
                "the tail"
            ),
            initial_deployment_id="20260409T154000Z_ab12cd",
            changed_deployment_id="20260409T154500Z_cd34ef",
            created_at="2026-04-09 15:40:00.123",
            initial_boundary_time="2026-04-09 16:30:00.000",
            changed_boundary_time="2026-04-09 16:30:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=1,
            expected_shadow_table_name="tbl__hourly_order_volume__20260409T154500Z_cd34ef",
            expected_shadow_rows=(
                ("2026-04-09 16:00:00.000", 1),
                ("2026-04-09 16:00:00.000", 1),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unseeded_bounded_aggregate_offset_replay_when_executing_then_it_replays_tail(
    test_case: ExecuteAggregateBoundedOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_aggregate_offset_replay_compiled_pipeline()
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
                _replay_offset=10,
                _replay_timestamp="2026-04-09 16:28:58.000",
                _replay_landed_at="2026-04-09 16:28:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=11,
                _replay_timestamp="2026-04-09 16:29:59.000",
                _replay_landed_at="2026-04-09 16:29:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_aggregate_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_aggregate_offset_replay_compiled_pipeline(),)
        )
        bootstrap_result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        unseeded_plan: DeploymentPlan = replace(
            bootstrap_result.deployment_plan,
            rebuild_subtrees=tuple(
                replace(
                    subtree,
                    execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
                    configured_backfill_mode="bounded",
                    forced_start_time=(
                        datetime.strptime(test_case.changed_boundary_time, "%Y-%m-%d %H:%M:%S.%f")
                        - timedelta(milliseconds=test_case.lower_bound_offset_millis)
                    ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    execution_lookback_seconds=None,
                )
                for subtree in bootstrap_result.deployment_plan.rebuild_subtrees
            ),
        )
        _ = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                confirmed_plan=unseeded_plan,
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=12,
                    _replay_timestamp="2026-04-09 16:30:01.000",
                    _replay_landed_at="2026-04-09 16:30:01.000",
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
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT toString(event_hour), order_event_count FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY event_hour"
    ).result_rows

    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteSeededBoundedScalarReplayIntegrationTestCase(
            description="seeded bounded scalar replay preserves prefix and recomputes the tail",
            initial_deployment_id="20260409T160000Z_ab12cd",
            changed_deployment_id="20260409T160500Z_cd34ef",
            created_at="2026-04-09 16:00:00.123",
            initial_boundary_time="2026-04-09 16:00:00.000",
            changed_boundary_time="2026-04-09 16:05:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T160500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", ""),
                ("live-order", "source.orders.created"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_seeded_bounded_scalar_replay_when_executing_then_it_copies_prefix_and_replays_tail(
    test_case: ExecuteSeededBoundedScalarReplayIntegrationTestCase,
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
                _replay_timestamp="2026-04-09 15:59:58.000",
                _replay_landed_at="2026-04-09 15:59:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 15:59:59.000",
                _replay_landed_at="2026-04-09 15:59:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_scalar_replay_compiled_pipeline("timestamp"),)
        )
        seeded_result: BackfillExecutionResult = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="timestamp",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=3,
                    _replay_timestamp="2026-04-09 16:05:01.000",
                    _replay_landed_at="2026-04-09 16:05:01.000",
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
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, kafka_topic FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY order_id"
    ).result_rows

    assert seeded_result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode == (
        "seeded_bounded_rebuild"
    )
    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteStartTimeReplayIntegrationTestCase(
            description="start time scalar replay keeps prefix and replays the explicit tail",
            replay_lineage_mode="timestamp",
            initial_deployment_id="20260409T170000Z_ab12cd",
            changed_deployment_id="20260409T170500Z_cd34ef",
            created_at="2026-04-09 17:00:00.123",
            initial_boundary_time="2026-04-09 17:00:00.000",
            changed_boundary_time="2026-04-09 17:05:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=500,
            expected_shadow_table_name="tbl__orders_enriched__20260409T170500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", ""),
                ("live-order", "source.orders.created"),
            ),
        ),
        ExecuteStartTimeReplayIntegrationTestCase(
            description="start time scalar replay before history replays the full available window",
            replay_lineage_mode="timestamp",
            initial_deployment_id="20260409T170000Z_ab12cd",
            changed_deployment_id="20260409T170500Z_ef56gh",
            created_at="2026-04-09 17:00:00.123",
            initial_boundary_time="2026-04-09 17:00:00.000",
            changed_boundary_time="2026-04-09 17:05:00.000",
            lower_bound_source_order_id="historical-order",
            lower_bound_offset_millis=500,
            expected_shadow_table_name="tbl__orders_enriched__20260409T170500Z_ef56gh",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", "source.orders.created"),
                ("live-order", "source.orders.created"),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_start_time_scalar_replay_when_executing_then_it_replays_expected_rows(
    test_case: ExecuteStartTimeReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    scenario_result: StartTimeReplayScenarioResult = run_start_time_replay_scenario(
        test_case=test_case,
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
    )

    assert scenario_result.start_time_result.bootstrap.deployment_plan.rebuild_subtrees[
        0
    ].forced_start_time == (scenario_result.converted_start_time)
    assert scenario_result.start_time_result.bootstrap.deployment_plan.rebuild_subtrees[
        0
    ].execution_mode == ("seeded_bounded_rebuild")
    assert scenario_result.shadow_rows == test_case.expected_shadow_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteUnseededBoundedScalarReplayIntegrationTestCase(
            description=(
                "unseeded bounded scalar replay skips prefix copy and replays only the tail"
            ),
            initial_deployment_id="20260409T162000Z_ab12cd",
            changed_deployment_id="20260409T162500Z_cd34ef",
            created_at="2026-04-09 16:20:00.123",
            initial_boundary_time="2026-04-09 16:20:00.000",
            changed_boundary_time="2026-04-09 16:25:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T162500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("live-order", "source.orders.created"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unseeded_bounded_scalar_replay_when_executing_then_it_replays_only_the_tail(
    test_case: ExecuteUnseededBoundedScalarReplayIntegrationTestCase,
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
                _replay_timestamp="2026-04-09 16:19:58.000",
                _replay_landed_at="2026-04-09 16:19:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 16:19:59.000",
                _replay_landed_at="2026-04-09 16:19:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_scalar_replay_compiled_pipeline("timestamp"),)
        )
        bootstrap_result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="timestamp",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        unseeded_plan: DeploymentPlan = replace(
            bootstrap_result.deployment_plan,
            rebuild_subtrees=tuple(
                replace(subtree, execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED)
                for subtree in bootstrap_result.deployment_plan.rebuild_subtrees
            ),
        )
        _ = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="timestamp",
                confirmed_plan=unseeded_plan,
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=3,
                    _replay_timestamp="2026-04-09 16:25:01.000",
                    _replay_landed_at="2026-04-09 16:25:01.000",
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
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, kafka_topic FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY order_id"
    ).result_rows

    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteUnseededBoundedOffsetReplayIntegrationTestCase(
            description=(
                "unseeded bounded offset replay skips prefix copy and replays only the tail"
            ),
            initial_deployment_id="20260409T163000Z_ab12cd",
            changed_deployment_id="20260409T163500Z_cd34ef",
            created_at="2026-04-09 16:30:00.123",
            initial_boundary_time="2026-04-09 16:30:00.000",
            changed_boundary_time="2026-04-09 16:35:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T163500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("live-order", "source.orders.created"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unseeded_bounded_offset_replay_when_executing_then_it_replays_only_the_tail(
    test_case: ExecuteUnseededBoundedOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
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
                _replay_offset=10,
                _replay_timestamp="2026-04-09 16:29:58.000",
                _replay_landed_at="2026-04-09 16:29:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=11,
                _replay_timestamp="2026-04-09 16:29:59.000",
                _replay_landed_at="2026-04-09 16:29:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_offset_replay_compiled_pipeline(),)
        )
        bootstrap_result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        unseeded_plan: DeploymentPlan = replace(
            bootstrap_result.deployment_plan,
            rebuild_subtrees=tuple(
                replace(subtree, execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED)
                for subtree in bootstrap_result.deployment_plan.rebuild_subtrees
            ),
        )
        _ = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                confirmed_plan=unseeded_plan,
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=12,
                    _replay_timestamp="2026-04-09 16:35:01.000",
                    _replay_landed_at="2026-04-09 16:35:01.000",
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
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, kafka_topic FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY order_id"
    ).result_rows

    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteSeededBoundedOffsetReplayIntegrationTestCase(
            description="seeded bounded offset replay preserves prefix and recomputes the tail",
            initial_deployment_id="20260409T161000Z_ab12cd",
            changed_deployment_id="20260409T161500Z_cd34ef",
            created_at="2026-04-09 16:10:00.123",
            initial_boundary_time="2026-04-09 16:10:00.000",
            changed_boundary_time="2026-04-09 16:15:00.000",
            expected_shadow_table_name="tbl__orders_enriched__20260409T161500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", ""),
                ("live-order", "source.orders.created"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_seeded_bounded_offset_replay_when_executing_then_it_copies_prefix_and_replays_tail(
    test_case: ExecuteSeededBoundedOffsetReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
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
                _replay_offset=10,
                _replay_timestamp="2026-04-09 16:09:58.000",
                _replay_landed_at="2026-04-09 16:09:58.000",
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=11,
                _replay_timestamp="2026-04-09 16:09:59.000",
                _replay_landed_at="2026-04-09 16:09:59.000",
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
        initial_result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_desired_state: DesiredState = build_desired_state(
            (build_changed_offset_replay_compiled_pipeline(),)
        )
        seeded_result: BackfillExecutionResult = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="offsets",
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=12,
                    _replay_timestamp="2026-04-09 16:15:01.000",
                    _replay_landed_at="2026-04-09 16:15:01.000",
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
    finally:
        managed_client.close()

    shadow_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, kafka_topic FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} ORDER BY order_id"
    ).result_rows

    assert seeded_result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode == (
        "seeded_bounded_rebuild"
    )
    assert shadow_rows == list(test_case.expected_shadow_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteStartTimeReplayIntegrationTestCase(
            description="start time offset replay keeps prefix and replays the explicit tail",
            replay_lineage_mode="offsets",
            initial_deployment_id="20260409T171000Z_ab12cd",
            changed_deployment_id="20260409T171500Z_cd34ef",
            created_at="2026-04-09 17:10:00.123",
            initial_boundary_time="2026-04-09 17:10:00.000",
            changed_boundary_time="2026-04-09 17:15:00.000",
            lower_bound_source_order_id="frontier-order",
            lower_bound_offset_millis=0,
            expected_shadow_table_name="tbl__orders_enriched__20260409T171500Z_cd34ef",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", ""),
                ("live-order", "source.orders.created"),
            ),
        ),
        ExecuteStartTimeReplayIntegrationTestCase(
            description="start time offset replay before history replays the full available window",
            replay_lineage_mode="offsets",
            initial_deployment_id="20260409T171000Z_ab12cd",
            changed_deployment_id="20260409T171500Z_gh78ij",
            created_at="2026-04-09 17:10:00.123",
            initial_boundary_time="2026-04-09 17:10:00.000",
            changed_boundary_time="2026-04-09 17:15:00.000",
            lower_bound_source_order_id="historical-order",
            lower_bound_offset_millis=500,
            expected_shadow_table_name="tbl__orders_enriched__20260409T171500Z_gh78ij",
            expected_shadow_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", "source.orders.created"),
                ("live-order", "source.orders.created"),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_start_time_offset_replay_when_executing_then_it_replays_expected_rows(
    test_case: ExecuteStartTimeReplayIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    scenario_result: StartTimeReplayScenarioResult = run_start_time_replay_scenario(
        test_case=test_case,
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
    )

    assert (
        scenario_result.start_time_result.bootstrap.deployment_plan.rebuild_subtrees[
            0
        ].forced_start_time
        == scenario_result.converted_start_time
    )
    assert scenario_result.start_time_result.bootstrap.deployment_plan.rebuild_subtrees[
        0
    ].execution_mode == ("seeded_bounded_rebuild")
    assert scenario_result.shadow_rows == test_case.expected_shadow_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteMultipleBackfillsIntegrationTestCase(
            description="allows two staged backfills before first publish in greenfield mode",
            first_deployment_id="20260409T190000Z_ab12cd",
            second_deployment_id="20260409T190500Z_cd34ef",
            created_at="2026-04-09 19:00:00.123",
            boundary_time="2026-04-09 19:00:00.000",
            expected_staged_table_names=(
                "tbl__orders_enriched__20260409T190000Z_ab12cd",
                "tbl__orders_enriched__20260409T190500Z_cd34ef",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_staged_backfills_before_publish_when_executing_then_both_staged_tables_exist(
    test_case: ExecuteMultipleBackfillsIntegrationTestCase,
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
        first_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.first_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        second_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.second_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    staged_names: tuple[str, ...] = test_case.expected_staged_table_names
    staged_table_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name IN {staged_names} "
        "ORDER BY name"
    ).result_rows

    assert first_result.bootstrap.deployment_id == test_case.first_deployment_id
    assert second_result.bootstrap.deployment_id == test_case.second_deployment_id
    assert first_result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert second_result.bootstrap.root_reports[0].replay_strategy == "full_rebuild_required"
    assert staged_table_rows == [(name,) for name in sorted(test_case.expected_staged_table_names)]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteBoundedReplayReportingIntegrationTestCase(
            description="reports bounded replay when an active view exists",
            first_deployment_id="20260409T230000Z_ab12cd",
            second_deployment_id="20260409T230500Z_cd34ef",
            created_at="2026-04-09 23:00:00.123",
            boundary_time="2026-04-09 23:00:00.000",
            expected_first_strategy="create_from_scratch",
            expected_second_strategy="bounded_replay",
            expected_active_deployment_id="20260409T230000Z_ab12cd",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_view_when_backfilling_then_it_reports_bounded_replay_strategy(
    test_case: ExecuteBoundedReplayReportingIntegrationTestCase,
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
                _replay_timestamp="2026-04-09 22:59:59.000",
                _replay_landed_at="2026-04-09 22:59:59.000",
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
        first_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.first_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            render_create_view_ddl(
                database=clickhouse_database,
                view_name="tbl__orders_enriched",
                target_table_name=f"tbl__orders_enriched__{test_case.first_deployment_id}",
            )
        )
        second_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.second_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    first_report: RootBackfillReport = first_result.bootstrap.root_reports[0]
    second_report: RootBackfillReport = second_result.bootstrap.root_reports[0]

    assert first_report.replay_strategy == test_case.expected_first_strategy
    assert second_report.replay_strategy == test_case.expected_second_strategy
    assert second_report.active_deployment_id == test_case.expected_active_deployment_id


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteRepeatedPublishedBackfillIntegrationTestCase(
            description="uses bounded replay after publish when raw root has stable view",
            first_deployment_id="20260411T170000Z_ab12cd",
            second_deployment_id="20260411T170500Z_cd34ef",
            created_at="2026-04-11 17:00:00.123",
            first_boundary_time="2026-04-11 17:00:00.000",
            second_boundary_time="2026-04-11 17:05:00.000",
            expected_raw_view_rows=(("raw__orders", "MergeTree"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_raw_root_when_backfilling_again_then_it_uses_bounded_replay(
    test_case: ExecuteRepeatedPublishedBackfillIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("offsets")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
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

    try:
        first_result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.first_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.first_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.first_deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        second_result: BackfillExecutionResult = execute_backfill(
            request=build_offset_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.second_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.second_boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    raw_view_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name = 'raw__orders'"
    ).result_rows

    assert first_result.bootstrap.root_reports[0].replay_strategy == "create_from_scratch"
    assert second_result.bootstrap.root_reports[0].replay_strategy == "bounded_replay"
    assert (
        second_result.bootstrap.root_reports[0].active_deployment_id
        == test_case.first_deployment_id
    )
    assert raw_view_rows == list(test_case.expected_raw_view_rows)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteMixedRootBackfillReportingIntegrationTestCase(
            description=(
                "reports mixed per-root strategies when one root is active and another "
                "root is missing its view"
            ),
            deployment_id="20260409T230800Z_ab12cd",
            created_at="2026-04-09 23:08:00.123",
            boundary_time="2026-04-09 23:08:00.000",
            expected_report_rows=(
                ("tbl__customers_enriched", "full_rebuild_required", None),
                (
                    "tbl__orders_enriched",
                    "bounded_replay",
                    "20260409T230000Z_orders1",
                ),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_root_state_when_backfilling_then_it_reports_per_root_strategies(
    test_case: ExecuteMixedRootBackfillReportingIntegrationTestCase,
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
    clickhouse_client.command(
        "CREATE TABLE "
        f"{clickhouse_database}.tbl__orders_enriched__20260409T230000Z_orders1 "
        "(order_id String, _replay_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY (order_id)"
    )
    clickhouse_client.command(
        "CREATE MATERIALIZED VIEW "
        f"{clickhouse_database}.mv__orders_enriched__20260409T230000Z_orders1 "
        f"TO {clickhouse_database}.tbl__orders_enriched__20260409T230000Z_orders1 AS "
        "SELECT CAST(kafka_key AS String) AS order_id, "
        "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp "
        f"FROM {clickhouse_database}.raw__orders"
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name="tbl__orders_enriched",
            target_table_name="tbl__orders_enriched__20260409T230000Z_orders1",
        )
    )
    clickhouse_client.command(
        "CREATE TABLE "
        f"{clickhouse_database}.tbl__customers_enriched__20260409T230100Z_custom1 "
        "(order_id String, _replay_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY (order_id)"
    )
    clickhouse_client.command(
        "CREATE MATERIALIZED VIEW "
        f"{clickhouse_database}.mv__customers_enriched__20260409T230100Z_custom1 "
        f"TO {clickhouse_database}.tbl__customers_enriched__20260409T230100Z_custom1 AS "
        "SELECT CAST(kafka_key AS String) AS order_id, "
        "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp "
        f"FROM {clickhouse_database}.raw__customers"
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
        result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=BackfillBootstrapRequest(
                desired_state=build_desired_state((orders_pipeline, customers_pipeline)),
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode="timestamp",
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    report_rows: tuple[tuple[str, str, str | None], ...] = tuple(
        sorted(
            (
                report.root_key.name,
                report.replay_strategy,
                report.active_deployment_id,
            )
            for report in result.root_reports
        )
    )

    assert report_rows == test_case.expected_report_rows


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        BackfillAfterDeletedStagedTableIntegrationTestCase(
            description="recreates a deleted staged table when rerunning the unpublished candidate",
            deployment_id="20260409T231000Z_ab12cd",
            created_at="2026-04-09 23:10:00.123",
            boundary_time="2026-04-09 23:10:00.000",
            expected_recreated_relation_name=("tbl__orders_enriched__20260409T231000Z_ab12cd"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_staged_table_after_bootstrap_when_rerunning_then_candidate_is_recreated(
    test_case: BackfillAfterDeletedStagedTableIntegrationTestCase,
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
                _replay_timestamp="2026-04-09 23:09:59.000",
                _replay_landed_at="2026-04-09 23:09:59.000",
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
        _ = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            f"DROP TABLE {clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id}"
        )
        _ = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        recreated_rows: tuple[tuple[object, ...], ...] = tuple(
            tuple(row)
            for row in clickhouse_client.query(
                "SELECT name FROM system.tables "
                f"WHERE database = '{clickhouse_database}' "
                f"AND name = '{test_case.expected_recreated_relation_name}'"
            ).result_rows
        )
    finally:
        managed_client.close()

    assert recreated_rows == ((test_case.expected_recreated_relation_name,),)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PersistWatermarksWithoutMetadataTableIntegrationTestCase(
            description="recreates a deleted watermark metadata table before persistence",
            deployment_id="20260409T232000Z_ab12cd",
            created_at="2026-04-09 23:20:00.123",
            boundary_time="2026-04-09 23:20:00.000",
            expected_watermark_table_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_watermark_table_when_replaying_then_workflow_recreates_metadata(
    test_case: PersistWatermarksWithoutMetadataTableIntegrationTestCase,
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
        bootstrap_result: BackfillBootstrapResult = execute_backfill_bootstrap(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            f"DROP TABLE {clickhouse_database}._sqltide_virtual_replay_boundaries"
        )
        request: BackfillBootstrapRequest = build_scalar_replay_request(
            database=clickhouse_database,
            deployment_id=test_case.deployment_id,
            created_at=test_case.created_at,
            boundary_time=test_case.boundary_time,
            replay_lineage_mode="timestamp",
        )
        _ = execute_backfill(
            request=replace(
                request,
                confirmed_plan=bootstrap_result.deployment_plan,
            ),
            client=managed_client,
        )
        watermark_table_rows: Sequence[Sequence[object]] = clickhouse_client.query(
            "SELECT count() FROM system.tables WHERE database = "
            f"'{clickhouse_database}' AND name = '_sqltide_virtual_replay_boundaries'"
        ).result_rows
    finally:
        managed_client.close()

    assert int(str(watermark_table_rows[0][0])) == test_case.expected_watermark_table_count
