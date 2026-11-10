from pathlib import Path

import pytest

from sqltide._native import (
    CliStyle,
    activity_line,
    reload_summary,
    startup_lines,
)
from sqltide.dev_server.models import CompileErrorInfo, CompileOutcome
from sqltide.dev_server.types import ActivityTone, CompileStateKind
from tests.unit.src.sqltide.cli.dev._helpers._test_types import (
    ActivityLineTestCase,
    ReloadSummaryTestCase,
    StartupLinesTestCase,
)

_PLAIN_STYLE: CliStyle = CliStyle(use_color=False)

_OK_OUTCOME: CompileOutcome = CompileOutcome(
    state=CompileStateKind.OK, version_key="v1", compiled_at="2026-08-04T00:00:00+00:00"
)

_FAILING_OUTCOME: CompileOutcome = CompileOutcome(
    state=CompileStateKind.FAILING,
    version_key="v2",
    compiled_at="2026-08-04T00:00:00+00:00",
    error=CompileErrorInfo(
        message="Unknown macro 'frobnicate'",
        path="pipelines/orders.sql",
        line=12,
        column=4,
    ),
)


@pytest.mark.parametrize(
    "test_case",
    [
        StartupLinesTestCase(
            description="an ok compile shows state, urls, database, and version",
            outcome=_OK_OUTCOME,
            database="orders_demo",
            expected_fragments=(
                "SQLTide dev server v1.2.3",
                "compile",
                "ok",
                "connected · orders_demo",
                "http://127.0.0.1:8000",
                "http://127.0.0.1:8000/api",
                "ctrl+c to stop",
            ),
        ),
        StartupLinesTestCase(
            description="a failing compile shows the error location and message",
            outcome=_FAILING_OUTCOME,
            database=None,
            expected_fragments=(
                "failing",
                "pipelines/orders.sql:12:4",
                "Unknown macro 'frobnicate'",
                "not connected",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_compile_outcome_when_rendering_startup_then_banner_contains_expected_fragments(
    test_case: StartupLinesTestCase,
) -> None:
    lines: tuple[str, ...] = startup_lines(
        style=_PLAIN_STYLE,
        outcome=test_case.outcome,
        project_dir=Path("/tmp/orders_demo"),
        database=test_case.database,
        host="127.0.0.1",
        port=8000,
        tool_version="1.2.3",
    )

    banner: str = "\n".join(lines)
    assert all(fragment in banner for fragment in test_case.expected_fragments)


@pytest.mark.parametrize(
    "test_case",
    [
        ReloadSummaryTestCase(
            description="an ok reload reads as good news",
            outcome=_OK_OUTCOME,
            expected_status="ok",
            expected_tone=ActivityTone.GOOD,
            expected_detail="",
        ),
        ReloadSummaryTestCase(
            description="a failing reload carries the compile error message",
            outcome=_FAILING_OUTCOME,
            expected_status="failing",
            expected_tone=ActivityTone.BAD,
            expected_detail="Unknown macro 'frobnicate'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_reload_outcome_when_summarising_then_returns_expected_activity_triple(
    test_case: ReloadSummaryTestCase,
) -> None:
    status: str
    tone: ActivityTone
    detail: str
    status, tone, detail = reload_summary(outcome=test_case.outcome)

    assert status == test_case.expected_status
    assert tone == test_case.expected_tone
    assert detail == test_case.expected_detail


@pytest.mark.parametrize(
    "test_case",
    [
        ActivityLineTestCase(
            description="columns are padded so consecutive lines align",
            category="build",
            status="started",
            tone=ActivityTone.NEUTRAL,
            detail="build --select orders --auto-approve --events",
            expected_line=(
                "12:00:00  build   started    build --select orders --auto-approve --events"
            ),
        ),
        ActivityLineTestCase(
            description="an empty detail leaves no trailing whitespace",
            category="reload",
            status="ok",
            tone=ActivityTone.GOOD,
            detail="",
            expected_line="12:00:00  reload  ok",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_activity_fields_when_rendering_line_then_matches_expected_alignment(
    test_case: ActivityLineTestCase,
) -> None:
    line: str = activity_line(
        style=_PLAIN_STYLE,
        timestamp="12:00:00",
        category=test_case.category,
        status=test_case.status,
        tone=test_case.tone,
        detail=test_case.detail,
    )

    assert line == test_case.expected_line
