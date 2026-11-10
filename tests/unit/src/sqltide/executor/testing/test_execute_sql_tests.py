import pytest

from sqltide._native import execute_sql_tests
from sqltide.executor.testing.exceptions import SqlTestExecutionError
from sqltide.executor.testing.models import SqlTestExecutionResult
from tests.unit.src.sqltide.executor.testing._test_types import (
    ComparisonDecodingErrorTestCase,
    ComparisonDecodingTestCase,
    SqlTestExecutionFailureTestCase,
)
from tests.unit.src.sqltide.executor.testing.helpers import (
    FailingFirstComparisonConnection,
    StubComparisonConnection,
    build_chain_test_case,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ComparisonDecodingTestCase(
            description="passes one chain target when the comparison returns no rows",
            target_model_names=("order_items",),
            assertion_names=(),
            rows=(),
            expected_labels=("order_items",),
            expected_passed=(True,),
            expected_missing_counts=(0,),
            expected_unexpected_counts=(0,),
        ),
        ComparisonDecodingTestCase(
            description="expands grouped multiplicity into repeated directional rows",
            target_model_names=("order_items",),
            assertion_names=(),
            rows=(
                (0, "missing", ["ord_001", "20"], 2),
                (0, "unexpected", ["ord_002", "30"], 1),
            ),
            expected_labels=("order_items",),
            expected_passed=(False,),
            expected_missing_counts=(2,),
            expected_unexpected_counts=(1,),
        ),
        ComparisonDecodingTestCase(
            description="routes rows to the chain and assertion cases by case index",
            target_model_names=("order_items",),
            assertion_names=("no_null_totals",),
            rows=((1, "unexpected", ["ord_003"], 3),),
            expected_labels=("order_items", "assert no_null_totals"),
            expected_passed=(True, False),
            expected_missing_counts=(0, 0),
            expected_unexpected_counts=(0, 3),
        ),
        ComparisonDecodingTestCase(
            description="keeps every chain target independent across case indexes",
            target_model_names=("order_items", "daily_revenue"),
            assertion_names=(),
            rows=((1, "missing", ["ord_001", "20"], 1),),
            expected_labels=("order_items", "daily_revenue"),
            expected_passed=(True, False),
            expected_missing_counts=(0, 1),
            expected_unexpected_counts=(0, 0),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_comparison_rows_when_executing_then_it_builds_directional_results(
    test_case: ComparisonDecodingTestCase,
) -> None:
    connection: StubComparisonConnection = StubComparisonConnection(rows=test_case.rows)

    results: tuple[SqlTestExecutionResult, ...] = execute_sql_tests(
        test_cases=(
            build_chain_test_case(
                target_model_names=test_case.target_model_names,
                assertion_names=test_case.assertion_names,
            ),
        ),
        client=connection,
    )

    assert tuple(target.target_model_name for target in results[0].target_results) == (
        test_case.expected_labels
    )
    assert tuple(target.passed for target in results[0].target_results) == (
        test_case.expected_passed
    )
    assert tuple(len(target.missing_rows) for target in results[0].target_results) == (
        test_case.expected_missing_counts
    )
    assert tuple(len(target.unexpected_rows) for target in results[0].target_results) == (
        test_case.expected_unexpected_counts
    )
    assert results[0].executed_sql == "SELECT 1"
    assert connection.statements == ["SELECT 1"]


@pytest.mark.parametrize(
    "test_case",
    [
        SqlTestExecutionFailureTestCase(
            description="records one warehouse error and continues with the remaining tests",
            expected_passed=(False, True),
            expected_error_fragment="warehouse rejected test SQL",
            expected_statements=("SELECT broken", "SELECT 1"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_one_warehouse_error_when_executing_suite_then_remaining_tests_still_run(
    test_case: SqlTestExecutionFailureTestCase,
) -> None:
    connection: FailingFirstComparisonConnection = FailingFirstComparisonConnection()

    results: tuple[SqlTestExecutionResult, ...] = execute_sql_tests(
        test_cases=(
            build_chain_test_case(query="SELECT broken"),
            build_chain_test_case(query="SELECT 1"),
        ),
        client=connection,
    )

    assert tuple(result.passed for result in results) == test_case.expected_passed
    assert test_case.expected_error_fragment in str(results[0].error_message)
    assert tuple(connection.statements) == test_case.expected_statements


@pytest.mark.parametrize(
    "test_case",
    [
        ComparisonDecodingErrorTestCase(
            description="rejects an adapter without set difference comparison support",
            rows=(),
            set_difference_comparison=False,
            expected_error_fragment="does not support SQL-test set-difference comparison",
        ),
        ComparisonDecodingErrorTestCase(
            description="rejects a comparison row with the wrong arity",
            rows=((0, "missing", ["ord_001"]),),
            set_difference_comparison=True,
            expected_error_fragment="returned an invalid comparison row",
        ),
        ComparisonDecodingErrorTestCase(
            description="rejects a case index outside the assembled comparison cases",
            rows=((5, "missing", ["ord_001"], 1),),
            set_difference_comparison=True,
            expected_error_fragment="returned an invalid comparison row",
        ),
        ComparisonDecodingErrorTestCase(
            description="rejects a non positive multiplicity",
            rows=((0, "missing", ["ord_001"], 0),),
            set_difference_comparison=True,
            expected_error_fragment="returned an invalid comparison row",
        ),
        ComparisonDecodingErrorTestCase(
            description="rejects an unsupported diff type",
            rows=((0, "sideways", ["ord_001"], 1),),
            set_difference_comparison=True,
            expected_error_fragment="returned unsupported diff type 'sideways'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_comparison_output_when_executing_then_it_raises_clear_errors(
    test_case: ComparisonDecodingErrorTestCase,
) -> None:
    connection: StubComparisonConnection = StubComparisonConnection(
        rows=test_case.rows,
        set_difference_comparison=test_case.set_difference_comparison,
    )

    with pytest.raises(SqlTestExecutionError, match=test_case.expected_error_fragment):
        execute_sql_tests(test_cases=(build_chain_test_case(),), client=connection)
