import pytest

from sqltide._native import rewrite_template_query
from sqltide.adapter.constants import ADAPTER_DATABASE_PLACEHOLDER
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import (
    SqlNamedQuery,
    SqlQueryRewriteResult,
    SqlRelationRewrite,
)
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    TemplateRewriteErrorTestCase,
    TemplateRewriteTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        TemplateRewriteTestCase(
            description="preserves author bytes while rewriting wrapping and prepending",
            template=(
                "SELECT order_id, startsWith(topic, 'races') AS is_race\n"
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.raw__orders\n"
                "WHERE status = 'open' OR status = 'held'"
            ),
            relation_rewrites=(
                SqlRelationRewrite(
                    source_name="raw__orders",
                    target_relation="orders_demo.raw__orders",
                ),
            ),
            predicate="_replay_cursor <= 20",
            prepend_ctes=(SqlNamedQuery(name="replay_cutoff", query="SELECT 20 AS cutoff_value"),),
            expected_query=(
                "WITH replay_cutoff AS (\nSELECT 20 AS cutoff_value\n)\n"
                "SELECT order_id, startsWith(topic, 'races') AS is_race\n"
                "FROM orders_demo.raw__orders\n"
                "WHERE (status = 'open' OR status = 'held') AND (_replay_cursor <= 20)"
            ),
            expected_aggregate_semantics=False,
        ),
        TemplateRewriteTestCase(
            description="injects one missing WHERE clause ahead of grouping boundaries",
            template=(
                "SELECT topic, count() AS event_count\n"
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.raw__orders\n"
                "GROUP BY topic"
            ),
            relation_rewrites=(
                SqlRelationRewrite(
                    source_name="raw__orders",
                    target_relation="orders_demo.raw__orders",
                ),
            ),
            predicate="_replay_cursor <= 20",
            prepend_ctes=(),
            expected_query=(
                "SELECT topic, count() AS event_count\n"
                "FROM orders_demo.raw__orders\n"
                "WHERE (_replay_cursor <= 20)\n"
                "GROUP BY topic"
            ),
            expected_aggregate_semantics=True,
        ),
        TemplateRewriteTestCase(
            description="replaces only boundary-safe placeholder-qualified relation tokens",
            template=(
                "SELECT a.x, b.x "
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl__a AS a "
                f"JOIN {ADAPTER_DATABASE_PLACEHOLDER}.tbl__a_extra AS b ON a.x = b.x"
            ),
            relation_rewrites=(
                SqlRelationRewrite(source_name="tbl__a", target_relation="db1.tbl__a__dep"),
            ),
            predicate=None,
            prepend_ctes=(),
            expected_query=(
                "SELECT a.x, b.x "
                "FROM db1.tbl__a__dep AS a "
                f"JOIN {ADAPTER_DATABASE_PLACEHOLDER}.tbl__a_extra AS b ON a.x = b.x"
            ),
            expected_aggregate_semantics=False,
        ),
        TemplateRewriteTestCase(
            description="keeps the placeholder database for shadow physical renames",
            template=(f"SELECT x FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl__orders GROUP BY x"),
            relation_rewrites=(
                SqlRelationRewrite(
                    source_name="tbl__orders",
                    target_relation="tbl__orders__dep",
                    source_databases=(None, ADAPTER_DATABASE_PLACEHOLDER),
                    preserve_source_database=True,
                ),
            ),
            predicate=None,
            prepend_ctes=(),
            expected_query=(
                f"SELECT x FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl__orders__dep GROUP BY x"
            ),
            expected_aggregate_semantics=True,
        ),
        TemplateRewriteTestCase(
            description="merges prepended CTEs into one authored leading WITH clause",
            template=("WITH staged AS (SELECT 1 AS x)\nSELECT x FROM staged"),
            relation_rewrites=(),
            predicate=None,
            prepend_ctes=(SqlNamedQuery(name="cutoff", query="SELECT 2 AS y"),),
            expected_query=(
                "WITH cutoff AS (\nSELECT 2 AS y\n), staged AS (SELECT 1 AS x)\n"
                "SELECT x FROM staged"
            ),
            expected_aggregate_semantics=False,
        ),
        TemplateRewriteTestCase(
            description="merges prepended CTEs after one leading comment before WITH",
            template=("-- authored comment\nWITH staged AS (SELECT 1 AS x)\nSELECT x FROM staged"),
            relation_rewrites=(),
            predicate=None,
            prepend_ctes=(SqlNamedQuery(name="cutoff", query="SELECT 2 AS y"),),
            expected_query=(
                "-- authored comment\n"
                "WITH cutoff AS (\nSELECT 2 AS y\n), staged AS (SELECT 1 AS x)\n"
                "SELECT x FROM staged"
            ),
            expected_aggregate_semantics=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_placeholder_template_when_rewriting_then_author_bytes_are_preserved(
    test_case: TemplateRewriteTestCase,
) -> None:
    result: SqlQueryRewriteResult = rewrite_template_query(
        template=test_case.template,
        dialect="clickhouse",
        database_placeholder=ADAPTER_DATABASE_PLACEHOLDER,
        relation_rewrites=test_case.relation_rewrites,
        predicate=test_case.predicate,
        prepend_ctes=test_case.prepend_ctes,
    )

    assert result.query == test_case.expected_query
    assert result.has_aggregate_semantics is test_case.expected_aggregate_semantics


@pytest.mark.parametrize(
    "test_case",
    [
        TemplateRewriteErrorTestCase(
            description="rejects one non-SELECT template statement",
            template="INSERT INTO t SELECT 1",
            expected_error_fragment="expects a SELECT query",
        ),
        TemplateRewriteErrorTestCase(
            description="rejects one unparseable template statement",
            template="SELECT (((",
            expected_error_fragment="could not be parsed",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_template_when_rewriting_then_it_raises_an_error(
    test_case: TemplateRewriteErrorTestCase,
) -> None:
    with pytest.raises(SqlAnalysisError, match=test_case.expected_error_fragment):
        rewrite_template_query(
            template=test_case.template,
            dialect="clickhouse",
            database_placeholder=ADAPTER_DATABASE_PLACEHOLDER,
        )
