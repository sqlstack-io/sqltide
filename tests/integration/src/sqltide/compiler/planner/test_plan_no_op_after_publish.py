import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import load_actual_state, plan_deployment
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline, DesiredState
from sqltide.compiler.planner.constants import (
    PLANNED_CHANGE_TYPE_NO_OP,
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    TABLE_SCHEMA_CHANGE_KIND_BREAKING,
    TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING,
    TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
    TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
)
from sqltide.compiler.planner.models import ActualState, DeploymentPlan, PlannedObjectChange
from sqltide.executor.backfill.models import BackfillExecutionResult
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest, PublishResult
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.compiler.planner._test_types import (
    PlannerNoOpAfterPublishIntegrationTestCase,
    PlannerNormalizedTypeNoOpIntegrationTestCase,
    PlannerSchemaChangeAfterPublishIntegrationTestCase,
    PlannerSqlChangeAfterPublishIntegrationTestCase,
)
from tests.integration.src.sqltide.compiler.planner.helpers import (
    build_changed_schema_variant_compiled_pipeline,
    build_changed_sql_compiled_pipeline,
    get_orders_enriched_change,
    normalize_orders_enriched_timestamp_type,
    realize_compiled_pipelines,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill.helpers import (
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
        PlannerNoOpAfterPublishIntegrationTestCase(
            description="published greenfield deployment plans as a no-op afterward",
            deployment_id="20260410T130000Z_ab12cd",
            created_at="2026-04-10 13:00:00.123",
            boundary_time="2026-04-10 13:00:00.000",
            expected_rebuild_subtrees=(),
            expected_steps=(),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_greenfield_backfill_and_publish_when_planning_again_then_it_is_a_no_op(
    test_case: PlannerNoOpAfterPublishIntegrationTestCase,
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
                _replay_timestamp="2026-04-10 12:59:59.000",
                _replay_landed_at="2026-04-10 12:59:59.000",
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
        backfill_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        publish_result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database=clickhouse_database,
        render_resource=managed_client.render_resource,
    )

    assert backfill_result.bootstrap.deployment_id == test_case.deployment_id
    assert publish_result.deployment_id == test_case.deployment_id
    assert plan.rebuild_subtrees == test_case.expected_rebuild_subtrees
    assert plan.steps == test_case.expected_steps


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PlannerNormalizedTypeNoOpIntegrationTestCase(
            description="treats equivalent datetime type casing as a no-op after publish",
            deployment_id="20260410T130000Z_ab12cd",
            created_at="2026-04-10 13:00:00.123",
            boundary_time="2026-04-10 13:00:00.000",
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_equivalent_type_casing_after_publish_when_planning_again_then_it_is_a_no_op(
    test_case: PlannerNormalizedTypeNoOpIntegrationTestCase,
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
                _replay_timestamp="2026-04-10 12:59:59.000",
                _replay_landed_at="2026-04-10 12:59:59.000",
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
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        normalized_desired_state: DesiredState = normalize_orders_enriched_timestamp_type(
            desired_state
        )
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=normalized_desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    plan: DeploymentPlan = plan_deployment(
        desired_state=normalized_desired_state,
        actual_state=actual_state,
        default_database=clickhouse_database,
        render_resource=managed_client.render_resource,
    )

    assert plan.rebuild_subtrees == ()
    assert all(
        object_change.change_type == test_case.expected_change_type
        for object_change in plan.object_changes
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PlannerSqlChangeAfterPublishIntegrationTestCase(
            description="published deployment plans a rebuild after authored sql changes",
            deployment_id="20260410T131500Z_cd34ef",
            created_at="2026-04-10 13:15:00.123",
            boundary_time="2026-04-10 13:15:00.000",
            expected_rebuild_root_names=("mv__orders_enriched",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_deployment_when_authored_sql_changes_then_plan_requires_rebuild(
    test_case: PlannerSqlChangeAfterPublishIntegrationTestCase,
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
                _replay_timestamp="2026-04-10 13:14:59.000",
                _replay_landed_at="2026-04-10 13:14:59.000",
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
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_compiled_pipeline: CompiledPipeline = build_changed_sql_compiled_pipeline()
        changed_desired_state: DesiredState = realize_compiled_pipelines(
            (changed_compiled_pipeline,)
        ).desired_state
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=changed_desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    plan: DeploymentPlan = plan_deployment(
        desired_state=changed_desired_state,
        actual_state=actual_state,
        default_database=clickhouse_database,
        render_resource=managed_client.render_resource,
    )

    assert (
        tuple(subtree.root_key.name for subtree in plan.rebuild_subtrees)
        == test_case.expected_rebuild_root_names
    )
    assert plan.steps != ()
    assert plan.sql_diffs != ()
    assert tuple(sql_diff.name for sql_diff in plan.sql_diffs) == ("mv__orders_enriched",)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PlannerSchemaChangeAfterPublishIntegrationTestCase(
            description=(
                "published deployment emits both transform and table diffs after added columns"
            ),
            deployment_id="20260410T133000Z_ef56gh",
            created_at="2026-04-10 13:30:00.123",
            boundary_time="2026-04-10 13:30:00.000",
            changed_pipeline_kind="add_column",
            expected_rebuild_root_names=("tbl__orders_enriched",),
            expected_sql_diff_names=("mv__orders_enriched", "tbl__orders_enriched"),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
            expected_execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
        ),
        PlannerSchemaChangeAfterPublishIntegrationTestCase(
            description="published deployment classifies removed columns as breaking but seedable",
            deployment_id="20260410T133500Z_gh78ij",
            created_at="2026-04-10 13:35:00.123",
            boundary_time="2026-04-10 13:35:00.000",
            changed_pipeline_kind="remove_column",
            expected_rebuild_root_names=("tbl__orders_enriched",),
            expected_sql_diff_names=("mv__orders_enriched", "tbl__orders_enriched"),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        PlannerSchemaChangeAfterPublishIntegrationTestCase(
            description=(
                "published deployment classifies add and remove changes as breaking but seedable"
            ),
            deployment_id="20260410T134000Z_jk90lm",
            created_at="2026-04-10 13:40:00.123",
            boundary_time="2026-04-10 13:40:00.000",
            changed_pipeline_kind="add_and_remove_columns",
            expected_rebuild_root_names=("tbl__orders_enriched",),
            expected_sql_diff_names=("mv__orders_enriched", "tbl__orders_enriched"),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        PlannerSchemaChangeAfterPublishIntegrationTestCase(
            description="published deployment classifies type changes as breaking and non-seedable",
            deployment_id="20260410T134500Z_mn12op",
            created_at="2026-04-10 13:45:00.123",
            boundary_time="2026-04-10 13:45:00.000",
            changed_pipeline_kind="type_change",
            expected_rebuild_root_names=("tbl__orders_enriched",),
            expected_sql_diff_names=("mv__orders_enriched", "tbl__orders_enriched"),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_published_deployment_when_transform_output_schema_changes_then_plan_emits_table_diff(
    test_case: PlannerSchemaChangeAfterPublishIntegrationTestCase,
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
                _replay_timestamp="2026-04-10 13:14:59.000",
                _replay_landed_at="2026-04-10 13:14:59.000",
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
                created_at=test_case.created_at,
                boundary_time=test_case.boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        changed_compiled_pipeline: CompiledPipeline = (
            build_changed_schema_variant_compiled_pipeline(test_case.changed_pipeline_kind)
        )
        changed_desired_state: DesiredState = realize_compiled_pipelines(
            (changed_compiled_pipeline,)
        ).desired_state
        actual_state: ActualState = load_actual_state(
            client=managed_client,
            desired_state=changed_desired_state,
            database=clickhouse_database,
        )
    finally:
        managed_client.close()

    plan: DeploymentPlan = plan_deployment(
        desired_state=changed_desired_state,
        actual_state=actual_state,
        default_database=clickhouse_database,
        render_resource=managed_client.render_resource,
    )

    assert (
        tuple(subtree.root_key.name for subtree in plan.rebuild_subtrees)
        == test_case.expected_rebuild_root_names
    )
    assert tuple(subtree.execution_mode for subtree in plan.rebuild_subtrees) == (
        test_case.expected_execution_mode,
    )
    assert tuple(sql_diff.name for sql_diff in plan.sql_diffs) == test_case.expected_sql_diff_names
    table_change: PlannedObjectChange = get_orders_enriched_change(plan)
    assert table_change.schema_change_kind == test_case.expected_schema_change_kind
    assert table_change.seed_compatibility == test_case.expected_seed_compatibility
