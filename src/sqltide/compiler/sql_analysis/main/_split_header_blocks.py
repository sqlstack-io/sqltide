"""Split SQL extension header blocks through lexical analysis."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis.models import SqlHeaderBlock


def split_sql_header_blocks(*, sql: str, keyword: str) -> tuple[SqlHeaderBlock, ...]:
    """Return line-leading extension headers and their SQL bodies."""

    return _native.split_header_blocks(sql, keyword)
