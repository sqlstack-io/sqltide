from __future__ import annotations

import pytest

from sqltide._native import normalize_cli_start_time
from sqltide.cli.entry.exceptions import CliUserError
from tests.unit.src.sqltide.cli.plan.main._test_types import (
    CliStartTimeNormalizationErrorTestCase,
    CliStartTimeNormalizationTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliStartTimeNormalizationTestCase(
            description="normalizes plain date to midnight utc",
            raw_value="2026-04-01",
            expected_normalized_value="2026-04-01 00:00:00.000",
        ),
        CliStartTimeNormalizationTestCase(
            description="normalizes utc datetime without z suffix",
            raw_value="2026-04-01T12:30:45",
            expected_normalized_value="2026-04-01 12:30:45.000",
        ),
        CliStartTimeNormalizationTestCase(
            description="normalizes fractional utc datetime with z suffix",
            raw_value="2026-04-01T12:30:45.123Z",
            expected_normalized_value="2026-04-01 12:30:45.123",
        ),
        CliStartTimeNormalizationTestCase(
            description="preserves first repeated-hour utc instant",
            raw_value="2026-11-01T05:30:00Z",
            expected_normalized_value="2026-11-01 05:30:00.000",
        ),
        CliStartTimeNormalizationTestCase(
            description="preserves second repeated-hour utc instant",
            raw_value="2026-11-01T06:30:00Z",
            expected_normalized_value="2026-11-01 06:30:00.000",
        ),
        CliStartTimeNormalizationTestCase(
            description="pads years before 1000 to four digits",
            raw_value="0999-01-01T00:00:00",
            expected_normalized_value="0999-01-01 00:00:00.000",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_cli_start_time_when_normalizing_then_it_returns_internal_timestamp(
    test_case: CliStartTimeNormalizationTestCase,
) -> None:
    normalized_value: str = normalize_cli_start_time(test_case.raw_value)

    assert normalized_value == test_case.expected_normalized_value


@pytest.mark.parametrize(
    "test_case",
    [
        CliStartTimeNormalizationErrorTestCase(
            description="rejects non utc offset timestamps",
            raw_value="2026-04-01T12:30:45+02:00",
            expected_error_fragment="--start-time must be YYYY-MM-DD",
        ),
        CliStartTimeNormalizationErrorTestCase(
            description="rejects non-ascii digits",
            raw_value="\u0662\u0660\u0662\u0666-04-01",
            expected_error_fragment="--start-time must be YYYY-MM-DD",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_cli_start_time_when_normalizing_then_it_raises_clear_error(
    test_case: CliStartTimeNormalizationErrorTestCase,
) -> None:
    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        normalize_cli_start_time(test_case.raw_value)
