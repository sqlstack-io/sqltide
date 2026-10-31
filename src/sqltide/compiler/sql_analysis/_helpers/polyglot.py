"""Adapt the native Polyglot engine to SQL analysis trees, models and exceptions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sqltide._native as _native
from sqltide.compiler.sql_analysis.exceptions import (
    SqlAnalysisError,
    SqlDuplicateAliasError,
    SqlQueryShapeError,
    SqlStarProjectionError,
    SqlStatementCountError,
    SqlUntypedProjectionError,
)
from sqltide.compiler.sql_analysis.models import SqlReference, SqlSourceSpan
from sqltide.compiler.sql_analysis.types import NativeReference, SqlTreeErrorKind

type _NativeSpan = tuple[int, int, int, int, int, int]


def normalize_data_type_sql(*, sql: str, dialect: str) -> str:
    """Generate one data type with canonical ClickHouse type vocabulary."""

    try:
        return _native.sql_normalize_data_type(sql, dialect)
    except ValueError as error:
        raise SqlAnalysisError(str(error)) from None


def analyze_query_facts(*, sql: str, dialect: str) -> dict[str, Any]:
    """Return one private compact Polyglot query-analysis payload."""

    try:
        result: Any = _native.polyglot_analyze_query(sql, dialect)
    except Exception as error:
        raise SqlAnalysisError(f"SQL could not be analyzed with Polyglot: {error}") from None
    if not isinstance(result, dict):
        raise SqlAnalysisError("Polyglot query analysis did not return a mapping")
    return result


def native_sql[T](call: Callable[[], T]) -> T:
    """Run one native SQL analysis call, raising its SQL analysis exception on failure."""

    try:
        return call()
    except _native.SqlTreeError as error:
        raise sql_tree_error(error) from None


def native_references(references: tuple[SqlReference, ...]) -> list[NativeReference]:
    """Flatten scanned references into the tuples native resolution validates against."""

    return [
        (
            str(reference.relation_type),
            reference.name,
            None if reference.ref_type is None else str(reference.ref_type),
            reference.span.start,
            reference.span.end,
        )
        for reference in references
    ]


def sql_tree_error(error: _native.SqlTreeError) -> SqlAnalysisError:
    """Convert one native tree-analysis failure into its SQL analysis exception."""

    kind: SqlTreeErrorKind = SqlTreeErrorKind(error.args[0])
    details: tuple[Any, ...] = error.args[1:]
    if kind is SqlTreeErrorKind.STATEMENT_COUNT:
        return SqlStatementCountError(details[0])
    if kind is SqlTreeErrorKind.QUERY_SHAPE:
        return SqlQueryShapeError(
            statement_type=details[0], statement_sql=details[1], is_set_operation=details[2]
        )
    if kind is SqlTreeErrorKind.UNTYPED_PROJECTION:
        return SqlUntypedProjectionError(
            column_index=details[0], projection_sql=details[1], span=_span(details[2])
        )
    if kind is SqlTreeErrorKind.STAR_PROJECTION:
        return SqlStarProjectionError(column_index=details[0], span=_span(details[1]))
    if kind is SqlTreeErrorKind.DUPLICATE_ALIAS:
        return SqlDuplicateAliasError(alias=details[0], span=_span(details[1]))
    return SqlAnalysisError(details[0])


def _span(native_span: _NativeSpan) -> SqlSourceSpan:
    return SqlSourceSpan(*native_span)
