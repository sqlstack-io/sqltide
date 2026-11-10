from collections.abc import Sequence
from pathlib import Path

import pytest
from _pytest.capture import CaptureResult
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import build_compiler_adapter_profile, run_test
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.integration.src.sqltide.cli._test_types import (
    CliTestCommandIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    DOWNSTREAM_REF_SQL_TEST,
    MULTI_NAMED_SQL_TESTS,
    MULTI_TARGET_FAILING_SQL_TEST,
    SINGLE_EXPECTED_SQL_TEST,
    build_managed_clickhouse_client,
    write_managed_source_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import write_pipeline_file
from tests.unit.src.sqltide.compiler.test_discovery.helpers import (
    write_sql_test_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliTestCommandIntegrationTestCase(
            description="reports a passing SQL-native test result",
            selectors=("order_items",),
            sql_test_content=SINGLE_EXPECTED_SQL_TEST,
            expected_line_total="20.0",
            extra_sql_test_files=(("test_daily_revenue.sql", DOWNSTREAM_REF_SQL_TEST),),
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "order_items",
                "tests/order_events/test_line_total.sql",
                "Results: 1 passed, 0 failed",
            ),
            expected_node_result_count=1,
            expected_invocation_outcome="succeeded",
        ),
        CliTestCommandIntegrationTestCase(
            description="runs multiple sql tests from one file",
            selectors=(),
            sql_test_content=MULTI_NAMED_SQL_TESTS,
            expected_line_total="20.0",
            extra_sql_test_files=(),
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "tests/order_events/test_line_total.sql  [line total computes correctly]",
                "tests/order_events/test_line_total.sql  [line total remains stable on repeat]",
                "Results: 2 passed, 0 failed",
            ),
            expected_node_result_count=2,
            expected_invocation_outcome="succeeded",
        ),
        CliTestCommandIntegrationTestCase(
            description="renders grouped missing and unexpected rows for failures",
            selectors=("order_items",),
            sql_test_content=SINGLE_EXPECTED_SQL_TEST,
            expected_line_total="25.0",
            extra_sql_test_files=(("test_daily_revenue.sql", DOWNSTREAM_REF_SQL_TEST),),
            expected_exit_code=1,
            expected_output_fragments=(
                "FAIL",
                "order_items",
                "diff (1 row differs):",
                "columns: order_id, line_total",
                "row  state     order_id  line_total",
                "1    expected  ord_001   25",
                "1    actual    ord_001   20",
                "Failed:",
                "sqt test tests/order_events/test_line_total.sql",
            ),
            expected_node_result_count=1,
            expected_invocation_outcome="failed",
        ),
        CliTestCommandIntegrationTestCase(
            description="renders grouped failures for multiple expected targets in one test",
            selectors=("order_items",),
            sql_test_content=MULTI_TARGET_FAILING_SQL_TEST,
            expected_line_total="25.0",
            extra_sql_test_files=(),
            expected_exit_code=1,
            expected_output_fragments=(
                "FAIL",
                "order_items, daily_revenue",
                "target: order_items",
                "target: daily_revenue",
                "sqt test tests/order_events/test_line_total.sql",
            ),
            expected_node_result_count=1,
            expected_invocation_outcome="failed",
        ),
    ],
    ids=lambda case: case.description,
)
@pytest.mark.integration
def test_given_sql_native_test_project_when_running_test_command_then_it_reports_expected_results(
    test_case: CliTestCommandIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    transform_file_path: Path = tmp_path / "pipelines" / "pl__order_events" / "order_items.sql"
    downstream_file_path: Path = tmp_path / "pipelines" / "pl__order_events" / "daily_revenue.sql"
    test_file_path: Path = tmp_path / "tests" / "order_events" / "test_line_total.sql"
    write_managed_source_project(project_dir=tmp_path)
    write_pipeline_file(
        transform_file_path,
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __ref("orders")
        """,
    )
    write_pipeline_file(
        downstream_file_path,
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total AS Nullable(Float64)) AS line_total
        FROM __ref("order_items")
        """,
    )
    write_sql_test_file(
        test_file_path,
        test_case.sql_test_content.format(expected_line_total=test_case.expected_line_total),
    )
    extra_test_file_name: str
    extra_test_content: str
    for extra_test_file_name, extra_test_content in test_case.extra_sql_test_files:
        write_sql_test_file(
            tmp_path / "tests" / "order_events" / extra_test_file_name, extra_test_content
        )

    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        exit_code: int = run_test(
            pipelines_root=tmp_path / "pipelines",
            project_dir=tmp_path,
            selectors=test_case.selectors,
            paths=(),
            verbose=False,
            client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=tmp_path),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
            database=clickhouse_database,
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

    assert clickhouse_client.query("SELECT 1").result_rows == [(1,)]
    assert exit_code == test_case.expected_exit_code
    expected_output_fragment: str
    for expected_output_fragment in test_case.expected_output_fragments:
        assert expected_output_fragment in captured.out
    assert invocation_rows == [("test", test_case.expected_invocation_outcome)]
    assert node_result_count == test_case.expected_node_result_count
