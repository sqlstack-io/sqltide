"""Parse one SQL expression list through the mandatory analysis boundary."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql


def parse_expression_list(*, sql: str, dialect: str) -> tuple[str, ...]:
    """Return canonical top-level expressions without textual comma splitting."""

    return tuple(native_sql(lambda: _native.sql_expression_list(sql, dialect)))
