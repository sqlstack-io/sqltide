"""Normalize one SQL data type through the mandatory analysis boundary."""

import sqltide._native as _native


def normalize_sql_data_type(*, sql: str, dialect: str) -> str:
    """Return one canonical SQL data type."""

    return _native.sql_normalize_data_type(sql, dialect)
