"""Adapt the native ClickHouse-aware SQL scanner to SQL analysis models and exceptions."""

from __future__ import annotations

from collections.abc import Callable

import sqltide._native as _native
from sqltide.compiler.sql_analysis.constants import (
    SCAN_ERROR_KIND_MISSING_WITH,
    SCAN_ERROR_MISSING_WITH_SUFFIX,
)
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError, SqlMissingWithClauseError
from sqltide.compiler.sql_analysis.models import (
    SqlCommonTableExpression,
    SqlHeaderBlock,
    SqlNamedQuery,
    SqlReference,
    SqlRelationRewrite,
    SqlSourceSpan,
    SqlTopLevelCtes,
)
from sqltide.compiler.sql_analysis.types import RefType

type _NativeSpan = tuple[int, int, int, int, int, int]


def scan_error(error: _native.SqlScanError) -> SqlAnalysisError:
    """Convert one native scan failure into its SQL analysis exception."""

    kind: str
    message: str
    native_span: _NativeSpan | None
    kind, message, native_span = error.args
    span: SqlSourceSpan | None = None if native_span is None else SqlSourceSpan(*native_span)
    if kind == SCAN_ERROR_KIND_MISSING_WITH and span is not None:
        return SqlMissingWithClauseError(
            context=message.removesuffix(SCAN_ERROR_MISSING_WITH_SUFFIX), span=span
        )
    return SqlAnalysisError(message, span=span)


def run_native_scan[T](scan: Callable[[], T]) -> T:
    """Run one native scan, raising its failure as a SQL analysis exception."""

    try:
        return scan()
    except _native.SqlScanError as error:
        raise scan_error(error) from None


def extract_references_impl(sql: str) -> tuple[SqlReference, ...]:
    """Return logical relation references found outside comments and quoted text."""

    return tuple(
        SqlReference(
            name=name,
            relation_type=relation_type,
            ref_type=None if ref_type is None else RefType(ref_type),
            span=SqlSourceSpan(*span),
        )
        for name, relation_type, ref_type, span in run_native_scan(
            lambda: _native.extract_references(sql)
        )
    )


def split_header_blocks(*, sql: str, keyword: str) -> tuple[SqlHeaderBlock, ...]:
    """Split line-leading extension headers while ignoring SQL literals and comments."""

    return tuple(
        SqlHeaderBlock(start=start, body_start=body_start, header=header, body=body)
        for start, body_start, header, body in run_native_scan(
            lambda: _native.split_header_blocks(sql, keyword)
        )
    )


def extract_top_level_ctes_impl(*, sql: str, context: str) -> SqlTopLevelCtes:
    """Scan one leading WITH clause and return its CTEs plus the trailing statement."""

    ctes, trailing_sql, trailing_span = run_native_scan(
        lambda: _native.extract_top_level_ctes(sql, context)
    )
    return SqlTopLevelCtes(
        ctes=tuple(
            SqlCommonTableExpression(name=name, query=query, span=SqlSourceSpan(*span))
            for name, query, span in ctes
        ),
        trailing_sql=trailing_sql,
        trailing_span=SqlSourceSpan(*trailing_span),
    )


def try_consume_keyword_impl(*, sql: str, start: int, keyword: str) -> int | None:
    """Consume one case-insensitive keyword bounded by a non-identifier character."""

    return _native.try_consume_keyword(sql, start, keyword)


def find_matching_parenthesis(*, sql: str, open_index: int, context: str) -> int:
    """Find a matching close parenthesis while skipping comments and quoted text."""

    return run_native_scan(lambda: _native.find_matching_parenthesis(sql, open_index, context))


def find_next_unquoted_character(*, sql: str, character: str, start: int) -> int | None:
    """Find one character outside SQL comments and quoted text."""

    return run_native_scan(lambda: _native.find_next_unquoted_character(sql, character, start))


def find_next_macro_start(*, sql: str, start: int) -> int | None:
    """Find the next `@name(` macro call outside comments and quoted text."""

    return run_native_scan(lambda: _native.find_next_macro_start(sql, start))


def skip_trivia(*, sql: str, start: int) -> int:
    """Skip whitespace and SQL comments."""

    return run_native_scan(lambda: _native.skip_trivia(sql, start))


def normalized_statement_sql(sql: str) -> str:
    """Remove one terminal statement delimiter while preserving trailing trivia."""

    return run_native_scan(lambda: _native.normalized_statement_sql(sql))


def outer_projection_spans(sql: str) -> tuple[SqlSourceSpan, ...]:
    """Return trimmed source spans for the outer SELECT projection list."""

    return tuple(
        SqlSourceSpan(*span)
        for span in run_native_scan(lambda: _native.outer_projection_spans(sql))
    )


def outer_star_span(*, sql: str, projection_span: SqlSourceSpan) -> SqlSourceSpan:
    """Return the outer wildcard token span within one projection."""

    span: _NativeSpan | None = run_native_scan(
        lambda: _native.outer_star_span(sql, projection_span.start, projection_span.end)
    )
    return projection_span if span is None else SqlSourceSpan(*span)


def outer_double_colon_type(*, sql: str, projection_span: SqlSourceSpan) -> str | None:
    """Return the authored outer double-colon cast type, if present."""

    return run_native_scan(
        lambda: _native.outer_double_colon_type(sql, projection_span.start, projection_span.end)
    )


def rewrite_template_relations_impl(
    *,
    template: str,
    relation_rewrites: tuple[SqlRelationRewrite, ...],
    database_placeholder: str,
) -> str:
    """Replace placeholder-qualified relation tokens with target relations."""

    return _native.rewrite_template_relations(
        template,
        [
            (rewrite.source_name, rewrite.target_relation, rewrite.preserve_source_database)
            for rewrite in relation_rewrites
        ],
        database_placeholder,
    )


def append_template_predicate_impl(*, template: str, predicate: str) -> str:
    """Conjoin one predicate into the outer WHERE clause byte-preservingly."""

    return run_native_scan(lambda: _native.append_template_predicate(template, predicate))


def merge_template_ctes_impl(*, template: str, named_queries: tuple[SqlNamedQuery, ...]) -> str:
    """Prepend named CTEs into one template's leading WITH clause byte-preservingly."""

    return run_native_scan(
        lambda: _native.merge_template_ctes(
            template, [(named_query.name, named_query.query) for named_query in named_queries]
        )
    )
