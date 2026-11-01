"""Apache-2.0: SQLBuild discovery/_helpers/sql/tests.py header parsing@7e3b2f854f05."""

from __future__ import annotations

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import ModelHeaderSyntaxError
from sqltide.compiler.discovery.main._parse_sql_header import parse_sql_header
from sqltide.compiler.test_discovery.constants import (
    DEFAULT_SQL_TEST_MODE,
)
from sqltide.compiler.test_discovery.exceptions import SqlTestParseError
from sqltide.compiler.test_discovery.models import SqlTestHeader
from sqltide.compiler.test_discovery.types import SqlTestMode


def parse_test_header(*, file_path: Path, header_contents: str) -> SqlTestHeader:
    """Parse one TEST(...) header into its optional name and effective mode."""

    stripped_header_contents: str = header_contents.strip()
    if not stripped_header_contents:
        return SqlTestHeader(name=None, mode=DEFAULT_SQL_TEST_MODE)
    parsed_header: dict[str, object] = _parse_header_mapping(
        file_path=file_path, header_contents=stripped_header_contents
    )
    try:
        name, mode = _native.sql_test_header_fields(str(file_path), list(parsed_header.items()))
    except ValueError as error:
        raise SqlTestParseError(str(error)) from None
    return SqlTestHeader(name=name, mode=SqlTestMode(mode))


def _parse_header_mapping(*, file_path: Path, header_contents: str) -> dict[str, object]:
    try:
        return parse_sql_header(header=header_contents)
    except ModelHeaderSyntaxError as error:
        raise SqlTestParseError(
            f"TEST() header in '{file_path}' uses invalid SQLBuild syntax: {error}"
        ) from error
