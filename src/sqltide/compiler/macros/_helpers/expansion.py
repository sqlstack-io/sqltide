"""Apache-2.0: SQLBuild compiler/compile/_helpers/render/macros.py@7e3b2f854f05."""

from __future__ import annotations

import inspect
from functools import partial
from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.macros.constants import (
    MACRO_CONTEXT_ANNOTATION_NAME,
    MACRO_CONTEXT_PARAMETER_NAME,
    MACRO_SIGIL,
)
from sqltide.compiler.macros.exceptions import MacroError
from sqltide.compiler.macros.models import LoadedMacro, MacroContext, MacroRegistry
from sqltide.compiler.sql_analysis.classes.sql_lexical_scanner import SqlLexicalScanner
from sqltide.diagnostics.models import RelatedDiagnosticLocation, SourceLocation


def expand_sql_body_macros(
    *,
    sql: str,
    file_path: Path,
    registry: MacroRegistry,
    context: MacroContext,
    source_line: int,
    source_column: int,
) -> str:
    """Expand authored Python macros in a SQL body string."""

    if MACRO_SIGIL not in sql:
        return sql
    locate: partial[SourceLocation] = partial(
        _sql_location,
        sql=sql,
        file_path=file_path,
        source_line=source_line,
        source_column=source_column,
    )
    return SqlLexicalScanner.scanned(
        lambda: _native.expand_macro_calls(
            sql,
            sorted(registry.macros),
            str(file_path),
            partial(
                _call_macro,
                registry=registry,
                context=context,
                file_path=file_path,
                locate=locate,
            ),
            partial(_macro_error, registry=registry, locate=locate),
            partial(_located_macro_error, registry=registry, locate=locate),
            registry.declarations,
        )
    )


def _call_macro(
    *,
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    index: int,
    registry: MacroRegistry,
    context: MacroContext,
    file_path: Path,
    locate: partial[SourceLocation],
) -> object:
    loaded_macro: LoadedMacro = registry.macros[name]
    call_location: SourceLocation = locate(index=index)
    try:
        return _call_loaded_macro(
            loaded_macro=loaded_macro,
            context=context,
            args=args,
            kwargs=kwargs,
            file_path=file_path,
            call_location=call_location,
        )
    except MacroError:
        raise
    except TypeError as error:
        raise MacroError(
            f"Macro '@{name}' in '{file_path}' could not be called: {error}",
            location=call_location,
            related_locations=_definition_related_location(loaded_macro),
        ) from error
    except Exception as error:
        raise MacroError(
            f"Macro '@{name}' in '{file_path}' failed: {error}",
            location=call_location,
            related_locations=_definition_related_location(loaded_macro),
        ) from error


def _macro_error(
    *,
    message: str,
    index: int | None,
    name: str | None,
    registry: MacroRegistry,
    locate: partial[SourceLocation],
) -> MacroError:
    if index is None:
        return MacroError(message)
    return MacroError(
        message,
        location=locate(index=index),
        related_locations=(
            () if name is None else _definition_related_location(registry.macros[name])
        ),
    )


def _located_macro_error(
    *,
    error: BaseException,
    index: int,
    name: str | None,
    registry: MacroRegistry,
    locate: partial[SourceLocation],
) -> BaseException:
    if not isinstance(error, MacroError) or error.location is not None:
        return error
    located: MacroError = MacroError(
        str(error),
        location=locate(index=index),
        related_locations=(
            () if name is None else _definition_related_location(registry.macros[name])
        ),
    )
    located.__cause__ = error
    return located


def _call_loaded_macro(
    *,
    loaded_macro: LoadedMacro,
    context: MacroContext,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    file_path: Path,
    call_location: SourceLocation,
) -> object:
    parameters: tuple[inspect.Parameter, ...] = tuple(
        inspect.signature(loaded_macro.function).parameters.values()
    )
    accepts_context: bool = bool(parameters) and parameters[0].name == MACRO_CONTEXT_PARAMETER_NAME
    if MACRO_CONTEXT_PARAMETER_NAME in kwargs:
        raise MacroError(
            f"Macro '@{loaded_macro.name}' in '{file_path}' reserves keyword 'ctx'",
            location=call_location,
            related_locations=_definition_related_location(loaded_macro),
        )
    if not accepts_context:
        return loaded_macro.function(*args, **kwargs)
    annotation: object = parameters[0].annotation
    if annotation not in {MacroContext, MACRO_CONTEXT_ANNOTATION_NAME}:
        raise MacroError(
            f"Macro '@{loaded_macro.name}' in '{loaded_macro.file_path}' must annotate ctx "
            "as MacroContext",
            location=call_location,
            related_locations=_definition_related_location(loaded_macro),
        )
    return loaded_macro.function(context, *args, **kwargs)


def _sql_location(
    *, sql: str, index: int, file_path: Path, source_line: int, source_column: int
) -> SourceLocation:
    relative_line: int = sql.count("\n", 0, index)
    previous_newline: int = sql.rfind("\n", 0, index)
    relative_column: int = index - previous_newline
    return SourceLocation(
        path=file_path,
        line=source_line + relative_line,
        column=(source_column + relative_column - 1 if relative_line == 0 else relative_column),
    )


def _definition_related_location(
    loaded_macro: LoadedMacro,
) -> tuple[RelatedDiagnosticLocation, ...]:
    return (
        RelatedDiagnosticLocation(
            label="macro definition",
            location=SourceLocation(
                path=loaded_macro.file_path,
                line=loaded_macro.definition_line,
                column=1,
            ),
        ),
    )


def find_macro_call_names_impl(sql: str) -> tuple[str, ...]:
    """Return unique authored macro call names in encounter order."""

    if MACRO_SIGIL not in sql:
        return ()
    names: list[str]
    failure: str | None
    names, failure = SqlLexicalScanner.scanned(lambda: _native.find_macro_call_names(sql))
    if failure is not None:
        raise MacroError(failure)
    return tuple(names)
