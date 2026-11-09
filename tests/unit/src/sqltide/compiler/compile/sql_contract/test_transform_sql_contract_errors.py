import pytest

from sqltide.compiler.compile.exceptions import (
    TransformSqlDuplicateAliasError,
    TransformSqlMultipleStatementsError,
    TransformSqlStarProjectionError,
    TransformSqlTopLevelSetOperationError,
    TransformSqlUntypedProjectionError,
)
from tests.unit.src.sqltide.compiler.compile.helpers import (
    build_inline_sql_pipeline,
    compile_test_pipeline,
)
from tests.unit.src.sqltide.compiler.compile.sql_contract._test_types import (
    DeriveTransformOutputColumnsErrorTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeriveTransformOutputColumnsErrorTestCase(
            description="rejects multiple statements",
            query="SELECT CAST(order_id AS UInt64) AS order_id FROM staged; SELECT 1",
            expected_error_type=TransformSqlMultipleStatementsError,
            expected_message_fragments=("exactly one SQL statement", "found 2"),
            expected_error_attributes={"statement_count": 2},
        ),
        DeriveTransformOutputColumnsErrorTestCase(
            description="rejects a top level union",
            query="""
            SELECT CAST(order_id AS UInt64) AS order_id FROM first_source
            UNION ALL
            SELECT CAST(order_id AS UInt64) AS order_id FROM second_source
        """,
            expected_error_type=TransformSqlTopLevelSetOperationError,
            expected_message_fragments=(
                "outermost SELECT",
                "UNION or UNION ALL",
                "expr::Type AS name",
            ),
            expected_error_attributes={},
        ),
        DeriveTransformOutputColumnsErrorTestCase(
            description="rejects wildcard projections in the outermost select",
            query="SELECT *, CAST(created_at AS DateTime64(3)) AS created_at FROM staged",
            expected_error_type=TransformSqlStarProjectionError,
            expected_message_fragments=(
                "column 1",
                "Wildcard projections",
                "expr::Type AS name",
            ),
            expected_error_attributes={"column_index": 1},
        ),
        DeriveTransformOutputColumnsErrorTestCase(
            description="rejects untyped projections even when aliased",
            query="SELECT order_id AS order_id FROM staged",
            expected_error_type=TransformSqlUntypedProjectionError,
            expected_message_fragments=(
                "column 1",
                "expr::Type AS name",
                "order_id AS order_id",
            ),
            expected_error_attributes={"column_index": 1},
        ),
        DeriveTransformOutputColumnsErrorTestCase(
            description="rejects duplicate aliases",
            query="""
            SELECT
                CAST(first_id AS UInt64) AS order_id,
                CAST(second_id AS UInt64) AS order_id
            FROM staged
        """,
            expected_error_type=TransformSqlDuplicateAliasError,
            expected_message_fragments=("Duplicate outermost SELECT alias 'order_id'",),
            expected_error_attributes={"alias": "order_id"},
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_transform_sql_when_compiling_then_it_raises_a_clear_custom_error(
    test_case: DeriveTransformOutputColumnsErrorTestCase,
) -> None:
    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(build_inline_sql_pipeline(test_case.query))

    error: Exception = error_info.value
    for attribute_name, expected_value in test_case.expected_error_attributes.items():
        assert getattr(error, attribute_name) == expected_value

    error_message: str = str(error)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message
