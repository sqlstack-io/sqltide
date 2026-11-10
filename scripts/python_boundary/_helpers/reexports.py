"""Recognise modules that only re-export native names under their public import path."""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.python_boundary.constants import EXPORTS_NAME, NATIVE_MODULE


def is_pure_reexport(path: Path) -> bool:
    """Whether `path` is only native `X as X` imports, a docstring and a matching `__all__`."""
    body: list[ast.stmt] = ast.parse(path.read_text(encoding="utf-8")).body
    if body and _is_docstring(body[0]):
        body = body[1:]
    imported: list[str] = []
    exports: list[ast.stmt] = []
    for statement in body:
        if isinstance(statement, ast.ImportFrom) and _is_native_reexport(statement):
            imported.extend(alias.name for alias in statement.names)
        elif _assigns_exports(statement):
            exports.append(statement)
        else:
            return False
    if not imported or len(exports) > 1:
        return False
    return all(_export_names(statement) == sorted(imported) for statement in exports)


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _is_native_reexport(statement: ast.ImportFrom) -> bool:
    return (
        statement.module == NATIVE_MODULE
        and statement.level == 0
        and all(alias.asname == alias.name for alias in statement.names)
    )


def _assigns_exports(statement: ast.stmt) -> bool:
    targets: list[ast.expr] = (
        list(statement.targets)
        if isinstance(statement, ast.Assign)
        else [statement.target]
        if isinstance(statement, ast.AnnAssign)
        else []
    )
    return len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == EXPORTS_NAME


def _export_names(statement: ast.stmt) -> list[str] | None:
    value: ast.expr | None = getattr(statement, "value", None)
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    names: list[str] = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        names.append(element.value)
    return sorted(names)
