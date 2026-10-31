"""Adapt the native Polyglot engine to SQL analysis trees, models and exceptions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sqltide._native as _native
from sqltide.compiler.sql_analysis.constants import (
    CLICKHOUSE_AGGREGATE_STATE_TYPE_NAMES,
    CLICKHOUSE_NAMED_FIELD_TYPE_NAMES,
    SQL_ARGUMENT_SEPARATOR,
    SQL_CLOSE_PARENTHESIS,
    SQL_ESCAPE_CHARACTER,
    SQL_IDENTIFIER_PREFIX,
    SQL_OPEN_PARENTHESIS,
    SQL_QUOTE_CHARACTERS,
)
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

_CANONICAL_TYPE_NAMES: dict[str, str] = {
    **{f"int{bits}": f"Int{bits}" for bits in (8, 16, 32, 64, 128, 256)},
    **{f"uint{bits}": f"UInt{bits}" for bits in (8, 16, 32, 64, 128, 256)},
    **{f"float{bits}": f"Float{bits}" for bits in (32, 64)},
    "aggregatefunction": "AggregateFunction",
    "array": "Array",
    "bool": "Bool",
    "date": "Date",
    "date32": "Date32",
    "datetime": "DateTime",
    "datetime64": "DateTime64",
    "decimal": "Decimal",
    "decimal32": "Decimal32",
    "decimal64": "Decimal64",
    "decimal128": "Decimal128",
    "decimal256": "Decimal256",
    "dynamic": "Dynamic",
    "enum8": "Enum8",
    "enum16": "Enum16",
    "fixedstring": "FixedString",
    "ipv4": "IPv4",
    "ipv6": "IPv6",
    "json": "JSON",
    "lowcardinality": "LowCardinality",
    "map": "Map",
    "nested": "Nested",
    "nothing": "Nothing",
    "nullable": "Nullable",
    "object": "Object",
    "simpleaggregatefunction": "SimpleAggregateFunction",
    "string": "String",
    "tuple": "Tuple",
    "uuid": "UUID",
    "variant": "Variant",
}


def normalize_data_type_sql(*, sql: str, dialect: str) -> str:
    """Generate one data type with canonical ClickHouse type vocabulary."""

    try:
        generated: str = _native.polyglot_generate_data_type(sql, dialect)
    except Exception as error:
        raise SqlAnalysisError(
            f"SQL data type could not be parsed with Polyglot: {error}"
        ) from None
    return _canonicalize_type_tokens(generated)


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


def _canonicalize_type_tokens(sql: str) -> str:
    result: list[str] = []
    contexts: list[tuple[str | None, int, bool]] = []
    pending_type_name: str | None = None
    index: int = 0
    while index < len(sql):
        if sql[index] in SQL_QUOTE_CHARACTERS:
            end: int = _type_quoted_end(sql=sql, start=index)
            result.append(sql[index:end])
            if contexts:
                context_name: str | None
                argument_index: int
                context_name, argument_index, _ = contexts[-1]
                contexts[-1] = (context_name, argument_index, False)
            pending_type_name = None
            index = end
            continue
        if sql[index].isalpha() or sql[index] == SQL_IDENTIFIER_PREFIX:
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == SQL_IDENTIFIER_PREFIX):
                end += 1
            token: str = sql[index:end]
            sensitive_detail: bool = _is_sensitive_type_detail(
                sql=sql,
                token_end=end,
                contexts=contexts,
            )
            canonical_token: str = (
                token if sensitive_detail else _CANONICAL_TYPE_NAMES.get(token.lower(), token)
            )
            result.append(canonical_token)
            if contexts:
                context_name, argument_index, _ = contexts[-1]
                contexts[-1] = (context_name, argument_index, False)
            pending_type_name = None if sensitive_detail else canonical_token
            index = end
            continue
        if sql[index] == SQL_OPEN_PARENTHESIS:
            contexts.append((pending_type_name, 0, True))
            pending_type_name = None
        elif sql[index] == SQL_ARGUMENT_SEPARATOR and contexts:
            context_name, argument_index, _ = contexts[-1]
            contexts[-1] = (context_name, argument_index + 1, True)
            pending_type_name = None
        elif sql[index] == SQL_CLOSE_PARENTHESIS and contexts:
            contexts.pop()
            pending_type_name = None
        result.append(sql[index])
        index += 1
    return "".join(result)


def _is_sensitive_type_detail(
    *,
    sql: str,
    token_end: int,
    contexts: list[tuple[str | None, int, bool]],
) -> bool:
    if not contexts:
        return False
    context_name: str | None
    argument_index: int
    argument_start: bool
    context_name, argument_index, argument_start = contexts[-1]
    normalized_context: str = "" if context_name is None else context_name.lower()
    if normalized_context in CLICKHOUSE_AGGREGATE_STATE_TYPE_NAMES:
        return argument_index == 0
    if normalized_context not in CLICKHOUSE_NAMED_FIELD_TYPE_NAMES or not argument_start:
        return False
    next_index: int = token_end
    while next_index < len(sql) and sql[next_index].isspace():
        next_index += 1
    if next_index == token_end or next_index >= len(sql):
        return False
    next_end: int = next_index
    while next_end < len(sql) and (
        sql[next_end].isalnum() or sql[next_end] == SQL_IDENTIFIER_PREFIX
    ):
        next_end += 1
    return sql[next_index:next_end].lower() in _CANONICAL_TYPE_NAMES


def _type_quoted_end(*, sql: str, start: int) -> int:
    quote: str = sql[start]
    index: int = start + 1
    while index < len(sql):
        if sql[index] == SQL_ESCAPE_CHARACTER and index + 1 < len(sql):
            index += 2
            continue
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)
