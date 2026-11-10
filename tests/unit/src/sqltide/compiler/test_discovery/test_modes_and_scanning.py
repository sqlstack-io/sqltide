from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.test_discovery.models import (
    LoadedSqlTest,
    SqlTestMacroPayload,
    SqlTestModelPayload,
)
from sqltide.compiler.test_discovery.types import SqlTestMode
from tests.unit.src.sqltide.compiler.macros.helpers import (
    build_test_macro_runtime,
    write_macro_file,
    write_project_file,
)
from tests.unit.src.sqltide.compiler.test_discovery._test_types import (
    DiscoverAssertionSqlTestTestCase,
    DiscoverMacroSqlTestTestCase,
    DiscoverSqlTestsErrorTestCase,
    MacroModeRestrictionTestCase,
    ScannedSqlTestCteTestCase,
)
from tests.unit.src.sqltide.compiler.test_discovery.helpers import (
    macro_payload,
    model_payload,
    write_sql_test_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverMacroSqlTestTestCase(
            description="discovers a macro mode test with helper ctes",
            file_contents="""
        TEST (mode macro, name "doubles the value");

        WITH
        input_values AS (
          SELECT 21 AS base_value
        ),
        __macro_actual__ AS (
          SELECT base_value * 2 AS doubled FROM input_values
        ),
        __macro_expected__ AS (
          SELECT 42 AS doubled
        )
        SELECT 1
        """,
            expected_name="doubles the value",
            expected_helper_cte_names=("input_values",),
            expected_actual_fragment="base_value * 2 AS doubled",
            expected_expected_fragment="SELECT 42 AS doubled",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_macro_mode_test_file_when_discovering_then_it_returns_a_macro_payload(
    test_case: DiscoverMacroSqlTestTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / "unit" / "test_macro.sql", test_case.file_contents)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(root=tests_root)
    payload: SqlTestMacroPayload = macro_payload(loaded_tests[0])

    assert loaded_tests[0].mode == SqlTestMode.MACRO
    assert loaded_tests[0].name == test_case.expected_name
    assert tuple(cte.name for cte in loaded_tests[0].authored_ctes) == (
        test_case.expected_helper_cte_names
    )
    assert test_case.expected_actual_fragment in payload.actual.query
    assert test_case.expected_expected_fragment in payload.expected.query
    assert _native.sql_test_target_names(loaded_test=loaded_tests[0]) == ()


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverAssertionSqlTestTestCase(
            description="discovers assertion ctes and their referenced model targets",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __assert__order_ids_are_not_null AS (
          SELECT order_id FROM __ref("order_items") WHERE order_id IS NULL
        )
        SELECT 1
        """,
            expected_assertion_cte_names=("__assert__order_ids_are_not_null",),
            expected_assertion_reference_names=("order_items",),
            expected_target_names=("order_items",),
        ),
        DiscoverAssertionSqlTestTestCase(
            description="combines expected targets and assertion targets for selection",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id
        ),
        __assert__downstream_is_clean AS (
          SELECT order_id FROM __ref("daily_revenue") WHERE order_id = ''
        )
        SELECT 1
        """,
            expected_assertion_cte_names=("__assert__downstream_is_clean",),
            expected_assertion_reference_names=("daily_revenue",),
            expected_target_names=("order_items", "daily_revenue"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_assertion_ctes_when_discovering_then_it_records_assertions_and_targets(
    test_case: DiscoverAssertionSqlTestTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / "unit" / "test_assert.sql", test_case.file_contents)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(root=tests_root)
    payload: SqlTestModelPayload = model_payload(loaded_tests[0])

    assert loaded_tests[0].mode == SqlTestMode.MODEL
    assert tuple(cte.name for cte in payload.assertions) == (test_case.expected_assertion_cte_names)
    assert payload.assertion_reference_names == test_case.expected_assertion_reference_names
    assert (
        _native.sql_test_target_names(loaded_test=loaded_tests[0])
        == test_case.expected_target_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ScannedSqlTestCteTestCase(
            description="ignores commas and parentheses inside strings and comments",
            file_contents="""
        TEST ();

        WITH
        -- a leading comment, with a comma
        __source__orders AS (
          /* block ), comment */
          SELECT 'a),b' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'a),b' AS order_id, 20.0 AS line_total -- trailing, comment
        )
        SELECT 1
        """,
            expected_cte_names=("__source__orders", "__expected__order_items"),
            expected_body_fragment="'a),b' AS order_id",
        ),
        ScannedSqlTestCteTestCase(
            description="accepts a recursive keyword and a column alias list",
            file_contents="""
        TEST ();

        WITH RECURSIVE
        __source__orders (order_id, quantity, unit_price) AS (
          SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id, 20.0 AS line_total
        )
        SELECT 1
        """,
            expected_cte_names=("__source__orders", "__expected__order_items"),
            expected_body_fragment="SELECT 'ord_001' AS order_id",
        ),
        ScannedSqlTestCteTestCase(
            description="accepts a terminated ceremonial select with a trailing comment",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 'ord_001' AS order_id
        ),
        __expected__order_items AS (
          SELECT 'ord_001' AS order_id
        )
        SELECT 1; -- done
        """,
            expected_cte_names=("__source__orders", "__expected__order_items"),
            expected_body_fragment="SELECT 'ord_001' AS order_id",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_lexically_tricky_sql_when_discovering_then_the_scanner_finds_every_cte(
    test_case: ScannedSqlTestCteTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(tests_root / "unit" / "test_scanner.sql", test_case.file_contents)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(root=tests_root)
    payload: SqlTestModelPayload = model_payload(loaded_tests[0])
    discovered_cte_names: tuple[str, ...] = (
        *(cte.name for cte in loaded_tests[0].authored_ctes),
        *(cte.name for cte in payload.expected_targets),
    )

    assert discovered_cte_names == test_case.expected_cte_names
    assert test_case.expected_body_fragment in payload.mocks[0].query


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlTestsErrorTestCase(
            description="rejects an unsupported udf mode",
            relative_file_path="unit/test_udf_mode.sql",
            file_contents="""
        TEST (mode udf, name "unsupported");

        WITH
        __udf_actual__ AS (
          SELECT 1 AS value
        ),
        __udf_expected__ AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment=r"must be one of: model, macro",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects an unsupported table function mode",
            relative_file_path="unit/test_table_fn_mode.sql",
            file_contents="""
        TEST (mode table_fn, name "unsupported");

        WITH
        __table_fn_actual__ AS (
          SELECT 1 AS value
        ),
        __table_fn_expected__ AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment=r"must be one of: model, macro",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects a macro test cte inside a model mode test",
            relative_file_path="unit/test_macro_in_model.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 1 AS value
        ),
        __macro_actual__ AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment=r"use TEST \(mode macro\)",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects a model test cte inside a macro mode test",
            relative_file_path="unit/test_model_in_macro.sql",
            file_contents="""
        TEST (mode macro, name "invalid");

        WITH
        __source__orders AS (
          SELECT 1 AS value
        ),
        __macro_actual__ AS (
          SELECT 1 AS value
        ),
        __macro_expected__ AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment="defines model-test CTE '__source__orders'",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects an unsupported seed mock cte",
            relative_file_path="unit/test_seed_mock.sql",
            file_contents="""
        TEST ();

        WITH
        __seed__regions AS (
          SELECT 1 AS value
        ),
        __expected__order_items AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment="does not support CTE '__seed__regions'",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects a model test without an expectation or assertion",
            relative_file_path="unit/test_no_checks.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment="must define at least one __expected__<model> or",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects duplicate cte names",
            relative_file_path="unit/test_duplicate_cte.sql",
            file_contents="""
        TEST ();

        WITH
        __source__orders AS (
          SELECT 1 AS value
        ),
        __source__orders AS (
          SELECT 2 AS value
        ),
        __expected__order_items AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment="defines duplicate CTE '__source__orders'",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects an unsupported header key",
            relative_file_path="unit/test_unsupported_key.sql",
            file_contents="""
        TEST (tags ["finance"]);

        WITH
        __source__orders AS (
          SELECT 1 AS value
        ),
        __expected__order_items AS (
          SELECT 1 AS value
        )
        SELECT 1
        """,
            expected_error_fragment="only supports `name` and `mode`",
        ),
        DiscoverSqlTestsErrorTestCase(
            description="rejects a test body without a with clause",
            relative_file_path="unit/test_no_with.sql",
            file_contents="""
        TEST ();

        SELECT 1
        """,
            expected_error_fragment="must declare mock CTEs and one",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unsupported_test_shapes_when_discovering_then_it_raises_clear_errors(
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
        MacroModeRestrictionTestCase(
            description="rejects macro calls in a macro mode helper cte",
            macro_file_contents="""
            def doubled(value: str) -> str:
                return f"({value}) * 2"
            """,
            file_contents="""
            TEST (mode macro, name "helper calls macro");

            WITH
            input_values AS (
              SELECT @doubled('21') AS base_value
            ),
            __macro_actual__ AS (
              SELECT base_value AS doubled FROM input_values
            ),
            __macro_expected__ AS (
              SELECT 42 AS doubled
            )
            SELECT 1
            """,
            expected_error_fragment=(
                r"helper CTE 'input_values' must not call macros; "
                r"call macros only in __macro_actual__"
            ),
        ),
        MacroModeRestrictionTestCase(
            description="rejects macro calls in the macro expected cte",
            macro_file_contents="""
            def doubled(value: str) -> str:
                return f"({value}) * 2"
            """,
            file_contents="""
            TEST (mode macro, name "expected calls macro");

            WITH
            __macro_actual__ AS (
              SELECT @doubled('21') AS doubled
            ),
            __macro_expected__ AS (
              SELECT @doubled('21') AS doubled
            )
            SELECT 1
            """,
            expected_error_fragment=r"CTE __macro_expected__ must not call macros",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_macro_mode_restrictions_when_discovering_then_it_rejects_extra_macro_calls(
    test_case: MacroModeRestrictionTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_project_file(tmp_path)
    write_macro_file(tmp_path, "macro_helpers.py", test_case.macro_file_contents)
    write_sql_test_file(tests_root / "unit" / "test_macro_mode.sql", test_case.file_contents)
    macro_registry: MacroRegistry
    macro_context: MacroContext
    macro_registry, macro_context = build_test_macro_runtime(tmp_path)

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        _native.discover_sql_tests(
            root=tests_root,
            macro_registry=macro_registry,
            macro_context=macro_context,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverMacroSqlTestTestCase(
            description="expands macros only inside the macro actual cte",
            file_contents="""
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
            """,
            expected_name="doubles the value",
            expected_helper_cte_names=("input_values",),
            expected_actual_fragment="(base_value) * 2 AS doubled",
            expected_expected_fragment="SELECT 42 AS doubled",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_macro_actual_cte_when_discovering_then_it_expands_through_the_registry(
    test_case: DiscoverMacroSqlTestTestCase,
    tmp_path: Path,
) -> None:
    tests_root: Path = tmp_path / "tests"
    write_project_file(tmp_path)
    write_macro_file(
        tmp_path,
        "macro_helpers.py",
        """
        def doubled(value: str) -> str:
            return f"({value}) * 2"
        """,
    )
    write_sql_test_file(tests_root / "unit" / "test_macro_expand.sql", test_case.file_contents)
    macro_registry: MacroRegistry
    macro_context: MacroContext
    macro_registry, macro_context = build_test_macro_runtime(tmp_path)

    loaded_tests: list[LoadedSqlTest] = _native.discover_sql_tests(
        root=tests_root,
        macro_registry=macro_registry,
        macro_context=macro_context,
    )
    payload: SqlTestMacroPayload = macro_payload(loaded_tests[0])

    assert test_case.expected_actual_fragment in payload.actual.query
    assert test_case.expected_expected_fragment in payload.expected.query
