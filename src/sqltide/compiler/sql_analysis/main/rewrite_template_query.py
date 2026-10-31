"""Rewrite one placeholder-qualified template while preserving author bytes."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.aggregates import build_aggregate_facts
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql
from sqltide.compiler.sql_analysis._helpers.scanning import (
    append_template_predicate_impl,
    merge_template_ctes_impl,
    rewrite_template_relations_impl,
)
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

    rewritten: str = rewrite_template_relations_impl(
        template=template,
        relation_rewrites=relation_rewrites,
        database_placeholder=database_placeholder,
    )
    if predicate is not None:
        rewritten = append_template_predicate_impl(template=rewritten, predicate=predicate)
    rewritten = merge_template_ctes_impl(template=rewritten, named_queries=prepend_ctes)
    function_names, has_group_by = native_sql(
        lambda: _native.sql_select_aggregate_facts(rewritten, dialect)
    )
    facts: SqlAggregateFacts = build_aggregate_facts(
        function_names=function_names, has_group_by=has_group_by, engine=""
    )
    return SqlQueryRewriteResult(
        query=rewritten,
        has_aggregate_semantics=facts.has_semantics,
    )
