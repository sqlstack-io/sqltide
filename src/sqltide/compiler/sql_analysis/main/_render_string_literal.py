"""Render one SQL string literal through the mandatory analysis boundary."""

import sqltide._native as _native


def render_string_literal(*, value: str, dialect: str) -> str:
    """Return one dialect-safe SQL string literal."""

    return _native.sql_render_string_literal(value, dialect)
