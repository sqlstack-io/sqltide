"""Rewrite one SELECT through the mandatory SQL-analysis boundary."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis.models import (
    SqlAggregateFacts,
    SqlNamedQuery,
    SqlQueryRewriteResult,
    SqlRelationRewrite,
)


def rewrite_query(
    *,
    sql: str,
    dialect: str,
    relation_rewrites: tuple[SqlRelationRewrite, ...] = (),
    predicate: str | None = None,
    prepend_ctes: tuple[SqlNamedQuery, ...] = (),
) -> SqlQueryRewriteResult:
    """Rewrite relations, predicate, and CTEs in one parsed SELECT."""

    query, function_names, has_group_by = _native.sql_rewrite_query(
        sql,
        dialect,
        [
            (
                rewrite.source_name,
                rewrite.target_relation,
                None if rewrite.source_databases is None else list(rewrite.source_databases),
                rewrite.preserve_source_database,
            )
            for rewrite in relation_rewrites
        ],
        predicate,
        [(named_query.name, named_query.query) for named_query in prepend_ctes],
    )
    facts: SqlAggregateFacts = _native.build_aggregate_facts(
        function_names=function_names, has_group_by=has_group_by, engine=""
    )
    return SqlQueryRewriteResult(query=query, has_aggregate_semantics=facts.has_semantics)
