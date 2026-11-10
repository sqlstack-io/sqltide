from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.test_discovery.models import LoadedSqlTest, SqlTestModelPayload
from tests.unit.src.sqltide.compiler.macros.helpers import (
    build_test_macro_runtime,
    write_macro_file,
    write_project_file,
)
from tests.unit.src.sqltide.compiler.test_discovery._test_types import (
    DiscoverMultipleSqlTestsInFileTestCase,
    DiscoverSqlTestsErrorTestCase,
    DiscoverSqlTestsTestCase,
    DiscoverSqlTestsWithMacrosTestCase,
)
from tests.unit.src.sqltide.compiler.test_discovery.helpers import (
    model_payload,
    write_sql_test_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlTestsTestCase(
            description="discovers one SQL test with inferred target model and mocks",
            relative_file_path="order_events/test_line_total.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_target_model_names=("order_items",),
            expected_authored_cte_names=("__source__orders",),
            expected_mock_names=("orders",),
        ),
        DiscoverSqlTestsTestCase(
            description="discovers helper ctes alongside mocks in authored order",
            relative_file_path="order_events/test_helper_ctes.sql",
            file_contents="""
        TEST ();

        WITH
        helper_orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __source__orders AS (
          SELECT * FROM helper_orders
        ),
        expected_rows AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        ),
        __expected__order_items AS (
          SELECT * FROM expected_rows
        )
        SELECT 1
        """,
            expected_target_model_names=("order_items",),
            expected_authored_cte_names=("helper_orders", "__source__orders", "expected_rows"),
            expected_mock_names=("orders",),
        ),
        DiscoverSqlTestsTestCase(
            description="discovers multiple expected targets in one test block",
            relative_file_path="order_events/test_multi_expected.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        ),
        __expected__daily_revenue AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_target_model_names=("order_items", "daily_revenue"),
            expected_authored_cte_names=("__source__orders",),
            expected_mock_names=("orders",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_sql_test_files_when_discovering_then_it_returns_loaded_sql_tests(
    test_case: DiscoverSqlTestsTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / test_case.relative_file_path, test_case.file_contents)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(root=tests_root)

    payload: SqlTestModelPayload = model_payload(loaded_tests[0])

    assert len(loaded_tests) == 1
    assert tuple(cte.name.removeprefix("__expected__") for cte in payload.expected_targets) == (
        test_case.expected_target_model_names
    )
    assert (
        tuple(cte.name for cte in loaded_tests[0].authored_ctes)
        == test_case.expected_authored_cte_names
    )
    assert tuple(mock.name for mock in payload.mocks) == test_case.expected_mock_names
    assert payload.expected_targets


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlTestsErrorTestCase(
            description="rejects missing ceremonial select one",
            relative_file_path="order_events/test_invalid.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT order_id FROM __expected__order_items
        """,
            expected_error_fragment="must end with a ceremonial top-level `SELECT 1`",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects reserved helper cte names",
            relative_file_path="order_events/test_reserved_helper_name.sql",
            file_contents="""
        TEST ();

        WITH
        __actual AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT order_id FROM __actual
        ),
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1
        """,
            expected_error_fragment="uses reserved helper CTE name '__actual'",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects unnamed blocks in multi test files",
            relative_file_path="order_events/test_unnamed_multi.sql",
            file_contents="""
        TEST (name "first test");

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1;

        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_002' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_002' AS order_id
        )
        SELECT 1
        """,
            expected_error_fragment="every block must define a unique `name`",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects duplicate names in one file",
            relative_file_path="order_events/test_duplicate_names.sql",
            file_contents="""
        TEST (name "shared name");

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1;

        TEST (name "shared name");

        WITH
        __ref__order_items AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__daily_revenue AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1
        """,
            expected_error_fragment=r"defines duplicate TEST\(\) name 'shared name'",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects removed colon header syntax",
            relative_file_path="order_events/test_legacy_header.sql",
            file_contents='TEST (name: "legacy"); SELECT 1',
            expected_error_fragment="unexpected ':' after key 'name'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sql_test_files_when_discovering_then_it_raises_clear_errors(
    test_case: DiscoverSqlTestsErrorTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / test_case.relative_file_path, test_case.file_contents)

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        _native.discover_sql_tests(root=tests_root)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlTestsWithMacrosTestCase(
            description="expands project macros in sql test bodies",
            macro_file_contents="""
            def mock_order_rows() -> str:
                return "SELECT 'ord_001' AS order_id, 20.0 AS line_total"
            """,
            test_file_contents="""
            TEST ();

            WITH
            __ref__orders AS (
              @mock_order_rows()
            ),
            __expected__order_items AS (
              SELECT 'ord_001' AS order_id, 20.0 AS line_total
            )
            SELECT 1
            """,
            expected_mock_query_fragment="SELECT 'ord_001' AS order_id, 20.0 AS line_total",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sql_test_macros_when_discovering_then_it_expands_test_body(
    test_case: DiscoverSqlTestsWithMacrosTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_project_file(tmp_path)
    write_macro_file(tmp_path, "mock_helpers.py", test_case.macro_file_contents)
    write_sql_test_file(tests_root / "order_events/test_macro.sql", test_case.test_file_contents)
    macro_registry: MacroRegistry
    macro_context: MacroContext
    macro_registry, macro_context = build_test_macro_runtime(tmp_path)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(
        root=tests_root,
        macro_registry=macro_registry,
        macro_context=macro_context,
    )

    assert test_case.expected_mock_query_fragment in model_payload(loaded_tests[0]).mocks[0].query


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverMultipleSqlTestsInFileTestCase(
            description="discovers multiple sql tests from one file",
            file_contents="""
            TEST (name "line total computes correctly");

            WITH
            __source__orders AS (
              SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
            ),
            __expected__order_items AS (
              SELECT 'ord_001' AS order_id, 20.0 AS line_total
            )
            SELECT 1;

            TEST (name "daily revenue mirrors order items");

            WITH
            __ref__order_items AS (
              SELECT 'ord_001' AS order_id, 20.0 AS line_total
            ),
            __expected__daily_revenue AS (
              SELECT 'ord_001' AS order_id, 20.0 AS line_total
            )
            SELECT 1
            """,
            expected_target_model_names=("order_items", "daily_revenue"),
            expected_test_indexes=(1, 2),
            expected_names=(
                "line total computes correctly",
                "daily revenue mirrors order items",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multiple_sql_tests_in_one_file_when_discovering_then_it_returns_each_test(
    test_case: DiscoverMultipleSqlTestsInFileTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / "order_events/test_multi.sql", test_case.file_contents)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(root=tests_root)

    assert (
        tuple(
            model_payload(loaded_test).expected_targets[0].name.removeprefix("__expected__")
            for loaded_test in loaded_tests
        )
        == test_case.expected_target_model_names
    )
    assert (
        tuple(loaded_test.test_index for loaded_test in loaded_tests)
        == test_case.expected_test_indexes
    )
    assert tuple(loaded_test.name for loaded_test in loaded_tests) == test_case.expected_names
