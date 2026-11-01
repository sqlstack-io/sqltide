"""SQL model parsing helpers for authored pipeline folders."""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from typing import Any

import sqltide._native as _native
from sqltide.compiler.compile.main._extract_refs import extract_refs
from sqltide.compiler.compile.models import ParsedRef
from sqltide.compiler.discovery._helpers.model_header import (
    parse_model_header,
)
from sqltide.compiler.discovery.constants import (
    MODEL_HEADER_PATTERN,
)
from sqltide.compiler.discovery.exceptions import ModelHeaderSyntaxError, PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    ModelKind,
    SqlRelationType,
)
from sqltide.compiler.macros.main._expand_macro_calls import expand_macro_calls
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.diagnostics.models import CompilerDiagnostic, SourceLocation
from sqltide.diagnostics.types import DiagnosticPhase, DiagnosticSeverity


def load_transform_from_sql_file(
    *,
    file_path: Path,
    contents: str | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> TransformStep | ViewStep:
    """Load one authored SQL model file into its kind-specific model step."""

    source_contents: str = file_path.read_text(encoding="utf-8") if contents is None else contents
    try:
        return _load_transform_from_sql_contents(
            file_path=file_path,
            source_contents=source_contents,
            macro_registry=macro_registry,
            macro_context=macro_context,
        )
    except PipelineDiscoveryError as error:
        error.diagnostic = CompilerDiagnostic(
            phase=DiagnosticPhase.DISCOVERY,
            severity=DiagnosticSeverity.ERROR,
            code="SQT-DISCOVERY-001",
            message=str(error),
            resource_name=file_path.stem,
            location=SourceLocation(path=file_path, line=1, column=1),
        )
        raise


def _load_transform_from_sql_contents(
    *,
    file_path: Path,
    source_contents: str,
    macro_registry: MacroRegistry | None,
    macro_context: MacroContext | None,
) -> TransformStep | ViewStep:
    header_values, query = parse_model_sql(
        contents=source_contents,
        file_path=file_path,
        macro_registry=macro_registry,
        macro_context=macro_context,
    )
    query_line: int
    query_column: int
    query_line, query_column = _query_source_position(source_contents)
    kind: str
    fields: dict[str, Any]
    kind, fields = _native.parse_model_spec(
        header_values,
        str(file_path),
        partial(
            _check_query,
            query=query,
            file_path=file_path,
            source_line=query_line,
            source_column=query_column,
        ),
    )
    if kind == ModelKind.VIEW:
        return ViewStep(
            name=file_path.stem,
            query=query,
            source_file_path=file_path,
            source_line=query_line,
            source_column=query_column,
            **fields,
        )
    return TransformStep(
        name=file_path.stem,
        query=query,
        source_file_path=file_path,
        source_line=query_line,
        source_column=query_column,
        **fields,
    )


def _check_query(
    *, kind: str, query: str, file_path: Path, source_line: int, source_column: int
) -> str | None:
    if kind == ModelKind.VIEW:
        _validate_view_refs(
            query=query,
            file_path=file_path,
            source_line=source_line,
            source_column=source_column,
        )
        return None
    return infer_transform_source(
        query=query,
        file_path=file_path,
        source_line=source_line,
        source_column=source_column,
    )


def parse_model_sql(
    *,
    contents: str,
    file_path: Path,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> tuple[dict[str, Any], str]:
    """Parse the required `MODEL(...)` header and SQL query body."""

    header_match: re.Match[str] | None = MODEL_HEADER_PATTERN.match(contents)
    if header_match is None:
        raise PipelineDiscoveryError(
            f"SQL model '{file_path}' must start with a MODEL(...) header as the first "
            "non-whitespace content"
        )

    header_values: dict[str, Any] = _parse_model_header(
        header=header_match.group("header"), file_path=file_path
    )
    query_line: int
    query_column: int
    query_line, query_column = _query_source_position(contents)
    query: str = _expanded_query(
        sql=header_match.group("sql").strip(),
        file_path=file_path,
        macro_registry=macro_registry,
        macro_context=macro_context,
        source_line=query_line,
        source_column=query_column,
    )
    if not query:
        raise PipelineDiscoveryError(
            f"SQL model '{file_path}' must contain a SELECT query after MODEL(...)"
        )
    return header_values, query


def _expanded_query(
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


def _query_source_position(contents: str) -> tuple[int, int]:
    header_match: re.Match[str] | None = MODEL_HEADER_PATTERN.match(contents)
    if header_match is None:
        raise PipelineDiscoveryError("Cannot locate SQL model query after a valid MODEL header")
    raw_query: str = header_match.group("sql")
    leading_length: int = len(raw_query) - len(raw_query.lstrip())
    query_index: int = header_match.start("sql") + leading_length
    line: int = contents.count("\n", 0, query_index) + 1
    previous_newline_index: int = contents.rfind("\n", 0, query_index)
    column: int = query_index - previous_newline_index
    return line, column


def infer_transform_source(
    *, query: str, file_path: Path, source_line: int, source_column: int
) -> str:
    """Infer the driving source from the single untyped relation reference."""

    parsed_refs: tuple[ParsedRef, ...] = tuple(
        extract_refs(
            sql=query,
            source_path=file_path,
            source_line=source_line,
            source_column=source_column,
        )
    )
    if not parsed_refs:
        raise PipelineDiscoveryError(
            f"SQL model '{file_path}' must reference exactly one driving input using "
            "__source(...) or __ref(...)"
        )
    unique_ref_names: set[str] = {parsed_ref.name for parsed_ref in parsed_refs}
    source_ref_names: set[str] = {
        parsed_ref.name
        for parsed_ref in parsed_refs
        if parsed_ref.relation_type == SqlRelationType.SOURCE
    }
    driving_ref_names: set[str] = {
        parsed_ref.name for parsed_ref in parsed_refs if parsed_ref.ref_type is None
    }
    if source_ref_names:
        if len(source_ref_names) != 1:
            raise PipelineDiscoveryError(
                f"SQL model '{file_path}' must contain exactly one unique __source(...) "
                "when source refs are present"
            )
        untyped_ref_names: set[str] = {
            parsed_ref.name
            for parsed_ref in parsed_refs
            if parsed_ref.relation_type == SqlRelationType.REF and parsed_ref.ref_type is None
        }
        if untyped_ref_names:
            raise PipelineDiscoveryError(
                f"SQL model '{file_path}' must not mix __source(...) with untyped __ref(...)"
            )
        return next(iter(source_ref_names))
    if len(unique_ref_names) == 1 and all(
        parsed_ref.ref_type is not None for parsed_ref in parsed_refs
    ):
        raise PipelineDiscoveryError(
            f"SQL model '{file_path}' must not declare ref_type for its driving input "
            f"'{next(iter(unique_ref_names))}'"
        )
    if len(driving_ref_names) != 1:
        raise PipelineDiscoveryError(
            f"SQL model '{file_path}' must contain exactly one unique untyped driving input "
            "when multiple relations are present"
        )
    return next(iter(driving_ref_names))


def _parse_model_header(*, header: str, file_path: Path) -> dict[str, Any]:
    try:
        parsed_header: dict[str, object] = parse_model_header(header=header)
    except ModelHeaderSyntaxError as error:
        raise PipelineDiscoveryError(
            f"MODEL(...) in '{file_path}' contains invalid SQLBuild header syntax: {error}"
        ) from error

    _native.check_model_header_keys(parsed_header, str(file_path))
    return parsed_header


def _validate_view_refs(
    *, query: str, file_path: Path, source_line: int, source_column: int
) -> None:
    parsed_refs: tuple[ParsedRef, ...] = tuple(
        extract_refs(
            sql=query,
            source_path=file_path,
            source_line=source_line,
            source_column=source_column,
        )
    )
    typed_ref_names: tuple[str, ...] = tuple(
        sorted({parsed_ref.name for parsed_ref in parsed_refs if parsed_ref.ref_type is not None})
    )
    if typed_ref_names:
        raise PipelineDiscoveryError(
            f"View MODEL(...) in '{file_path}' cannot declare ref_type annotations for: "
            f"{', '.join(typed_ref_names)}"
        )
