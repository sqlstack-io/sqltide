"""Build one INSERT SELECT through the mandatory SQL-analysis boundary."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql


def build_insert_query(*, target_relation: str, query: str, dialect: str) -> str:
    """Compose one byte-preserving INSERT SELECT statement validated by parsing."""

    statement: str = f"INSERT INTO {target_relation}\n{query}"
    _ = native_sql(lambda: _native.sql_validate_insert_query(statement, dialect))
    return statement
