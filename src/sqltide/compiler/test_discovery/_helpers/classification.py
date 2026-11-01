"""Apache-2.0: SQLBuild compile/_helpers/sql_tests/core.py classification@7e3b2f854f05."""

from __future__ import annotations

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.discovery.types import SqlRelationType
from sqltide.compiler.sql_analysis.main._extract_references import extract_references
from sqltide.compiler.sql_analysis.models import SqlReference
from sqltide.compiler.test_discovery.constants import (
    ASSERTION_CTE_ROLE,
    EXPECTED_CTE_ROLE,
    HELPER_CTE_ROLE,
    MACRO_ACTUAL_CTE_ROLE,
    MACRO_EXPECTED_CTE_ROLE,
    REF_CTE_PREFIX,
    REF_MOCK_CTE_ROLE,
    SOURCE_CTE_PREFIX,
)
from sqltide.compiler.test_discovery.exceptions import SqlTestParseError
from sqltide.compiler.test_discovery.models import (
    SqlTestCte,
    SqlTestMacroPayload,
    SqlTestMock,
    SqlTestModelPayload,
)


def classify_model_test_ctes(
    *, ctes: tuple[SqlTestCte, ...], file_path: Path
) -> tuple[tuple[SqlTestCte, ...], SqlTestModelPayload]:
    """Split model-mode CTEs into helpers, mocks, expectations, and assertions."""

    authored_ctes: list[SqlTestCte] = []
    mocks: list[SqlTestMock] = []
    expected_targets: list[SqlTestCte] = []
    assertions: list[SqlTestCte] = []
    cte: SqlTestCte
    role: str
    for cte, role in zip(
        ctes, _roles(ctes=ctes, file_path=file_path, macro_mode=False), strict=True
    ):
        if role == EXPECTED_CTE_ROLE:
            expected_targets.append(cte)
        elif role == ASSERTION_CTE_ROLE:
            assertions.append(cte)
        else:
            authored_ctes.append(cte)
            if role != HELPER_CTE_ROLE:
                mocks.append(_mock(cte=cte, is_ref=role == REF_MOCK_CTE_ROLE))
    return tuple(authored_ctes), SqlTestModelPayload(
        mocks=tuple(mocks),
        expected_targets=tuple(expected_targets),
        assertions=tuple(assertions),
        assertion_reference_names=_assertion_reference_names(assertions=tuple(assertions)),
    )


def classify_macro_test_ctes(
    *, ctes: tuple[SqlTestCte, ...], file_path: Path
) -> tuple[tuple[SqlTestCte, ...], SqlTestMacroPayload]:
    """Split macro-mode CTEs into helper CTEs and the actual/expected pair."""

    authored_ctes: list[SqlTestCte] = []
    actual_ctes: list[SqlTestCte] = []
    expected_ctes: list[SqlTestCte] = []
    cte: SqlTestCte
    role: str
    for cte, role in zip(
        ctes, _roles(ctes=ctes, file_path=file_path, macro_mode=True), strict=True
    ):
        if role == MACRO_ACTUAL_CTE_ROLE:
            actual_ctes.append(cte)
        elif role == MACRO_EXPECTED_CTE_ROLE:
            expected_ctes.append(cte)
        else:
            authored_ctes.append(cte)
    return tuple(authored_ctes), SqlTestMacroPayload(
        actual=actual_ctes[-1], expected=expected_ctes[-1]
    )


def _roles(*, ctes: tuple[SqlTestCte, ...], file_path: Path, macro_mode: bool) -> list[str]:
    try:
        return _native.classify_sql_test_ctes([cte.name for cte in ctes], macro_mode)
    except ValueError as error:
        raise SqlTestParseError(f"SQL test '{file_path}' {error}") from None


def _mock(*, cte: SqlTestCte, is_ref: bool) -> SqlTestMock:
    return SqlTestMock(
        cte_name=cte.name,
        name=cte.name.removeprefix(REF_CTE_PREFIX if is_ref else SOURCE_CTE_PREFIX),
        relation_type=SqlRelationType.REF if is_ref else SqlRelationType.SOURCE,
        query=cte.query,
    )


def _assertion_reference_names(*, assertions: tuple[SqlTestCte, ...]) -> tuple[str, ...]:
    names: list[str] = []
    assertion: SqlTestCte
    for assertion in assertions:
        reference: SqlReference
        for reference in extract_references(assertion.query):
            if reference.relation_type == SqlRelationType.REF:
                names.append(reference.name)
    return tuple(dict.fromkeys(names))
