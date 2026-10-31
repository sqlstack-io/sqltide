"""Render one SQL string literal through the mandatory analysis boundary."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql


def render_string_literal(*, value: str, dialect: str) -> str:
    """Return one dialect-safe SQL string literal."""

    return native_sql(lambda: _native.sql_render_string_literal(value, dialect))
