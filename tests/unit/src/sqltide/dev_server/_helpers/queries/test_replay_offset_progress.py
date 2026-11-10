import pytest

from sqltide._native import (
    calculate_replay_offset_progress,
    decode_replay_offset_progress_request,
)
from sqltide.adapter.models import (
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterReplayOffsetRange,
)
from sqltide.dev_server.models import ReplayOffsetProgress
from tests.unit.src.sqltide.dev_server._helpers.queries._test_types import (
    ReplayOffsetProgressCalculationTestCase,
    ReplayOffsetProgressDecodeTestCase,
)
from tests.unit.src.sqltide.dev_server._helpers.queries.helpers import replay_progress_request


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayOffsetProgressCalculationTestCase(
            description="uneven sparse ranges are weighted by offset span",
            ranges=(
                AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=200),
                AdapterReplayOffsetRange(partition=1, lower_offset=1000, upper_offset=1900),
            ),
            frontiers=(
                AdapterReplayOffsetFrontier(partition=0, completed_offset=150),
                AdapterReplayOffsetFrontier(partition=1, completed_offset=1450),
            ),
            elapsed_seconds=10,
            completed=False,
            expected_progress=ReplayOffsetProgress(50.0, 10.0, 500, 1000, 2, 2),
        ),
        ReplayOffsetProgressCalculationTestCase(
            description="frontiers below and above captured ranges are clamped",
            ranges=(
                AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=200),
                AdapterReplayOffsetRange(partition=1, lower_offset=300, upper_offset=500),
                AdapterReplayOffsetRange(partition=2, lower_offset=600, upper_offset=700),
            ),
            frontiers=(
                AdapterReplayOffsetFrontier(partition=0, completed_offset=50),
                AdapterReplayOffsetFrontier(partition=1, completed_offset=900),
            ),
            elapsed_seconds=20,
            completed=False,
            expected_progress=ReplayOffsetProgress(50.0, 20.0, 200, 400, 2, 3),
        ),
        ReplayOffsetProgressCalculationTestCase(
            description="filtered tails reach one hundred when the statement completes",
            ranges=(AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=200),),
            frontiers=(AdapterReplayOffsetFrontier(partition=0, completed_offset=180),),
            elapsed_seconds=10,
            completed=True,
            expected_progress=ReplayOffsetProgress(100.0, 0.0, 100, 100, 1, 1),
        ),
        ReplayOffsetProgressCalculationTestCase(
            description="missing frontiers start at zero without an ETA",
            ranges=(AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=200),),
            frontiers=(),
            elapsed_seconds=10,
            completed=False,
            expected_progress=ReplayOffsetProgress(0.0, None, 0, 100, 0, 1),
        ),
        ReplayOffsetProgressCalculationTestCase(
            description="zero-width ranges retain indeterminate progress",
            ranges=(AdapterReplayOffsetRange(partition=0, lower_offset=100, upper_offset=100),),
            frontiers=(),
            elapsed_seconds=10,
            completed=False,
            expected_progress=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_captured_ranges_when_calculating_then_progress_is_truthful(
    test_case: ReplayOffsetProgressCalculationTestCase,
) -> None:
    request: AdapterReplayOffsetProgressRequest = replay_progress_request(*test_case.ranges)

    progress: ReplayOffsetProgress | None = calculate_replay_offset_progress(
        request=request,
        frontiers=test_case.frontiers,
        elapsed_seconds=test_case.elapsed_seconds,
        completed=test_case.completed,
    )

    assert progress == test_case.expected_progress


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayOffsetProgressDecodeTestCase(
            description="truncated range collection is ignored",
            metadata={"ranges": "truncated"},
            expected_available=False,
        ),
        ReplayOffsetProgressDecodeTestCase(
            description="malformed partition is ignored",
            metadata={"ranges": [{"partition": "bad"}]},
            expected_available=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_historical_metadata_when_decoding_then_malformed_values_are_ignored(
    test_case: ReplayOffsetProgressDecodeTestCase,
) -> None:
    decoded: AdapterReplayOffsetProgressRequest | None = decode_replay_offset_progress_request(
        test_case.metadata
    )

    assert (decoded is not None) is test_case.expected_available
