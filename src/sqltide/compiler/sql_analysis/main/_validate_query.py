"""Validate one SQL query through the mandatory analysis boundary."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql


def validate_query(*, sql: str, dialect: str) -> None:
    """Require exactly one SELECT or set-operation query."""

    _ = native_sql(lambda: _native.sql_validate_query(sql, dialect))
