"""Validate one SQL query through the mandatory analysis boundary."""

import sqltide._native as _native


def validate_query(*, sql: str, dialect: str) -> None:
    """Require exactly one SELECT or set-operation query."""

    _native.sql_validate_query(sql, dialect)
