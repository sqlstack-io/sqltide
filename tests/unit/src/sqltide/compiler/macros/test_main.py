from pathlib import Path
from typing import cast

import pytest

from sqltide.compiler.macros.exceptions import MacroError
from sqltide.compiler.macros.main._build_macro_context import build_macro_context
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from tests.unit.src.sqltide.compiler.macros._test_types import (
    ExpandProjectSqlMacrosCollisionTestCase,
    ExpandProjectSqlMacrosErrorTestCase,
    ExpandProjectSqlMacrosTestCase,
    MacroDescriptionTestCase,
    MacroExecutionDiagnosticTestCase,
    MacroImportDiagnosticTestCase,
    MacroRegistrationTestCase,
    MacroRuntimeImmutabilityTestCase,
)
from tests.unit.src.sqltide.compiler.macros.helpers import (
    build_test_macro_runtime,
    expand_project_sql_macros,
    write_macro_file,
    write_project_file,
    write_sql_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ExpandProjectSqlMacrosTestCase(
            description="expands a simple sql macro from the project macros directory",
            macro_file_name="common_columns.py",
            macro_file_contents="""
        def replay_columns() -> str:
            return "_replay_partition AS _replay_partition"
        """,
            sql_body="SELECT @replay_columns() FROM source_table",
            expected_expanded_fragment=(
                "SELECT _replay_partition AS _replay_partition FROM source_table"
            ),
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports nested macro calls inside arguments",
            macro_file_name="mock_helpers.py",
            macro_file_contents="""
        def load_fixture(name: str) -> list[dict[str, object]]:
            if name != "orders":
                raise ValueError("unexpected fixture")
            return [
                {"order_id": "ord_001", "quantity": 2, "unit_price": 10.0},
                {"order_id": "ord_002", "quantity": None, "unit_price": 5.0},
            ]

        def mock_rows(rows: list[dict[str, object]]) -> str:
            selects: list[str] = []
            for row in rows:
                selects.append(
                    "SELECT "
                    + ", ".join(
                        "NULL AS {key}".format(key=key)
                        if value is None
                        else "'{value}' AS {key}".format(value=value, key=key)
                        if isinstance(value, str)
                        else "{value} AS {key}".format(value=value, key=key)
                        for key, value in row.items()
                    )
                )
            return " UNION ALL ".join(selects)
        """,
            sql_body='WITH __source__orders AS (@mock_rows(@load_fixture("orders"))) SELECT 1',
            expected_expanded_fragment=(
                "WITH __source__orders AS (SELECT 'ord_001' AS order_id, 2 AS quantity, "
                "10.0 AS unit_price "
                "UNION ALL SELECT 'ord_002' AS order_id, NULL AS quantity, "
                "5.0 AS unit_price) SELECT 1"
            ),
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports nested macro arguments alongside literal positional arguments",
            macro_file_name="arg_helpers.py",
            macro_file_contents="""
        def load_fixture(name: str) -> list[dict[str, object]]:
            return [{"order_id": "ord_001"}, {"order_id": "ord_002"}] if name == "orders" else []

        def wrap_rows(rows: list[dict[str, object]], key_column: str) -> str:
            return "SELECT " + ", ".join(row[key_column] for row in rows)
        """,
            sql_body='SELECT @wrap_rows(@load_fixture("orders"), "order_id")',
            expected_expanded_fragment="SELECT SELECT ord_001, ord_002",
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports named macro arguments with nested values",
            macro_file_name="kwarg_helpers.py",
            macro_file_contents="""
        def load_fixture(name: str) -> list[dict[str, object]]:
            return [{"order_id": "ord_001"}] if name == "orders" else []

        def wrap_rows(*, rows: list[dict[str, object]], key_column: str) -> str:
            return "SELECT " + rows[0][key_column]
        """,
            sql_body='SELECT @wrap_rows(rows=@load_fixture("orders"), key_column="order_id")',
            expected_expanded_fragment="SELECT SELECT ord_001",
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports mixed positional and named macro arguments",
            macro_file_name="mixed_arg_helpers.py",
            macro_file_contents="""
        def load_fixture(name: str) -> list[dict[str, object]]:
            return [{"order_id": "ord_001"}, {"order_id": "ord_002"}] if name == "orders" else []

        def limit_rows(rows: list[dict[str, object]], *, limit: int) -> str:
            return "SELECT " + ", ".join(row["order_id"] for row in rows[:limit])
        """,
            sql_body='SELECT @limit_rows(@load_fixture("orders"), limit=1)',
            expected_expanded_fragment="SELECT SELECT ord_001",
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports multiple nested macros in one call",
            macro_file_name="multi_nested_helpers.py",
            macro_file_contents="""
        def left_rows() -> list[str]:
            return ["ord_001", "ord_002"]

        def right_rows() -> list[str]:
            return ["ord_003"]

        def join_rows(left: list[str], right: list[str], separator: str) -> str:
            return "SELECT " + separator.join([*left, *right])
        """,
            sql_body='SELECT @join_rows(@left_rows(), @right_rows(), ", ")',
            expected_expanded_fragment="SELECT SELECT ord_001, ord_002, ord_003",
        ),
        ExpandProjectSqlMacrosTestCase(
            description="supports the complete nested Python literal matrix",
            macro_file_name="literal_helpers.py",
            macro_file_contents="""
        def render_literal(value: object) -> str:
            return repr(value)
        """,
            sql_body=(
                "SELECT @render_literal([True, False, None, -1, +2.5, ('value',), {'count': 3}])"
            ),
            expected_expanded_fragment=(
                "SELECT [True, False, None, -1, 2.5, ('value',), {'count': 3}]"
            ),
        ),
        ExpandProjectSqlMacrosTestCase(
            description="ignores calls in strings comments emails and quoted identifiers",
            macro_file_name="scanner_helpers.py",
            macro_file_contents="""
        def selected_value() -> str:
            return "expanded_value"
        """,
            sql_body=(
                "SELECT '@selected_value()' AS literal, `@selected_value()` AS quoted, "
                "owner@selected_value() AS email -- @selected_value()\n"
                "# @selected_value()\n"
                "/* @selected_value() */\n"
                ", @selected_value()"
            ),
            expected_expanded_fragment=(
                "SELECT '@selected_value()' AS literal, `@selected_value()` AS quoted, "
                "owner@selected_value() AS email -- @selected_value()\n"
                "# @selected_value()\n"
                "/* @selected_value() */\n"
                ", expanded_value"
            ),
        ),
        ExpandProjectSqlMacrosTestCase(
            description="allows macro-like text in returned SQL strings and comments",
            macro_file_name="returned_sql_helpers.py",
            macro_file_contents="""
        def returned_sql() -> str:
            return "'@not_a_call()' /* @also_not_a_call() */"
        """,
            sql_body="SELECT @returned_sql()",
            expected_expanded_fragment=("SELECT '@not_a_call()' /* @also_not_a_call() */"),
        ),
        ExpandProjectSqlMacrosTestCase(
            description="reads arguments as Python does, including NFKC names and lone surrogates",
            macro_file_name="literal_helpers.py",
            macro_file_contents="""
        def show(*values: object) -> str:
            return repr(values)
        """,
            sql_body="SELECT @show(Ｎone, Ｔrue, '\\ud800')",
            expected_expanded_fragment="SELECT (None, True, '\\ud800')",
        ),
        ExpandProjectSqlMacrosTestCase(
            description="injects typed immutable compiler context into the first ctx parameter",
            macro_file_name="context_helpers.py",
            macro_file_contents="""
        from sqltide.compiler.macros.models import MacroContext

        def context_value(ctx: MacroContext) -> str:
            return (
                f"{ctx.adapter_name}|{ctx.dialect}|{ctx.target_name}|{ctx.database}|"
                f"{ctx.schema}|{ctx.virtual_environments}|"
                f"{ctx.variables['nested']['value']}"
            )
        """,
            sql_body="SELECT '@context_value()' AS literal, @context_value()",
            expected_expanded_fragment=(
                "SELECT '@context_value()' AS literal, "
                "clickhouse|clickhouse|dev|analytics|analytics_schema|False|configured"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_project_macros_when_expanding_then_it_returns_expected_sql(
    test_case: ExpandProjectSqlMacrosTestCase,
    tmp_path: Path,
) -> None:
    write_project_file(tmp_path)
    write_macro_file(tmp_path, test_case.macro_file_name, test_case.macro_file_contents)
    sql_file_path: Path = write_sql_file(
        tmp_path, "pipelines/orders/order_items.sql", test_case.sql_body
    )

    expanded_sql: str = expand_project_sql_macros(
        project_dir=tmp_path,
        sql=test_case.sql_body,
        file_path=sql_file_path,
    )

    assert expanded_sql == test_case.expected_expanded_fragment


@pytest.mark.parametrize(
    "test_case",
    [
        ExpandProjectSqlMacrosErrorTestCase(
            description="raises a clear error for unknown macros",
            macro_file_name="common_columns.py",
            macro_file_contents="""
        def replay_columns() -> str:
            return "order_id"
        """,
            sql_body="SELECT @unknown_macro()",
            expected_error_fragment="Unknown macro '@unknown_macro'",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="raises a clear error for meta macro output",
            macro_file_name="common_columns.py",
            macro_file_contents="""
        def outer_macro() -> str:
            return "@inner_macro()"

        def inner_macro() -> str:
            return "order_id"
        """,
            sql_body="SELECT @outer_macro()",
            expected_error_fragment=(
                r"produced output containing unexpanded macro call '@inner_macro\('"
            ),
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="raises a clear error when a top level macro returns a non string",
            macro_file_name="bad_macro.py",
            macro_file_contents="""
        def bad_macro() -> list[str]:
            return ["ord_001"]
        """,
            sql_body="SELECT @bad_macro()",
            expected_error_fragment="must return a SQL string when used directly in SQL",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects bytes outside the locked literal matrix",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(b'bytes')",
            expected_error_fragment="unsupported literal value",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects comprehensions outside the locked literal matrix",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render([value for value in [1]])",
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects dictionary unpacking outside the locked literal matrix",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render({**{'count': 1}})",
            expected_error_fragment="must not use dictionary unpacking",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects non scalar dictionary keys outside the locked literal matrix",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render({('composite',): 1})",
            expected_error_fragment="unsupported dictionary key",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects unary booleans outside the locked literal matrix",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
            """,
            sql_body="SELECT @render(-True)",
            expected_error_fragment="unsupported unary value",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects repeated keyword arguments",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(*, value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(value=1, value=2)",
            expected_error_fragment="repeat keyword 'value'",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects arbitrary bare argument names",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(arbitrary_name)",
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects ordinary nested function calls",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(len([1]))",
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects attribute argument expressions",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(value.attribute)",
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects positional argument unpacking",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def render(value: object) -> str:
            return str(value)
        """,
            sql_body="SELECT @render(*[1])",
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects authored names that resemble nested placeholders",
            macro_file_name="literal_errors.py",
            macro_file_contents="""
        def nested() -> str:
            return "nested"

        def render(left: object, right: object) -> str:
            return f"{left}|{right}"
        """,
            sql_body=("SELECT @render(__sqltide_macro_arg_0, @nested())"),
            expected_error_fragment="must use only Python literals",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects imported public functions from macro registration",
            macro_file_name="imported_helpers.py",
            macro_file_contents="""
        from textwrap import dedent

        def owned_macro() -> str:
            return "owned"
        """,
            sql_body="SELECT @dedent()",
            expected_error_fragment="Unknown macro '@dedent'",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects an untyped ctx parameter",
            macro_file_name="context_errors.py",
            macro_file_contents="""
        def context_value(ctx) -> str:
            return str(ctx.adapter_name)
        """,
            sql_body="SELECT @context_value()",
            expected_error_fragment="must annotate ctx as MacroContext",
        ),
        ExpandProjectSqlMacrosErrorTestCase(
            description="rejects explicit context overrides",
            macro_file_name="context_errors.py",
            macro_file_contents="""
        from sqltide.compiler.macros.models import MacroContext

        def context_value(ctx: MacroContext) -> str:
            return str(ctx.adapter_name)
        """,
            sql_body="SELECT @context_value(ctx=None)",
            expected_error_fragment="reserves keyword 'ctx'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_project_macros_when_expanding_then_it_raises_clear_errors(
    test_case: ExpandProjectSqlMacrosErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_file(tmp_path)
    write_macro_file(tmp_path, test_case.macro_file_name, test_case.macro_file_contents)
    sql_file_path: Path = write_sql_file(
        tmp_path, "pipelines/orders/order_items.sql", test_case.sql_body
    )

    with pytest.raises(MacroError, match=test_case.expected_error_fragment) as error_info:
        expand_project_sql_macros(
            project_dir=tmp_path,
            sql=test_case.sql_body,
            file_path=sql_file_path,
        )

    assert error_info.value.diagnostic.location is not None
    assert error_info.value.diagnostic.location.path == sql_file_path


@pytest.mark.parametrize(
    "test_case",
    [
        ExpandProjectSqlMacrosCollisionTestCase(
            description="raises a clear error for colliding macro names",
            first_macro_file_name="common.py",
            first_macro_file_contents="""
            def replay_columns() -> str:
                return "order_id"
            """,
            second_macro_file_name="nested/other.py",
            second_macro_file_contents="""
            def replay_columns() -> str:
                return "customer_id"
            """,
            sql_body="SELECT @replay_columns()",
            expected_error_fragment="Macro name collision for 'replay_columns'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_colliding_macro_names_when_expanding_then_it_raises_clear_error(
    test_case: ExpandProjectSqlMacrosCollisionTestCase,
    tmp_path: Path,
) -> None:
    write_project_file(tmp_path)
    first_macro_path: Path = write_macro_file(
        tmp_path,
        test_case.first_macro_file_name,
        test_case.first_macro_file_contents,
    )
    second_macro_path: Path = write_macro_file(
        tmp_path,
        test_case.second_macro_file_name,
        test_case.second_macro_file_contents,
    )
    sql_file_path: Path = write_sql_file(
        tmp_path,
        "pipelines/orders/order_items.sql",
        test_case.sql_body,
    )

    with pytest.raises(MacroError, match=test_case.expected_error_fragment) as error_info:
        expand_project_sql_macros(
            project_dir=tmp_path,
            sql=test_case.sql_body,
            file_path=sql_file_path,
        )

    assert error_info.value.diagnostic.location is not None
    assert error_info.value.diagnostic.location.path == second_macro_path
    assert error_info.value.diagnostic.location.line == 1
    assert error_info.value.diagnostic.related_locations[0].location.path == first_macro_path
    assert error_info.value.diagnostic.related_locations[0].location.line == 1


@pytest.mark.parametrize(
    "test_case",
    [
        MacroRuntimeImmutabilityTestCase(
            description="deeply freezes the registry and nested context variables",
            variables={"nested": {"values": [1, "two"]}},
            expected_macro_names=("owned_macro",),
            expected_nested_values=(1, "two"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_loaded_macro_runtime_when_mutating_then_registry_and_context_are_immutable(
    test_case: MacroRuntimeImmutabilityTestCase,
    tmp_path: Path,
) -> None:
    write_macro_file(
        tmp_path,
        "owned.py",
        """
        def owned_macro() -> str:
            return "owned"
        """,
    )
    registry: MacroRegistry
    _default_context: MacroContext
    registry, _default_context = build_test_macro_runtime(tmp_path)
    context: MacroContext = build_macro_context(
        adapter_name="clickhouse",
        dialect="clickhouse",
        target_name="dev",
        database="analytics",
        schema=None,
        virtual_environments=True,
        variables=test_case.variables,
    )
    nested_variables: dict[str, object] = cast(dict[str, object], context.variables["nested"])

    with pytest.raises(TypeError):
        cast(dict[str, object], registry.macros)["other"] = object()
    with pytest.raises(TypeError):
        cast(dict[str, object], context.variables)["other"] = object()
    with pytest.raises(TypeError):
        nested_variables["other"] = object()

    assert tuple(registry.macros) == test_case.expected_macro_names
    assert nested_variables["values"] == test_case.expected_nested_values


@pytest.mark.parametrize(
    "test_case",
    [
        MacroExecutionDiagnosticTestCase(
            description="locates a failed SQL call and its macro definition",
            macro_file_contents="""
            def failed_macro() -> str:
                raise RuntimeError("deliberate macro failure")
            """,
            sql_body="SELECT\n  @failed_macro()",
            expected_error_fragment="deliberate macro failure",
            expected_sql_line=2,
            expected_sql_column=3,
            expected_definition_line=1,
        ),
        MacroExecutionDiagnosticTestCase(
            description="locates a nested failed call in the complete SQL source",
            macro_file_contents="""
            def outer(value: object) -> str:
                return str(value)

            def failed_macro() -> str:
                raise RuntimeError("nested deliberate failure")
            """,
            sql_body="SELECT\n  @outer(\n    @failed_macro()\n  )",
            expected_error_fragment="nested deliberate failure",
            expected_sql_line=3,
            expected_sql_column=5,
            expected_definition_line=4,
        ),
        MacroExecutionDiagnosticTestCase(
            description="locates unresolved returned output and its macro definition",
            macro_file_contents="""
            def failed_macro() -> str:
                return "@not_expanded()"
            """,
            sql_body="SELECT\n  @failed_macro()",
            expected_error_fragment="unexpanded macro call",
            expected_sql_line=2,
            expected_sql_column=3,
            expected_definition_line=1,
        ),
        MacroExecutionDiagnosticTestCase(
            description="locates an unterminated call and its macro definition",
            macro_file_contents="""
            def failed_macro() -> str:
                return "unused"
            """,
            sql_body="SELECT\n  @failed_macro(",
            expected_error_fragment="unclosed parenthesis",
            expected_sql_line=2,
            expected_sql_column=3,
            expected_definition_line=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failing_macro_call_when_expanding_then_diagnostic_locates_call_and_definition(
    test_case: MacroExecutionDiagnosticTestCase,
    tmp_path: Path,
) -> None:
    macro_file_path: Path = write_macro_file(
        tmp_path,
        "failed.py",
        test_case.macro_file_contents,
    )
    sql_file_path: Path = write_sql_file(
        tmp_path,
        "pipelines/orders/model.sql",
        test_case.sql_body,
    )

    with pytest.raises(MacroError, match=test_case.expected_error_fragment) as error_info:
        expand_project_sql_macros(
            project_dir=tmp_path,
            sql=test_case.sql_body,
            file_path=sql_file_path,
        )

    assert error_info.value.diagnostic.location is not None
    assert error_info.value.diagnostic.location.path == sql_file_path
    assert error_info.value.diagnostic.location.line == test_case.expected_sql_line
    assert error_info.value.diagnostic.location.column == test_case.expected_sql_column
    assert error_info.value.diagnostic.related_locations[0].location.path == macro_file_path
    assert (
        error_info.value.diagnostic.related_locations[0].location.line
        == test_case.expected_definition_line
    )


@pytest.mark.parametrize(
    "test_case",
    [
        MacroImportDiagnosticTestCase(
            description="locates a later failed macro module statement",
            macro_file_contents=(
                'marker = "loaded"\n\nraise RuntimeError("deliberate import failure")'
            ),
            expected_error_fragment="deliberate import failure",
            expected_definition_line=3,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_failing_macro_module_when_loading_then_diagnostic_locates_definition(
    test_case: MacroImportDiagnosticTestCase,
    tmp_path: Path,
) -> None:
    macro_file_path: Path = write_macro_file(
        tmp_path,
        "failed_import.py",
        test_case.macro_file_contents,
    )

    with pytest.raises(MacroError, match=test_case.expected_error_fragment) as error_info:
        _registry, _context = build_test_macro_runtime(tmp_path)

    assert error_info.value.diagnostic.location is not None
    assert error_info.value.diagnostic.location.path == macro_file_path
    assert error_info.value.diagnostic.location.line == test_case.expected_definition_line


@pytest.mark.parametrize(
    "test_case",
    [
        MacroRegistrationTestCase(
            description="registers only direct public functions with deterministic provenance",
            expected_macro_names=("alpha_macro", "zeta_macro"),
            expected_relative_path="macros/alpha.py",
            expected_source_fragment="def alpha_macro()",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_module_objects_when_loading_then_only_direct_public_functions_register(
    test_case: MacroRegistrationTestCase, tmp_path: Path
) -> None:
    write_macro_file(
        tmp_path,
        "zeta.py",
        """
        def zeta_macro() -> str:
            return "zeta"
        """,
    )
    alpha_path: Path = write_macro_file(
        tmp_path,
        "alpha.py",
        """
        class PublicClass:
            pass

        class CallableThing:
            def __call__(self) -> str:
                return "callable"

        def alpha_macro() -> str:
            return "alpha"

        alias_macro = alpha_macro
        lambda_macro = lambda: "lambda"
        callable_object = CallableThing()
        """,
    )

    registry: MacroRegistry
    _context: MacroContext
    registry, _context = build_test_macro_runtime(tmp_path)

    assert tuple(registry.macros) == test_case.expected_macro_names
    assert registry.macros["alpha_macro"].relative_path == Path(test_case.expected_relative_path)
    assert registry.macros["alpha_macro"].file_path == alpha_path
    assert test_case.expected_source_fragment in registry.macros["alpha_macro"].source


@pytest.mark.parametrize(
    "test_case",
    [
        MacroDescriptionTestCase(
            description="a documented macro carries its docstring first paragraph",
            macro_file_contents="""
        def documented_macro() -> str:
            \"\"\"Render the replay lineage columns.

            Extended details that should not be part of the description.
            \"\"\"
            return "1"
        """,
            macro_name="documented_macro",
            expected_description="Render the replay lineage columns.",
        ),
        MacroDescriptionTestCase(
            description="an undocumented macro has no description",
            macro_file_contents="""
        def bare_macro() -> str:
            return "1"
        """,
            macro_name="bare_macro",
            expected_description=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_macro_docstring_when_loading_then_description_is_first_paragraph(
    test_case: MacroDescriptionTestCase, tmp_path: Path
) -> None:
    write_macro_file(tmp_path, "documented.py", test_case.macro_file_contents)

    registry: MacroRegistry
    _context: MacroContext
    registry, _context = build_test_macro_runtime(tmp_path)

    assert registry.macros[test_case.macro_name].description == test_case.expected_description
