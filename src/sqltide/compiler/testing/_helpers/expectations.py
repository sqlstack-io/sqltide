"""Expected-column inference and target-type casting for SQL tests."""

from __future__ import annotations

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.main._infer_projection_names import infer_projection_names
from sqltide.compiler.testing.exceptions import SqlTestAssemblyError


def derive_column_names(
    *,
    query: str,
    file_path: Path,
    label: str,
    authored_ctes: tuple[tuple[str, str], ...],
    dialect: str,
) -> tuple[str, ...]:
    """Infer the output column names of one authored test query through Polyglot."""

    try:
        return infer_projection_names(
            sql=wrap_with_ctes(query=query, ctes=authored_ctes),
            dialect=dialect,
        )
    except SqlAnalysisError as error:
        raise SqlTestAssemblyError(
            f"SQL test '{file_path}' cannot resolve {label} columns: {error}"
        ) from error


def wrap_with_ctes(*, query: str, ctes: tuple[tuple[str, str], ...]) -> str:
    """Prefix one query with the authored CTEs it may reference."""

    return _native.sql_test_wrap_with_ctes(query, list(ctes))
