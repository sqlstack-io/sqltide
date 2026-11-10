from pathlib import Path

import pytest
from _pytest.capture import CaptureResult
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.cli.test.main._run_test import run_test
from tests.integration.src.sqltide.cli._test_types import (
    CliTestSemanticsIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    RecordingDelegatingConnection,
    build_managed_clickhouse_client,
    write_sql_test_semantics_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.unit.src.sqltide.cli.compile.helpers import copy_orders_demo

_DUPLICATE_MULTIPLICITY_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, 1 AS quantity, 1.0 AS unit_price
  UNION ALL
  SELECT 'b' AS order_id, 1 AS quantity, 1.0 AS unit_price
  UNION ALL
  SELECT 'c' AS order_id, 2 AS quantity, 1.0 AS unit_price
),
__expected__order_items AS (
  SELECT 'a' AS order_id, 1.0 AS line_total
  UNION ALL
  SELECT 'b' AS order_id, 2.0 AS line_total
  UNION ALL
  SELECT 'c' AS order_id, 2.0 AS line_total
)
SELECT 1
"""

_EQUAL_COUNT_REDISTRIBUTED_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'x' AS order_id, 1 AS quantity, 1.0 AS unit_price
  UNION ALL
  SELECT 'x' AS order_id, 1 AS quantity, 1.0 AS unit_price
  UNION ALL
  SELECT 'x' AS order_id, 2 AS quantity, 1.0 AS unit_price
),
__expected__order_items AS (
  SELECT 'x' AS order_id, 1.0 AS line_total
  UNION ALL
  SELECT 'x' AS order_id, 2.0 AS line_total
  UNION ALL
  SELECT 'x' AS order_id, 2.0 AS line_total
)
SELECT 1
"""

_NULL_MATCHES_NULL_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Int64)) AS quantity, 1.0 AS unit_price
),
__expected__order_items AS (
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Float64)) AS line_total
)
SELECT 1
"""

_NULL_VERSUS_VALUE_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Int64)) AS quantity, 1.0 AS unit_price
),
__expected__order_items AS (
  SELECT 'a' AS order_id, 1.0 AS line_total
)
SELECT 1
"""

_DUPLICATE_NULL_DRIFT_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Int64)) AS quantity, 1.0 AS unit_price
  UNION ALL
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Int64)) AS quantity, 1.0 AS unit_price
),
__expected__order_items AS (
  SELECT 'a' AS order_id, CAST(NULL AS Nullable(Float64)) AS line_total
)
SELECT 1
"""

_PASSING_ASSERTION_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, 2 AS quantity, 10.0 AS unit_price
),
__assert__line_totals_are_positive AS (
  SELECT order_id FROM __ref("order_items") WHERE line_total <= 0
)
SELECT 1
"""

_FAILING_ASSERTION_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, -2 AS quantity, 10.0 AS unit_price
),
__assert__line_totals_are_positive AS (
  SELECT order_id FROM __ref("order_items") WHERE line_total <= 0
)
SELECT 1
"""

_TERMINAL_CHAIN_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, 2 AS quantity, 10.0 AS unit_price
),
__expected__revenue_report AS (
  SELECT 'a' AS order_id, 20.0 AS reported_total
)
SELECT 1
"""

_DIAMOND_CHAIN_TEST: str = """
TEST ();

WITH
__source__orders AS (
  SELECT 'a' AS order_id, 2 AS quantity, 10.0 AS unit_price
),
__expected__order_summary AS (
  SELECT 'a' AS order_id, 22.0 AS total_with_tax
)
SELECT 1
"""

_PASSING_MACRO_TEST: str = """
TEST (mode macro, name "doubles the value");

WITH
input_values AS (
  SELECT 21 AS base_value
),
__macro_actual__ AS (
  SELECT @doubled('base_value') AS doubled FROM input_values
),
__macro_expected__ AS (
  SELECT 42 AS doubled
)
SELECT 1
"""

_FAILING_MACRO_TEST: str = """
TEST (mode macro, name "doubles the value");

WITH
input_values AS (
  SELECT 21 AS base_value
),
__macro_actual__ AS (
  SELECT @doubled('base_value') AS doubled FROM input_values
),
__macro_expected__ AS (
  SELECT 41 AS doubled
)
SELECT 1
"""

_DOUBLED_MACRO: str = """
def doubled(value: str) -> str:
    return f"({value}) * 2"
"""


