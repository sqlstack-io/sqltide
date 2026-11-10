from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import (
    add_empty_replay_source_warnings,
    analyze_project,
    build_compiler_adapter_profile,
    load_planning_warehouse_snapshot,
    resolve_unsupported_bounded_replay_behavior,
    run_plan,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import CatalogSnapshot
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.compiler.compile.models import (
    Column,
    CompilerAdapterProfile,
    DesiredState,
    DesiredTable,
    ObjectKey,
    TableSpec,
    TableStorage,
)
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.constants import (
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    REBUILD_STRATEGY_SHADOW,
)
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    PlanningWarehouseSnapshot,
    RebuildSubtree,
)
from tests.integration.src.sqltide.cli._test_types import (
    CliBoundedPlanSnapshotIntegrationTestCase,
    CliPlanSnapshotIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    RecordingDelegatingConnection,
    build_managed_clickhouse_client,
    write_source_mode_plan_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings

MANAGED_PLAN_SOURCE_TEMPLATE: str = """
sources:
  - kind: kafka
    name: orders
    broker_list: kafka:9092
    topic: source.orders
    replay_boundary:
      mode: {mode}
"""
MANAGED_PLAN_MODEL_TEMPLATE: str = """
MODEL (
  engine "MergeTree()",
  order_by ["order_id"]
);

SELECT
  CAST(kafka_key AS String) AS order_id,
  CAST({lineage_column} AS {lineage_type}) AS {lineage_column}
FROM __ref("orders")
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanSnapshotIntegrationTestCase(
            description="plans a managed offsets source against real ClickHouse",
            source_contents=MANAGED_PLAN_SOURCE_TEMPLATE.format(mode="offsets"),
            model_contents=MANAGED_PLAN_MODEL_TEMPLATE.format(
                lineage_column="_replay_offset", lineage_type="Int64"
            ),
            existing_table_ddl_statements=(),
            expected_replay_lineage_mode="offsets",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
        CliPlanSnapshotIntegrationTestCase(
            description="plans a managed timestamp source against real ClickHouse",
            source_contents=MANAGED_PLAN_SOURCE_TEMPLATE.format(mode="timestamp"),
            model_contents=MANAGED_PLAN_MODEL_TEMPLATE.format(
                lineage_column="_replay_timestamp", lineage_type="DateTime64(3)"
            ),
            existing_table_ddl_statements=(),
            expected_replay_lineage_mode="timestamp",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
        CliPlanSnapshotIntegrationTestCase(
            description="plans a managed landed-at source against real ClickHouse",
            source_contents=MANAGED_PLAN_SOURCE_TEMPLATE.format(mode="landed_at"),
            model_contents=MANAGED_PLAN_MODEL_TEMPLATE.format(
                lineage_column="_replay_landed_at", lineage_type="DateTime64(3)"
            ),
            existing_table_ddl_statements=(),
            expected_replay_lineage_mode="landed_at",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
        CliPlanSnapshotIntegrationTestCase(
            description="plans an adopted offsets source against real ClickHouse",
            source_contents="""
            sources:
              - kind: stream_table
                name: orders
                table_name: orders_existing
                replay_boundary:
                  mode: offsets
                  columns:
                    _replay_partition: event_partition
                    _replay_offset: event_offset
                    _replay_timestamp: event_timestamp
            """,
            model_contents="""
            MODEL (
              engine "MergeTree()",
              order_by ["order_id"]
            );

            SELECT
              CAST(order_id AS String) AS order_id,
              CAST(_replay_partition AS Int32) AS _replay_partition,
              CAST(_replay_offset AS Int64) AS _replay_offset
            FROM __ref("orders")
            """,
            existing_table_ddl_statements=(
                "CREATE TABLE {database}.orders_existing "
                "(order_id String, event_partition Int32, event_offset Int64, "
                "event_timestamp DateTime64(3)) ENGINE = MergeTree ORDER BY order_id",
            ),
            expected_replay_lineage_mode="offsets",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
        CliPlanSnapshotIntegrationTestCase(
            description="plans an adopted timestamp source against real ClickHouse",
            source_contents="""
            sources:
              - kind: stream_table
                name: orders
                table_name: orders_existing
                replay_boundary:
                  mode: timestamp
                  columns:
                    _replay_timestamp: event_timestamp
            """,
            model_contents="""
            MODEL (
              engine "MergeTree()",
              order_by ["order_id"]
            );

            SELECT
              CAST(order_id AS String) AS order_id,
              CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp
            FROM __ref("orders")
            """,
            existing_table_ddl_statements=(
                "CREATE TABLE {database}.orders_existing "
                "(order_id String, event_timestamp DateTime64(3)) "
                "ENGINE = MergeTree ORDER BY order_id",
            ),
            expected_replay_lineage_mode="timestamp",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
        CliPlanSnapshotIntegrationTestCase(
            description="plans an adopted cursor source against real ClickHouse",
            source_contents="""
            sources:
              - kind: stream_table
                name: orders
                table_name: orders_existing
                replay_boundary:
                  mode: cursor
                  columns:
                    _replay_cursor: event_cursor
                    _replay_timestamp: event_timestamp
            """,
            model_contents="""
            MODEL (
              engine "MergeTree()",
              order_by ["order_id"]
            );

            SELECT
              CAST(order_id AS String) AS order_id,
              CAST(_replay_cursor AS UInt64) AS _replay_cursor
            FROM __ref("orders")
            """,
            existing_table_ddl_statements=(
                "CREATE TABLE {database}.orders_existing "
                "(order_id String, event_cursor UInt64, event_timestamp DateTime64(3)) "
                "ENGINE = MergeTree ORDER BY order_id",
            ),
            expected_replay_lineage_mode="cursor",
            expected_exit_code=0,
            expected_output_fragment="Plan Ready",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_real_source_mode_when_planning_then_snapshot_validation_succeeds(
    test_case: CliPlanSnapshotIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipelines_root: Path = write_source_mode_plan_project(
        project_dir=tmp_path / "project",
        source_contents=test_case.source_contents,
        model_contents=test_case.model_contents,
    )
    client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    ddl_statement: str
    for ddl_statement in test_case.existing_table_ddl_statements:
        clickhouse_client.command(ddl_statement.format(database=clickhouse_database))
    relation_query: str = (
        f"SELECT name FROM system.tables WHERE database = '{clickhouse_database}' ORDER BY name"
    )
    before_relation_names: tuple[str, ...] = tuple(
        str(row[0]) for row in clickhouse_client.query(relation_query).result_rows
    )
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(path=pipelines_root)
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=pipelines_root,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
    )

    try:
        exit_code: int = run_plan(
            options=PlanCommandOptions(
                pipelines_root=pipelines_root,
                database=clickhouse_database,
                selectors=(),
                full_refresh=False,
                start_time=None,
                deployment_id=None,
                json_output=False,
                verbose=False,
            ),
            client=client,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
    finally:
        client.close()

    after_relation_names: tuple[str, ...] = tuple(
        str(row[0]) for row in clickhouse_client.query(relation_query).result_rows
    )
    artifact_root: Path = pipelines_root.parent / "target/run/plan"
    step_paths: tuple[Path, ...] = tuple(sorted((artifact_root / "steps").glob("*.sql")))
    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_output_fragment in capsys.readouterr().out
    assert before_relation_names == after_relation_names
    assert step_paths
    assert (artifact_root / "workflow.sql").read_bytes() == b"\n".join(
        path.read_bytes() for path in step_paths
    )
    assert (
        str(analysis.compiled_project.pipelines[0].effective_replay_lineage_mode)
        == test_case.expected_replay_lineage_mode
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliBoundedPlanSnapshotIntegrationTestCase(
            description="resolves a history-preserving bounded plan from one real snapshot",
            start_time="2026-07-26 12:00:00.000",
            expected_execution_mode="seeded_bounded_rebuild",
            expected_warning_count=1,
            expected_catalog_load_count=1,
            expected_query_count=6,
            expected_point_in_time_query_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_bounded_root_when_planning_then_one_snapshot_preserves_resolution(
    test_case: CliBoundedPlanSnapshotIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    delegate: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    client: RecordingDelegatingConnection = RecordingDelegatingConnection(delegate)
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing "
        "(order_id String, event_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY order_id"
    )
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.tbl__orders_enriched__dep_a "
        "(order_id String, _replay_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY order_id"
    )
    clickhouse_client.command(
        f"CREATE VIEW {clickhouse_database}.tbl__orders_enriched AS "
        f"SELECT * FROM {clickhouse_database}.tbl__orders_enriched__dep_a"
    )
    source_key: ObjectKey = ObjectKey(None, "table", "orders_existing")
    root_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    desired_state: DesiredState = DesiredState(
        objects=(
            DesiredTable(
                key=root_key,
                deps=(source_key,),
                spec=TableSpec(
                    columns=(
                        Column(name="order_id", type="String"),
                        Column(name="_replay_timestamp", type="DateTime64(3)"),
                    ),
                    storage=TableStorage(engine="MergeTree()", order_by=("order_id",)),
                ),
            ),
        ),
        replay_anchor_keys=frozenset({source_key}),
        mutable_ref_warning_keys=frozenset(),
    )
    plan: DeploymentPlan = DeploymentPlan(
        deployment_id=None,
        object_changes=(),
        rebuild_subtrees=(
            RebuildSubtree(
                root_key=root_key,
                affected_keys=(root_key,),
                upstream_boundary_key=source_key,
                strategy=REBUILD_STRATEGY_SHADOW,
                execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                forced_start_time=test_case.start_time,
            ),
        ),
        steps=(),
        prepared_shadow_objects=(),
        warnings=(),
    )

    try:
        snapshot: PlanningWarehouseSnapshot = load_planning_warehouse_snapshot(
            client=client,
            database=clickhouse_database,
        )
        catalog: CatalogSnapshot = snapshot.catalog
        resolved_plan: DeploymentPlan = resolve_unsupported_bounded_replay_behavior(
            catalog=catalog,
            deployment_plan=plan,
            desired_state=desired_state,
            default_database=clickhouse_database,
            replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
        )
        resolved_plan = add_empty_replay_source_warnings(
            client=client,
            catalog=catalog,
            database=clickhouse_database,
            desired_state=desired_state,
            plan=resolved_plan,
        )
    finally:
        client.close()

    point_in_time_query_count: int = sum(
        statement.startswith("SELECT count() FROM ") for statement in client.query_statements
    )
    assert (
        str(resolved_plan.rebuild_subtrees[0].execution_mode) == test_case.expected_execution_mode
    )
    assert resolved_plan.rebuild_subtrees[0].history_preserving_bounded_supported
    assert len(resolved_plan.warnings) == test_case.expected_warning_count
    assert client.catalog_load_count == test_case.expected_catalog_load_count
    assert len(client.query_statements) == test_case.expected_query_count
    assert point_in_time_query_count == test_case.expected_point_in_time_query_count
