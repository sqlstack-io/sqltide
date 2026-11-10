from pathlib import Path

import pytest

from sqltide.compiler.testing.models import SqlTestCase
from tests.unit.src.sqltide.compiler.testing._test_types import (
    BuildSqlTestCasesErrorTestCase,
    SqlTestChainClosureTestCase,
    SqlTestDeepChainTestCase,
    SqlTestWarningTestCase,
)
from tests.unit.src.sqltide.compiler.testing.helpers import (
    build_deep_chain_test_case,
    build_single_sql_test_case,
)


@pytest.mark.parametrize(
    "test_case",
    [
        SqlTestChainClosureTestCase(
            description="resolves a three model chain from one terminal expected target",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__revenue_report AS (
          SELECT 'ord_001' AS order_id, 20.0 AS reported_total
        )
        SELECT 1
        """,
            expected_assembled_cte_names=(
                "__source__orders",
                "__model__order_items",
                "__model__daily_revenue",
                "__model__revenue_report",
            ),
            expected_target_model_names=("revenue_report",),
        ),
        SqlTestChainClosureTestCase(
            description="assembles each diamond model once from one terminal expected target",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_summary AS (
          SELECT 'ord_001' AS order_id, 22.0 AS total_with_tax
        )
        SELECT 1
        """,
            expected_assembled_cte_names=(
                "__source__orders",
                "__model__order_items",
                "__model__daily_revenue",
                "__model__order_tax",
                "__model__order_summary",
            ),
            expected_target_model_names=("order_summary",),
        ),
        SqlTestChainClosureTestCase(
            description="cuts the inferred chain at an explicit ref mock",
            test_file_contents="""
        TEST ();

        WITH
        __ref__daily_revenue AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        ),
        __expected__revenue_report AS (
          SELECT 'ord_001' AS order_id, 20.0 AS reported_total
        )
        SELECT 1
        """,
            expected_assembled_cte_names=(
                "__ref__daily_revenue",
                "__model__revenue_report",
            ),
            expected_target_model_names=("revenue_report",),
        ),
        SqlTestChainClosureTestCase(
            description="orders multiple expected targets by assembled dependency order",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__daily_revenue AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_assembled_cte_names=(
                "__source__orders",
                "__model__order_items",
                "__model__daily_revenue",
            ),
            expected_target_model_names=("order_items", "daily_revenue"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_terminal_expected_target_when_assembling_then_it_resolves_the_real_chain(
    test_case: SqlTestChainClosureTestCase,
    tmp_path: Path,
) -> None:
    assembled: SqlTestCase = build_single_sql_test_case(
        tmp_path=tmp_path,
        test_file_contents=test_case.test_file_contents,
    )

    assembled_cte_names: tuple[str, ...] = tuple(
        cte_name for cte_name, _query in assembled.target_cases[-1].ctes
    )

    assert assembled_cte_names == test_case.expected_assembled_cte_names
    assert tuple(target.target_model_name for target in assembled.target_cases) == (
        test_case.expected_target_model_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SqlTestDeepChainTestCase(
            description="assembles a chain deeper than the Python recursion limit",
            model_count=1100,
            expected_terminal_cte_name="__model__model_1099",
            expected_assembled_count=1100,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deep_model_chain_when_resolving_then_it_uses_iterative_dependency_order(
    test_case: SqlTestDeepChainTestCase,
) -> None:
    assembled: SqlTestCase = build_deep_chain_test_case(model_count=test_case.model_count)
    cte_names: tuple[str, ...] = tuple(name for name, _query in assembled.target_cases[0].ctes)

    assert cte_names[-1] == test_case.expected_terminal_cte_name
    assert sum(name.startswith("__model__") for name in cte_names) == (
        test_case.expected_assembled_count
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SqlTestWarningTestCase(
            description="warns when an authored mock is never reached by the chain",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __ref__order_tax AS (
          SELECT 'ord_001' AS order_id, 2.0 AS tax_total
        ),
        __expected__daily_revenue AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_warnings=("never reaches mock '__ref__order_tax'",),
        ),
        SqlTestWarningTestCase(
            description="reports no warning when every authored mock is reached",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__daily_revenue AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_warnings=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_authored_mocks_when_assembling_then_unreachable_mocks_warn(
    test_case: SqlTestWarningTestCase,
    tmp_path: Path,
) -> None:
    assembled: SqlTestCase = build_single_sql_test_case(
        tmp_path=tmp_path,
        test_file_contents=test_case.test_file_contents,
    )

    assert len(assembled.warnings) == len(test_case.expected_warnings)
    expected_warning: str
    for expected_warning in test_case.expected_warnings:
        assert any(expected_warning in warning for warning in assembled.warnings)


@pytest.mark.parametrize(
    "test_case",
    [
        BuildSqlTestCasesErrorTestCase(
            description="rejects an unknown expected model",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__missing_model AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1
        """,
            expected_error_fragment="targets unknown model 'missing_model'",
        ),
        BuildSqlTestCasesErrorTestCase(
            description="rejects expected columns the target model does not produce",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 1 AS unknown_column
        )
        SELECT 1
        """,
            expected_error_fragment="expects columns not produced by 'order_items'",
        ),
        BuildSqlTestCasesErrorTestCase(
            description="rejects an expected query whose star cannot be resolved",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT * FROM some_unknown_relation
        )
        SELECT 1
        """,
            expected_error_fragment="cannot resolve __expected__ order_items columns",
        ),
        BuildSqlTestCasesErrorTestCase(
            description="rejects an unaliased expected projection",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001'
        )
        SELECT 1
        """,
            expected_error_fragment="must alias every projected column",
        ),
        BuildSqlTestCasesErrorTestCase(
            description="rejects mismatched set-operation branch arity",
            test_file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
          UNION ALL
          SELECT 'ord_002' AS order_id
        )
        SELECT 1
        """,
            expected_error_fragment="same column count in every set-operation branch",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_expected_targets_when_assembling_then_it_raises_clear_errors(
    test_case: BuildSqlTestCasesErrorTestCase,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        build_single_sql_test_case(
            tmp_path=tmp_path,
            test_file_contents=test_case.test_file_contents,
        )
