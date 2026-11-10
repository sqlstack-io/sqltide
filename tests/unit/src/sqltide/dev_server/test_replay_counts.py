import pytest

from sqltide._native import (
    build_replay_count_query,
    replay_time_column,
)
from sqltide.adapter.models import AdapterReplayColumns
from tests.unit.src.sqltide.dev_server._test_types import (
    ReplayCountQueryTestCase,
    ReplayTimeColumnTestCase,
)

_COLUMNS: AdapterReplayColumns = AdapterReplayColumns(
    partition="_replay_partition",
    offset="_replay_offset",
    timestamp="event_time",
    landed_at="_replay_landed_at",
    cursor="_replay_cursor",
)


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayCountQueryTestCase(
            description="full replay counts the whole landing table",
            start_time=None,
            expected_query="SELECT count() AS rows FROM `analytics`.`raw__orders`",
        ),
        ReplayCountQueryTestCase(
            description="windowed replay mirrors the executor lower-bound predicate",
            start_time="2026-08-01 00:00:00.000",
            expected_query=(
                "SELECT count() AS rows FROM `analytics`.`raw__orders` "
                "WHERE `_replay_landed_at` >= "
                "toDateTime64('2026-08-01 00:00:00.000', 3, 'UTC')"
            ),
        ),
        ReplayCountQueryTestCase(
            description="quotes in the start literal are escaped",
            start_time="2026-08-01' OR 1=1 --",
            expected_query=(
                "SELECT count() AS rows FROM `analytics`.`raw__orders` "
                "WHERE `_replay_landed_at` >= "
                "toDateTime64('2026-08-01\\' OR 1=1 --', 3, 'UTC')"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_replay_window_when_building_count_query_then_mirrors_build_predicate(
    test_case: ReplayCountQueryTestCase,
) -> None:
    query: str = build_replay_count_query(
        database="analytics",
        relation_name="raw__orders",
        time_column="_replay_landed_at",
        start_time=test_case.start_time,
    )

    assert query == test_case.expected_query


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayTimeColumnTestCase(
            description="landed_at mode compares against the landed-at column",
            boundary_mode="landed_at",
            expected_column="_replay_landed_at",
        ),
        ReplayTimeColumnTestCase(
            description="timestamp mode compares against the event-time column",
            boundary_mode="timestamp",
            expected_column="event_time",
        ),
        ReplayTimeColumnTestCase(
            description="cursor mode selects its lower bound by event time",
            boundary_mode="cursor",
            expected_column="event_time",
        ),
        ReplayTimeColumnTestCase(
            description="offsets mode prefers landed-at for the boundary lookup",
            boundary_mode="offsets",
            expected_column="_replay_landed_at",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_boundary_mode_when_choosing_time_column_then_matches_executor(
    test_case: ReplayTimeColumnTestCase,
) -> None:
    column: str = replay_time_column(boundary_mode=test_case.boundary_mode, columns=_COLUMNS)

    assert column == test_case.expected_column
