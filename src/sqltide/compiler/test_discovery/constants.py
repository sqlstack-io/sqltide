"""Constants for SQL-native test discovery."""

from __future__ import annotations

from sqltide.compiler.test_discovery.types import SqlTestMode

EXPECTED_CTE_PREFIX: str = "__expected__"
REF_CTE_PREFIX: str = "__ref__"
SOURCE_CTE_PREFIX: str = "__source__"
ASSERT_CTE_PREFIX: str = "__assert__"
MACRO_ACTUAL_CTE_NAME: str = "__macro_actual__"
MACRO_EXPECTED_CTE_NAME: str = "__macro_expected__"
MACRO_TARGET_LABEL_PREFIX: str = "macro "

DEFAULT_SQL_TEST_MODE: SqlTestMode = SqlTestMode.MODEL
CEREMONIAL_SELECT_KEYWORD: str = "SELECT"
CEREMONIAL_SELECT_LITERAL: str = "1"
SQL_TEST_SCANNER_CONTEXT: str = "SQL test"

HELPER_CTE_ROLE: str = "helper"
REF_MOCK_CTE_ROLE: str = "ref_mock"
EXPECTED_CTE_ROLE: str = "expected"
ASSERTION_CTE_ROLE: str = "assertion"
MACRO_ACTUAL_CTE_ROLE: str = "macro_actual"
MACRO_EXPECTED_CTE_ROLE: str = "macro_expected"
