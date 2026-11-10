from pathlib import Path

import pytest

from sqltide._native import render_sql_test_results
from sqltide.executor.testing.models import SqlTestExecutionResult
from tests.unit.src.sqltide.cli.test._helpers._test_types import (
    RenderSqlTestResultsTestCase,
)
from tests.unit.src.sqltide.cli.test._helpers.helpers import build_render_sql_test_results


@pytest.mark.parametrize(
    "test_case",
    [
        RenderSqlTestResultsTestCase(
            description="renders side by side diff for keyed row changes",
            verbose=False,
            expected_fragments=(
                (
                    "FAIL  order_items  tests/order_events/test_line_total.sql  "
                    "[line total computes correctly]"
                ),
                "diff (1 row differs):",
                "columns: order_id, line_total",
                "row  state     order_id  line_total",
                "1    expected  ord_001   25.0",
                "1    actual    ord_001   20.0",
                "Results: 0 passed, 1 failed",
                "sqt test tests/order_events/test_line_total.sql",
            ),
        ),
        RenderSqlTestResultsTestCase(
            description=(
                "renders aligned missing and unexpected tables when rows do not share a key"
            ),
            verbose=False,
            expected_fragments=(
                "missing rows (1):",
                "columns: order_id, line_total, region",
                "order_id  line_total  region",
                "ord_001   25.0        us-east",
                "unexpected rows (1):",
                "ord_004   99.0        ap-south",
            ),
        ),
        RenderSqlTestResultsTestCase(
            description="truncates long sections when not verbose",
            verbose=False,
            expected_fragments=(
                "unexpected rows (12, showing first 10):",
                "(2 more rows not shown, run with --verbose to see all)",
            ),
        ),
        RenderSqlTestResultsTestCase(
            description="renders all rows when verbose",
            verbose=True,
            expected_fragments=(
                "unexpected rows (12):",
                "ord_011",
                "ord_012",
            ),
        ),
        RenderSqlTestResultsTestCase(
            description="renders blank lines between failed multi target sections",
            verbose=False,
            expected_fragments=(
                "target: order_items\n  diff (1 row differs):",
                "\n\n  target: daily_revenue\n  diff (1 row differs):",
            ),
        ),
        RenderSqlTestResultsTestCase(
            description="renders a warehouse execution error without target diffs",
            verbose=False,
            expected_fragments=(
                "ERROR  execution  tests/order_events/test_broken.sql",
                "error: warehouse rejected test SQL",
                "Results: 0 passed, 0 failed, 1 error",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_test_results_when_rendering_then_it_returns_expected_sections(
    test_case: RenderSqlTestResultsTestCase,
) -> None:
    results: tuple[SqlTestExecutionResult, ...] = build_render_sql_test_results(
        test_case.description
    )

    rendered: str = render_sql_test_results(
        results=results,
        project_dir=Path("/project"),
        verbose=test_case.verbose,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
