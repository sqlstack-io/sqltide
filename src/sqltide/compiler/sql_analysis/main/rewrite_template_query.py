"""Rewrite one placeholder-qualified template while preserving author bytes."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis.models import (
    SqlAggregateFacts,
    SqlNamedQuery,
    SqlQueryRewriteResult,
    SqlRelationRewrite,
)


def rewrite_template_query(
    *,
    template: str,
    dialect: str,
    database_placeholder: str,
    relation_rewrites: tuple[SqlRelationRewrite, ...] = (),
    predicate: str | None = None,
    prepend_ctes: tuple[SqlNamedQuery, ...] = (),
) -> SqlQueryRewriteResult:
    """Rewrite relations, predicate, and CTEs through byte-preserving text edits."""

    rewritten: str = _native.rewrite_template_relations(
        template, list(relation_rewrites), database_placeholder
    )
    if predicate is not None:
        rewritten = _native.append_template_predicate(rewritten, predicate)
    rewritten = _native.merge_template_ctes(rewritten, list(prepend_ctes))
    function_names, has_group_by = _native.sql_select_aggregate_facts(rewritten, dialect)
    facts: SqlAggregateFacts = _native.build_aggregate_facts(
        function_names=function_names, has_group_by=has_group_by, engine=""
    )
    return SqlQueryRewriteResult(
        query=rewritten,
        has_aggregate_semantics=facts.has_semantics,
    )
