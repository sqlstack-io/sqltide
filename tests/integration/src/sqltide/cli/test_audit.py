import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from _pytest.capture import CaptureResult
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import (
    build_compiler_adapter_profile,
    build_deployment_physical_name,
    run_audit,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.readiness.main._run_deployment_audit import run_deployment_audit
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.executor.backfill.models import BackfillExecutionResult
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.cli._test_types import (
    CliAuditBackfillCommandIntegrationTestCase,
    CliAuditCommandIntegrationTestCase,
    CliManagedSourceResources,
)
from tests.integration.src.sqltide.cli.helpers import (
    KEYED_ORDER_ITEMS_COLUMNS,
    KEYED_ORDER_ITEMS_ORDER_BY,
    NULLABLE_ORDER_ITEMS_COLUMNS,
    UNORDERED_ORDER_ITEMS_ORDER_BY,
    build_managed_clickhouse_client,
    build_order_items_ddl,
    write_audit_project_for,
    write_backfill_audit_project_files,
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
        CliAuditCommandIntegrationTestCase(
            description="reports warning audit results against published logical tables",
            selectors=("order_items",),
            project_writer_name="singular",
            order_items_columns=KEYED_ORDER_ITEMS_COLUMNS,
            order_items_order_by=KEYED_ORDER_ITEMS_ORDER_BY,
            order_items_rows=(("ord_001", -5.0), ("ord_missing", 10.0)),
            expected_exit_code=0,
            expected_output_fragments=(
                "Audit Results",
                "Warnings",
                "audits/singular/order_events/negative_line_totals.sql",
                "failing rows: 1",
                "Result: PASS (0 errors, 1 warnings)",
            ),
            expected_node_result_count=1,
            expected_node_result_statuses=("warning",),
            expected_invocation_outcome="succeeded",
        ),
        CliAuditCommandIntegrationTestCase(
            description="reports generic warning audit results against published logical tables",
            selectors=("order_items",),
            project_writer_name="generic",
            order_items_columns=NULLABLE_ORDER_ITEMS_COLUMNS,
            order_items_order_by=UNORDERED_ORDER_ITEMS_ORDER_BY,
            order_items_rows=((None, 10.0), ("ord_002", 10.0)),
            expected_exit_code=0,
            expected_output_fragments=(
                "Audit Results",
                "Warnings",
                "pipelines/pl__order_events/order_items.sql  [order items order id not null]",
                "failing rows: 1",
                "Result: PASS (0 errors, 1 warnings)",
            ),
            expected_node_result_count=1,
            expected_node_result_statuses=("warning",),
            expected_invocation_outcome="succeeded",
        ),
        CliAuditCommandIntegrationTestCase(
            description="reports multiple singular audits from one file with names",
            selectors=("order_items",),
            project_writer_name="multi",
            order_items_columns=KEYED_ORDER_ITEMS_COLUMNS,
            order_items_order_by=KEYED_ORDER_ITEMS_ORDER_BY,
            order_items_rows=(("ord_001", -5.0), ("ord_missing", 10.0)),
            expected_exit_code=0,
            expected_output_fragments=(
                "Audit Results",
                "audits/singular/order_events/quality.sql  [negative line totals]",
                "audits/singular/order_events/quality.sql  [missing order ids]",
                "failing rows: 1",
                "Result: PASS (0 errors, 2 warnings)",
            ),
            expected_node_result_count=2,
            expected_node_result_statuses=("warning", "warning"),
            expected_invocation_outcome="succeeded",
        ),
        CliAuditCommandIntegrationTestCase(
            description="records one warehouse audit error and continues later audits",
            selectors=("order_items",),
            project_writer_name="error",
            order_items_columns=KEYED_ORDER_ITEMS_COLUMNS,
            order_items_order_by=KEYED_ORDER_ITEMS_ORDER_BY,
            order_items_rows=(("ord_001", -5.0), ("ord_002", 10.0)),
            expected_exit_code=1,
            expected_output_fragments=(
                "Audit Results",
                "audits/singular/order_events/quality.sql  [broken column]",
                "audits/singular/order_events/quality.sql  [negative line totals]",
                "Result: FAIL (1 errors, 1 warnings)",
            ),
            expected_node_result_count=2,
            expected_node_result_statuses=("error", "warning"),
            expected_invocation_outcome="failed",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_project_when_running_live_audit_then_it_reports_expected_results(
    test_case: CliAuditCommandIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    write_audit_project_for(project_writer_name=test_case.project_writer_name, project_dir=tmp_path)
    clickhouse_client.command(
        build_order_items_ddl(
            database=clickhouse_database,
            columns=test_case.order_items_columns,
            order_by=test_case.order_items_order_by,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.tbl__order_items",
        data=[list(row) for row in test_case.order_items_rows],
        column_names=["order_id", "line_total"],
    )
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        exit_code: int = run_audit(
            pipelines_root=tmp_path / "pipelines",
            project_dir=tmp_path,
            database=clickhouse_database,
            selectors=test_case.selectors,
            json_output=False,
            client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=tmp_path),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()
    captured: CaptureResult[str] = capsys.readouterr()
    invocation_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT command, outcome FROM {clickhouse_database}._sqltide_invocations"
    ).result_rows
    node_result_count: int = int(
        clickhouse_client.query(
            f"SELECT count() FROM {clickhouse_database}._sqltide_node_results"
        ).result_rows[0][0]
    )
    node_result_statuses: tuple[str, ...] = tuple(
        str(row[0])
        for row in clickhouse_client.query(
            f"SELECT status FROM {clickhouse_database}._sqltide_node_results ORDER BY node_name"
        ).result_rows
    )

    assert exit_code == test_case.expected_exit_code
    expected_output_fragment: str
    for expected_output_fragment in test_case.expected_output_fragments:
        assert expected_output_fragment in captured.out
    assert invocation_rows == [("audit", test_case.expected_invocation_outcome)]
    assert node_result_count == test_case.expected_node_result_count
    assert node_result_statuses == test_case.expected_node_result_statuses


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliAuditBackfillCommandIntegrationTestCase(
            description="runs project sql quality checks during deployment audit",
            expected_exit_code=0,
            expected_quality_check_count=1,
            expected_assessment="not_ready",
            expected_failing_row_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_audit_project_when_running_audit_backfill_then_it_includes_quality_checks(
    test_case: CliAuditBackfillCommandIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    deployment_id: str = "20260420T000000Z_ab12cd"
    created_at: str = "2026-04-20 00:00:00.000"
    boundary_time: str = "2026-04-20 00:00:00.000"
    write_backfill_audit_project_files(tmp_path)
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    managed_source: CliManagedSourceResources = cast(
        CliManagedSourceResources,
        require_managed_source(compiled_pipeline),
    )
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=managed_source.kafka_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(table=managed_source.raw_table, database=clickhouse_database)
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=managed_source.materialized_view, database=clickhouse_database
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{managed_source.raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="ord_001",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-19 23:59:59.000",
                _replay_landed_at="2026-04-19 23:59:59.000",
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
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        backfill_result: BackfillExecutionResult = execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=deployment_id,
                created_at=created_at,
                boundary_time=boundary_time,
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        staged_table_name: str = build_deployment_physical_name(
            logical_name="tbl__orders_enriched",
            deployment_id=backfill_result.bootstrap.deployment_id,
        )
        clickhouse_client.command(
            f"ALTER TABLE {clickhouse_database}.{staged_table_name} "
            "UPDATE _replay_timestamp = toDateTime64('2026-04-19 23:59:59.000', 3) WHERE 1"
        )
        exit_code: int = run_deployment_audit(
            pipelines_root=tmp_path / "pipelines",
            project_dir=tmp_path,
            database=clickhouse_database,
            metadata_database=None,
            deployment_id=deployment_id,
            json_output=True,
            client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=tmp_path),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()
    captured: CaptureResult[str] = capsys.readouterr()
    payload: dict[str, object] = json.loads(captured.out)

    assert exit_code == test_case.expected_exit_code
    assert payload["assessment"] == test_case.expected_assessment
    quality_check_results: list[dict[str, object]] = payload["quality_check_results"]  # type: ignore[assignment]
    assert len(quality_check_results) == test_case.expected_quality_check_count
    assert quality_check_results[0]["failing_row_count"] == test_case.expected_failing_row_count
