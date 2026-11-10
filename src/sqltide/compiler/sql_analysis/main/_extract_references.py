"""Extract typed logical SQL references."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis.models import SqlReference


def extract_references(sql: str) -> tuple[SqlReference, ...]:
    """Return source-located logical references in authored order."""

    return _native.extract_references(sql)