@pytest.mark.parametrize(
    "test_case",
    [
        CliTestSemanticsIntegrationTestCase(
            description="fails when duplicate multiplicity differs",
            sql_test_content=_DUPLICATE_MULTIPLICITY_TEST,
            macro_file_contents="",
            expected_exit_code=1,
            expected_output_fragments=("FAIL", "order_items", "Results: 0 passed, 1 failed"),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="fails equal count redistributed duplicates",
            sql_test_content=_EQUAL_COUNT_REDISTRIBUTED_TEST,
            macro_file_contents="",
            expected_exit_code=1,
            expected_output_fragments=("FAIL", "Results: 0 passed, 1 failed"),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="passes when null matches null",
            sql_test_content=_NULL_MATCHES_NULL_TEST,
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=("PASS", "Results: 1 passed, 0 failed"),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="fails when null is compared against a value",
            sql_test_content=_NULL_VERSUS_VALUE_TEST,
            macro_file_contents="",
            expected_exit_code=1,
            expected_output_fragments=("FAIL", "NULL", "Results: 0 passed, 1 failed"),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="fails when duplicate null multiplicity drifts",
            sql_test_content=_DUPLICATE_NULL_DRIFT_TEST,
            macro_file_contents="",
            expected_exit_code=1,
            expected_output_fragments=("FAIL", "unexpected rows", "Results: 0 passed, 1 failed"),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="passes a zero row assertion",
            sql_test_content=_PASSING_ASSERTION_TEST,
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "assert line_totals_are_positive",
                "Results: 1 passed, 0 failed",
            ),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="fails an assertion that returns violating rows",
            sql_test_content=_FAILING_ASSERTION_TEST,
            macro_file_contents="",
            expected_exit_code=1,
            expected_output_fragments=(
                "FAIL",
                "assert line_totals_are_positive",
                "unexpected rows",
                "Results: 0 passed, 1 failed",
            ),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="passes a three model chain from one terminal expected target",
            sql_test_content=_TERMINAL_CHAIN_TEST,
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "revenue_report",
                "Results: 1 passed, 0 failed",
            ),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="passes a diamond chain assembled once from one terminal target",
            sql_test_content=_DIAMOND_CHAIN_TEST,
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "order_summary",
                "Results: 1 passed, 0 failed",
            ),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="passes a macro mode test",
            sql_test_content=_PASSING_MACRO_TEST,
            macro_file_contents=_DOUBLED_MACRO,
            expected_exit_code=0,
            expected_output_fragments=(
                "PASS",
                "macro doubles the value",
                "Results: 1 passed, 0 failed",
            ),
        ),
        CliTestSemanticsIntegrationTestCase(
            description="fails a macro mode test with a wrong expectation",
            sql_test_content=_FAILING_MACRO_TEST,
            macro_file_contents=_DOUBLED_MACRO,
            expected_exit_code=1,
            expected_output_fragments=(
                "FAIL",
                "macro doubles the value",
                "Results: 0 passed, 1 failed",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
@pytest.mark.integration
def test_given_comparison_semantics_project_when_running_test_command_then_it_reports_outcomes(
    test_case: CliTestSemanticsIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    write_sql_test_semantics_project(
        project_dir=tmp_path,
        sql_test_content=test_case.sql_test_content,
        macro_file_contents=test_case.macro_file_contents,
    )
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        exit_code: int = run_test(
            pipelines_root=tmp_path / "pipelines",
            project_dir=tmp_path,
            selectors=(),
            paths=(),
            verbose=True,
            client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=tmp_path),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()
    captured: CaptureResult[str] = capsys.readouterr()

    assert clickhouse_client.query("SELECT 1").result_rows == [(1,)]
    assert exit_code == test_case.expected_exit_code
    expected_output_fragment: str
    for expected_output_fragment in test_case.expected_output_fragments:
        assert expected_output_fragment in captured.out


@pytest.mark.parametrize(
    "test_case",
    [
        CliTestSemanticsIntegrationTestCase(
            description="persists the exact adapter input as the runtime test artifact",
            sql_test_content=_TERMINAL_CHAIN_TEST,
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=("run/tests/revenue_report/test_semantics.sql",),
        )
    ],
    ids=lambda case: case.description,
)
@pytest.mark.integration
def test_given_executed_sql_test_when_running_test_command_then_runtime_bytes_match_adapter_input(
    test_case: CliTestSemanticsIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    write_sql_test_semantics_project(
        project_dir=tmp_path,
        sql_test_content=test_case.sql_test_content,
        macro_file_contents=test_case.macro_file_contents,
    )
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    recording_client: RecordingDelegatingConnection = RecordingDelegatingConnection(managed_client)

    try:
        exit_code: int = run_test(
            pipelines_root=tmp_path / "pipelines",
            project_dir=tmp_path,
            selectors=(),
            paths=(),
            verbose=False,
            client=recording_client,
            loaded_project=_native.load_project_input_for_path(path=tmp_path),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()
    _ = capsys.readouterr()

    runtime_path: Path = tmp_path / "target" / test_case.expected_output_fragments[0]

    assert exit_code == test_case.expected_exit_code
    assert len(recording_client.query_statements) == 1
    assert runtime_path.read_bytes() == recording_client.query_statements[0].encode("utf-8")
    assert not (tmp_path / "target" / "compiled").exists()


@pytest.mark.parametrize(
    "test_case",
    [
        CliTestSemanticsIntegrationTestCase(
            description="runs every focused commerce demo model and macro test",
            sql_test_content="",
            macro_file_contents="",
            expected_exit_code=0,
            expected_output_fragments=(
                "order events derive stable region labels",
                "order facts emit additive values",
                "terminal kpis deduplicate facts before deriving rates",
                "commerce expressions preserve integer money and safe rates",
                "Results: 4 passed, 0 failed",
            ),
        )
    ],
    ids=lambda case: case.description,
)
@pytest.mark.integration
def test_given_orders_demo_example_when_running_test_command_then_every_authored_test_passes(
    test_case: CliTestSemanticsIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    copy_orders_demo(project_dir=project_dir)
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        exit_code: int = run_test(
            pipelines_root=project_dir / "pipelines",
            project_dir=project_dir,
            selectors=(),
            paths=(),
            verbose=True,
            client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()
    captured: CaptureResult[str] = capsys.readouterr()

    assert exit_code == test_case.expected_exit_code
    expected_output_fragment: str
    for expected_output_fragment in test_case.expected_output_fragments:
        assert expected_output_fragment in captured.out
