import pytest

from sqltide._native import rewrite_query
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import (
    SqlNamedQuery,
    SqlQueryRewriteResult,
    SqlRelationRewrite,
)
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    QueryPredicateRewriteTestCase,
    QueryRelationRewriteTestCase,
    QueryRewriteErrorTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        QueryRelationRewriteTestCase(
            description=(
                "rewrites only eligible physical relations while preserving aliases and CTEs"
            ),
            sql=(
                "WITH lookup AS (SELECT * FROM external.lookup) "
                "SELECT 'orders' AS literal_name, x.id FROM lookup AS l "
                "JOIN __sqltide_target_database__.orders AS x ON l.id = x.id "
                "JOIN external.orders AS e ON e.id = x.id"
            ),
            rewrites=(
                SqlRelationRewrite(
                    source_name="orders",
                    target_relation="orders__shadow",
                    source_databases=(None, "__sqltide_target_database__"),
                    preserve_source_database=True,
                ),
                SqlRelationRewrite(
                    source_name="lookup",
                    target_relation="lookup__shadow",
                    source_databases=(None, "__sqltide_target_database__"),
                    preserve_source_database=True,
                ),
            ),
            expected_fragments=(
                "FROM lookup AS l",
                "JOIN __sqltide_target_database__.orders__shadow AS x",
                "JOIN external.orders AS e",
                "'orders' AS literal_name",
            ),
            expected_absent_fragments=(
                "FROM lookup__shadow AS l",
                "external.orders__shadow",
            ),
        ),
        QueryRelationRewriteTestCase(
            description="rewrites nested CTE bodies without rewriting their visible identities",
            sql=(
                "WITH staged_orders AS ("
                "SELECT * FROM __sqltide_target_database__.orders AS source_rows"
                ") SELECT nested.id FROM (SELECT * FROM staged_orders) AS nested "
                "JOIN __sqltide_target_database__.lookup AS l ON nested.id = l.id"
            ),
            rewrites=(
                SqlRelationRewrite(
                    source_name="orders",
                    target_relation="orders__shadow",
                    source_databases=(None, "__sqltide_target_database__"),
                    preserve_source_database=True,
                ),
                SqlRelationRewrite(
                    source_name="lookup",
                    target_relation="lookup__shadow",
                    source_databases=(None, "__sqltide_target_database__"),
                    preserve_source_database=True,
                ),
            ),
            expected_fragments=(
                "FROM __sqltide_target_database__.orders__shadow AS source_rows",
                "FROM staged_orders",
                "JOIN __sqltide_target_database__.lookup__shadow AS l",
            ),
            expected_absent_fragments=(
                "FROM staged_orders__shadow",
                "__sqltide_target_database__.orders AS source_rows",
            ),
        ),
        QueryRelationRewriteTestCase(
            description="rewrites a physical relation shadowed by a nonrecursive CTE name",
            sql="WITH orders AS (SELECT * FROM orders) SELECT * FROM orders",
            rewrites=(
                SqlRelationRewrite(
                    source_name="orders",
                    target_relation="orders__shadow",
                ),
            ),
            expected_fragments=(
                "WITH orders AS (SELECT * FROM orders__shadow)",
                "SELECT * FROM orders",
            ),
            expected_absent_fragments=("WITH orders AS (SELECT * FROM orders)",),
        ),
        QueryRelationRewriteTestCase(
            description="preserves a recursive CTE identity inside its own body",
            sql="WITH RECURSIVE orders AS (SELECT * FROM orders) SELECT * FROM orders",
            rewrites=(
                SqlRelationRewrite(
                    source_name="orders",
                    target_relation="orders__shadow",
                ),
            ),
            expected_fragments=(
                "WITH RECURSIVE orders AS (SELECT * FROM orders)",
                "SELECT * FROM orders",
            ),
            expected_absent_fragments=("orders__shadow",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_relation_mapping_when_rewriting_query_then_only_eligible_relations_change(
    test_case: QueryRelationRewriteTestCase,
) -> None:
    result: SqlQueryRewriteResult = rewrite_query(
        sql=test_case.sql,
        dialect="clickhouse",
        relation_rewrites=test_case.rewrites,
    )

    assert all(fragment in result.query for fragment in test_case.expected_fragments)
    assert all(fragment not in result.query for fragment in test_case.expected_absent_fragments)


@pytest.mark.parametrize(
    "test_case",
    [
        QueryPredicateRewriteTestCase(
            description="parenthesizes authored disjunction before appending replay bounds",
            sql="SELECT * FROM raw_orders WHERE status = 'open' OR status = 'held'",
            predicate=("cursor >= CAST('10' AS UInt64) AND cursor <= CAST('20' AS UInt64)"),
            expected_query=(
                "SELECT * FROM raw_orders WHERE (status = 'open' OR status = 'held') "
                "AND (cursor >= CAST('10' AS UInt64) AND cursor <= CAST('20' AS UInt64))"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_where_when_appending_predicate_then_boolean_precedence_is_preserved(
    test_case: QueryPredicateRewriteTestCase,
) -> None:
    result: SqlQueryRewriteResult = rewrite_query(
        sql=test_case.sql,
        dialect="clickhouse",
        predicate=test_case.predicate,
    )

    assert result.query == test_case.expected_query


@pytest.mark.parametrize(
    "test_case",
    [
        QueryRewriteErrorTestCase(
            description="rejects replay CTE names that collide with authored CTEs",
            sql="WITH cutoff_offsets AS (SELECT 1 AS cutoff) SELECT * FROM cutoff_offsets",
            named_queries=(SqlNamedQuery(name="cutoff_offsets", query="SELECT 2 AS cutoff"),),
            expected_error_fragment="Replay CTE name collides",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_colliding_cte_when_rewriting_query_then_it_raises_structured_error(
    test_case: QueryRewriteErrorTestCase,
) -> None:
    with pytest.raises(SqlAnalysisError, match=test_case.expected_error_fragment):
        rewrite_query(
            sql=test_case.sql,
            dialect="clickhouse",
            prepend_ctes=test_case.named_queries,
        )
