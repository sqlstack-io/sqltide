import pytest

import sqltide._native as _native
from sqltide._native import SqlReferenceRewriter
from sqltide.compiler.compile.exceptions import PipelineCompileError
from tests.unit.src.sqltide.compiler.compile._helpers.refs._test_types import (
    ReplaceRefsErrorTestCase,
    ReplaceRefsTestCase,
)
from tests.unit.src.sqltide.compiler.compile._helpers.refs.helpers import build_ref_resolver


@pytest.mark.parametrize(
    "test_case",
    [
        ReplaceRefsTestCase(
            description="replaces refs in plain and nested query positions",
            sql=(
                'SELECT * FROM __source("orders") WHERE customer_id IN '
                '(SELECT customer_id FROM __ref("customers", ref_type="reference"))'
            ),
            resolver=build_ref_resolver(),
            expected_sql_fragments=("FROM raw__orders", "FROM tbl__customers"),
            expected_absent_fragments=(
                '__source("orders")',
                '__ref("customers", ref_type = "reference")',
            ),
        ),
        ReplaceRefsTestCase(
            description="does not replace ref text inside string literals",
            sql='SELECT \'__source("orders")\' AS label FROM __source("orders")',
            resolver=build_ref_resolver(),
            expected_sql_fragments=(
                "SELECT '__source(\"orders\")' AS label",
                "FROM raw__orders",
            ),
            expected_absent_fragments=("'raw__orders' AS label",),
        ),
        ReplaceRefsTestCase(
            description="replaces refs that declare ref_type",
            sql=(
                'SELECT * FROM __source("orders") LEFT JOIN '
                '__ref("customers", ref_type="reference") USING customer_id'
            ),
            resolver=build_ref_resolver(),
            expected_sql_fragments=("FROM raw__orders", "JOIN tbl__customers"),
            expected_absent_fragments=(
                '__source("orders")',
                '__ref("customers", ref_type = "reference")',
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_when_replacing_refs_then_it_rewrites_only_real_ref_calls(
    test_case: ReplaceRefsTestCase,
) -> None:
    replaced_sql: str = _native.replace_refs(
        sql=test_case.sql,
        resolver=test_case.resolver,
        rewriter=SqlReferenceRewriter(dialect="clickhouse"),
    )

    for expected_sql_fragment in test_case.expected_sql_fragments:
        assert expected_sql_fragment in replaced_sql
    for expected_absent_fragment in test_case.expected_absent_fragments:
        assert expected_absent_fragment not in replaced_sql


@pytest.mark.parametrize(
    "test_case",
    [
        ReplaceRefsErrorTestCase(
            description="raises a structured compile error when ref is unresolved",
            sql='SELECT * FROM __source("missing")',
            resolver=build_ref_resolver(),
            expected_error_type=PipelineCompileError,
            expected_error_fragment="Unresolved ref: missing",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unresolved_ref_when_replacing_then_it_raises_expected_error(
    test_case: ReplaceRefsErrorTestCase,
) -> None:
    with pytest.raises(test_case.expected_error_type, match=test_case.expected_error_fragment):
        _native.replace_refs(
            sql=test_case.sql,
            resolver=test_case.resolver,
            rewriter=SqlReferenceRewriter(dialect="clickhouse"),
        )
