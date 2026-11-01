"""Parsing helpers for authored SQL audit files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqltide._native as _native
from sqltide.compiler.audit_discovery.exceptions import SqlAuditParseError
from sqltide.compiler.audit_discovery.models import (
    LoadedGenericSqlAuditDefinition,
    LoadedSqlAudit,
)
from sqltide.compiler.compile.main._extract_refs import extract_refs
from sqltide.compiler.compile.models import ParsedRef
from sqltide.compiler.discovery.exceptions import ModelHeaderSyntaxError
from sqltide.compiler.discovery.main._parse_sql_header import parse_sql_header
from sqltide.compiler.discovery.types import SqlRelationType
from sqltide.compiler.macros.main._expand_macro_calls import expand_macro_calls
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.main._split_header_blocks import split_sql_header_blocks
from sqltide.compiler.sql_analysis.main._validate_query import validate_query
from sqltide.compiler.sql_analysis.models import SqlHeaderBlock


def parse_sql_audit_file(
    *,
    file_path: Path,
    contents: str | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> tuple[LoadedSqlAudit, ...]:
    """Parse one authored SQL audit file into one or more discovered audit specs."""

    source_contents: str = file_path.read_text(encoding="utf-8") if contents is None else contents
    matched_blocks: tuple[SqlHeaderBlock, ...] = _audit_blocks(
        file_path=file_path,
        contents=source_contents,
    )
    if not matched_blocks:
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' must start with an AUDIT(...) header as the first "
            "non-whitespace content"
        )
    if source_contents[: matched_blocks[0].start].strip():
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' must not contain content before the first AUDIT(...) block"
        )
    loaded_audits: list[LoadedSqlAudit] = []
    block_index: int
    header: str
    sql: str
    block: SqlHeaderBlock
    for block_index, block in enumerate(matched_blocks, start=1):
        header: str = block.header
        sql: str = block.body
        leading_length: int = len(sql) - len(sql.lstrip())
        source_line: int
        source_column: int
        source_line, source_column = _source_position(
            contents=source_contents,
            index=block.body_start + leading_length,
        )
        loaded_audits.append(
            _parse_concrete_audit_block(
                file_path=file_path,
                header=header,
                sql=sql,
                audit_index=block_index,
                macro_registry=macro_registry,
                macro_context=macro_context,
                source_line=source_line,
                source_column=source_column,
            )
        )
    if len(loaded_audits) > 1:
        _validate_multi_audit_names(file_path=file_path, loaded_audits=tuple(loaded_audits))
    return tuple(loaded_audits)


def parse_generic_sql_audit_definition(
    *,
    file_path: Path,
    contents: str | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> LoadedGenericSqlAuditDefinition:
    """Parse one generic SQL audit definition from `audits/generic/`."""

    source_contents: str = file_path.read_text(encoding="utf-8") if contents is None else contents
    matched_blocks: tuple[SqlHeaderBlock, ...] = _audit_blocks(
        file_path=file_path,
        contents=source_contents,
    )
    if len(matched_blocks) != 1:
        raise SqlAuditParseError(
            f"Generic SQL audit definition '{file_path}' must contain exactly one AUDIT(...) block"
        )
    first_block: SqlHeaderBlock = matched_blocks[0]
    if source_contents[: first_block.start].strip():
        raise SqlAuditParseError(
            f"Generic SQL audit definition '{file_path}' must not contain content before the "
            "AUDIT(...) block"
        )
    first_header: str = first_block.header
    first_sql: str = first_block.body
    header_values: dict[str, Any] = _parse_audit_header(
        header=first_header,
        file_path=file_path,
    )
    if header_values:
        raise SqlAuditParseError(
            f"Generic SQL audit definition '{file_path}' must not define AUDIT() header fields"
        )
    leading_length: int = len(first_sql) - len(first_sql.lstrip())
    source_line: int
    source_column: int
    source_line, source_column = _source_position(
        contents=source_contents,
        index=first_block.body_start + leading_length,
    )
    query: str = _expand_audit_query(
        sql=first_sql.strip(),
        file_path=file_path,
        macro_registry=macro_registry,
        macro_context=macro_context,
        source_line=source_line,
        source_column=source_column,
    )
    if not query:
        raise SqlAuditParseError(
            f"Generic SQL audit definition '{file_path}' must define a query after AUDIT(...)"
        )
    _validate_single_query(file_path=file_path, sql=query)
    raw_parameter_names, quoted_parameter_names = _native.generic_audit_parameter_names(query)
    return LoadedGenericSqlAuditDefinition(
        file_path=file_path,
        query=query,
        raw_parameter_names=tuple(raw_parameter_names),
        quoted_parameter_names=tuple(quoted_parameter_names),
        name=file_path.stem,
    )


def _parse_concrete_audit_block(
    *,
    file_path: Path,
    header: str,
    sql: str,
    audit_index: int,
    macro_registry: MacroRegistry | None,
    macro_context: MacroContext | None,
    source_line: int,
    source_column: int,
) -> LoadedSqlAudit:
    header_values: dict[str, Any] = _parse_audit_header(header=header, file_path=file_path)
    query: str = _expand_audit_query(
        sql=sql.strip(),
        file_path=file_path,
        macro_registry=macro_registry,
        macro_context=macro_context,
        source_line=source_line,
        source_column=source_column,
    )
    if not query:
        raise SqlAuditParseError(f"SQL audit '{file_path}' must define a query after AUDIT(...)")
    _validate_single_query(file_path=file_path, sql=query)
    parsed_refs: tuple[ParsedRef, ...] = tuple(extract_refs(sql=query, source_path=file_path))
    if not parsed_refs:
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' must reference at least one model with __ref(...)"
        )
    parsed_ref: ParsedRef
    for parsed_ref in parsed_refs:
        if parsed_ref.relation_type != SqlRelationType.REF:
            raise SqlAuditParseError(
                f"SQL audit '{file_path}' may only use __ref(...); __source(...) is not allowed"
            )
    referenced_model_names: tuple[str, ...] = tuple(dict.fromkeys(ref.name for ref in parsed_refs))
    try:
        (
            severity,
            description,
            name,
            severity_is_explicit,
            cadence_seconds,
            warmup_seconds,
            scheduled,
        ) = _native.audit_header_fields(str(file_path), list(header_values.items()))
    except ValueError as error:
        raise SqlAuditParseError(str(error)) from None
    return LoadedSqlAudit(
        file_path=file_path,
        query=query,
        referenced_model_names=referenced_model_names,
        severity=severity,
        description=description,
        name=name,
        audit_index=audit_index,
        severity_is_explicit=severity_is_explicit,
        cadence_seconds_override=cadence_seconds,
        warmup_seconds_override=warmup_seconds,
        scheduled_override=scheduled,
    )


def _expand_audit_query(
    *,
    sql: str,
    file_path: Path,
    macro_registry: MacroRegistry | None,
    macro_context: MacroContext | None,
    source_line: int,
    source_column: int,
) -> str:
    if macro_registry is None or macro_context is None:
        return sql
    return expand_macro_calls(
        sql=sql,
        file_path=file_path,
        registry=macro_registry,
        context=macro_context,
        source_line=source_line,
        source_column=source_column,
    )


def _source_position(*, contents: str, index: int) -> tuple[int, int]:
    line: int = contents.count("\n", 0, index) + 1
    previous_newline_index: int = contents.rfind("\n", 0, index)
    return line, index - previous_newline_index


def _parse_audit_header(*, header: str, file_path: Path) -> dict[str, Any]:
    stripped_header: str = header.strip()
    if not stripped_header:
        return {}
    try:
        parsed_header: dict[str, object] = parse_sql_header(header=stripped_header)
    except ModelHeaderSyntaxError as error:
        raise SqlAuditParseError(
            f"AUDIT() header in '{file_path}' uses invalid SQLBuild syntax: {error}"
        ) from error
    unsupported_keys: str | None = _native.unsupported_audit_header_keys(
        str(file_path), list(parsed_header.items())
    )
    if unsupported_keys is not None:
        raise SqlAuditParseError(unsupported_keys)
    return parsed_header


def _validate_single_query(*, file_path: Path, sql: str) -> None:
    try:
        validate_query(sql=sql, dialect="clickhouse")
    except SqlAnalysisError as error:
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' must contain exactly one valid top-level query after "
            f"AUDIT(): {error}"
        ) from None


def _audit_blocks(*, file_path: Path, contents: str) -> tuple[SqlHeaderBlock, ...]:
    try:
        return split_sql_header_blocks(sql=contents, keyword="AUDIT")
    except SqlAnalysisError as error:
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' header could not be parsed: {error}"
        ) from None


def _validate_multi_audit_names(
    *,
    file_path: Path,
    loaded_audits: tuple[LoadedSqlAudit, ...],
) -> None:
    missing_named_indexes: tuple[int, ...] = tuple(
        loaded_audit.audit_index for loaded_audit in loaded_audits if loaded_audit.name is None
    )
    if missing_named_indexes:
        raise SqlAuditParseError(
            f"SQL audit '{file_path}' contains multiple AUDIT(...) blocks; each must define name"
        )
    seen_names: set[str] = set()
    loaded_audit: LoadedSqlAudit
    for loaded_audit in loaded_audits:
        if loaded_audit.name in seen_names:
            raise SqlAuditParseError(
                f"SQL audit '{file_path}' contains duplicate audit name '{loaded_audit.name}'"
            )
        seen_names.add(loaded_audit.name or "")
