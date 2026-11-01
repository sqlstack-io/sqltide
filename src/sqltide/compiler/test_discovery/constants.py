"""Constants for SQL-native test discovery."""

from __future__ import annotations

import re

from sqltide.compiler.test_discovery.types import SqlTestMode

EXPECTED_CTE_PREFIX: str = "__expected__"
REF_CTE_PREFIX: str = "__ref__"
SOURCE_CTE_PREFIX: str = "__source__"
ASSERT_CTE_PREFIX: str = "__assert__"
MACRO_ACTUAL_CTE_NAME: str = "__macro_actual__"
MACRO_EXPECTED_CTE_NAME: str = "__macro_expected__"
MACRO_TARGET_LABEL_PREFIX: str = "macro "
TEST_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*TEST\s*\((?P<header>.*?)\)\s*;\s*(?P<sql>.*)$",
    re.DOTALL,
)
TEST_HEADER_ONLY_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*TEST\s*\((?P<header>.*?)\)\s*;\s*",
    re.DOTALL | re.MULTILINE,
)

TEST_HEADER_NAME_KEY: str = "name"
TEST_HEADER_MODE_KEY: str = "mode"
TEST_HEADER_SUPPORTED_KEYS: frozenset[str] = frozenset({TEST_HEADER_NAME_KEY, TEST_HEADER_MODE_KEY})
DEFAULT_SQL_TEST_MODE: SqlTestMode = SqlTestMode.MODEL
SUPPORTED_SQL_TEST_MODES: str = ", ".join(mode.value for mode in SqlTestMode)
CEREMONIAL_SELECT_KEYWORD: str = "SELECT"
CEREMONIAL_SELECT_LITERAL: str = "1"
SQL_TEST_SCANNER_CONTEXT: str = "SQL test"

HELPER_CTE_ROLE: str = "helper"
REF_MOCK_CTE_ROLE: str = "ref_mock"
EXPECTED_CTE_ROLE: str = "expected"
ASSERTION_CTE_ROLE: str = "assertion"
MACRO_ACTUAL_CTE_ROLE: str = "macro_actual"
MACRO_EXPECTED_CTE_ROLE: str = "macro_expected"
