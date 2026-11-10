"""Parse one SQL expression list through the mandatory analysis boundary."""

import sqltide._native as _native


def parse_expression_list(*, sql: str, dialect: str) -> tuple[str, ...]:
    """Return canonical top-level expressions without textual comma splitting."""

    return tuple(_native.sql_expression_list(sql, dialect))
