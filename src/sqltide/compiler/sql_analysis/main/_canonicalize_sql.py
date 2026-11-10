"""SQL analysis entrypoints."""

import sqltide._native as _native


def canonicalize_sql(*, sql: str, dialect: str) -> str:
    """Parse and generate one SQL statement through the mandatory analysis engine."""

    return _native.sql_canonicalize(sql, dialect)
