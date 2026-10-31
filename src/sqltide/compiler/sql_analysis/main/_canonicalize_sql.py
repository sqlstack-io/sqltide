import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql


def canonicalize_sql(*, sql: str, dialect: str) -> str:
    """Parse and generate one SQL statement through the mandatory analysis engine."""

    return native_sql(lambda: _native.sql_canonicalize(sql, dialect))
