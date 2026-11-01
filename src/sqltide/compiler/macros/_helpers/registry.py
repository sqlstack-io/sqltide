"""Apache-2.0: SQLBuild compiler/compile/_helpers/render/macros.py@7e3b2f854f05."""

from __future__ import annotations

import ast
import hashlib
import inspect
import traceback
from pathlib import Path
from types import ModuleType

from sqltide.compiler.discovery.models import DiscoveredProjectFile
from sqltide.compiler.macros.constants import RESERVED_MACRO_NAMES
from sqltide.compiler.macros.exceptions import MacroError
from sqltide.compiler.macros.models import LoadedMacro, MacroRegistry
from sqltide.diagnostics.models import RelatedDiagnosticLocation, SourceLocation


def load_project_macros(*, macro_files: tuple[DiscoveredProjectFile, ...]) -> MacroRegistry:
    """Load one deterministic registry from retained project macro sources."""

    loaded_macros: dict[str, LoadedMacro] = {}
    macro_file: DiscoveredProjectFile
    for macro_file in macro_files:
        module: ModuleType = _load_macro_module(
            macro_file=macro_file,
        )
        loaded_macro: LoadedMacro
        for loaded_macro in _load_public_macros_from_module(
            module=module,
            macro_file=macro_file,
        ):
            if loaded_macro.name in RESERVED_MACRO_NAMES:
                raise MacroError(
                    f"Macro '{loaded_macro.name}' in '{macro_file.file_path}' uses a reserved "
                    "name; @const and @enum read project constants and enums",
                    location=_definition_location(loaded_macro),
                )
            existing_macro: LoadedMacro | None = loaded_macros.get(loaded_macro.name)
            if existing_macro is not None:
                collision_paths: str = f"'{macro_file.file_path}' and '{existing_macro.file_path}'"
                raise MacroError(
                    f"Macro name collision for '{loaded_macro.name}' in {collision_paths}",
                    location=_definition_location(loaded_macro),
                    related_locations=(
                        RelatedDiagnosticLocation(
                            label="first macro definition",
                            location=_definition_location(existing_macro),
                        ),
                    ),
                )
            loaded_macros[loaded_macro.name] = loaded_macro
    return MacroRegistry(macros=loaded_macros)


def _load_macro_module(*, macro_file: DiscoveredProjectFile) -> ModuleType:
    module_name: str = _macro_module_name(macro_file)
    module: ModuleType = ModuleType(module_name)
    module.__file__ = str(macro_file.file_path)
    try:
        exec(
            compile(macro_file.contents, str(macro_file.file_path), "exec"),
            module.__dict__,
        )
    except SyntaxError as error:
        raise MacroError(
            f"Failed to load {macro_file.file_path}: {error}",
            location=SourceLocation(
                path=macro_file.file_path,
                line=error.lineno or 1,
                column=error.offset or 1,
            ),
        ) from error
    except Exception as error:
        raise MacroError(
            f"Failed to load {macro_file.file_path}: {error}",
            location=SourceLocation(
                path=macro_file.file_path,
                line=_macro_exception_line(error=error, macro_file=macro_file),
                column=1,
            ),
        ) from error
    return module


def _load_public_macros_from_module(
    *, module: ModuleType, macro_file: DiscoveredProjectFile
) -> tuple[LoadedMacro, ...]:
    definition_line_by_name: dict[str, int] = _definition_lines(macro_file)
    loaded_macros: list[LoadedMacro] = []
    name: str
    function: object
    for name, function in inspect.getmembers(module, inspect.isfunction):
        if (
            name.startswith("_")
            or function.__module__ != module.__name__
            or inspect.iscoroutinefunction(function)
            or name not in definition_line_by_name
        ):
            continue
        loaded_macros.append(
            LoadedMacro(
                name=name,
                file_path=macro_file.file_path,
                relative_path=macro_file.relative_path,
                source=macro_file.contents,
                definition_line=definition_line_by_name[name],
                function=function,
                description=_macro_description(function),
            )
        )
    return tuple(loaded_macros)


def _macro_description(function: object) -> str | None:
    """The macro's docstring first paragraph; authors document macros in place."""

    docstring: str | None = inspect.getdoc(function)
    if docstring is None:
        return None
    first_paragraph: str = docstring.split("\n\n")[0].strip()
    return first_paragraph or None


def _macro_module_name(macro_file: DiscoveredProjectFile) -> str:
    digest: str = hashlib.sha1(str(macro_file.file_path).encode("utf-8")).hexdigest()[:12]
    return f"sqltide_project_macros_{digest}"


def _definition_lines(macro_file: DiscoveredProjectFile) -> dict[str, int]:
    try:
        tree: ast.Module = ast.parse(macro_file.contents, filename=str(macro_file.file_path))
    except SyntaxError as error:
        raise MacroError(
            f"Failed to load {macro_file.file_path}: {error}",
            location=SourceLocation(
                path=macro_file.file_path,
                line=error.lineno or 1,
                column=error.offset or 1,
            ),
        ) from error
    return {
        node.name: node.lineno
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _definition_location(macro: LoadedMacro) -> SourceLocation:
    return SourceLocation(
        path=macro.file_path,
        line=macro.definition_line,
        column=1,
    )


def _macro_exception_line(*, error: Exception, macro_file: DiscoveredProjectFile) -> int:
    frame: traceback.FrameSummary
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        if Path(frame.filename) == macro_file.file_path:
            return frame.lineno or 1
    return 1
