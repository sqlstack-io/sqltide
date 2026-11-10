"""Behavior tests for run event derivation from terminal invocations."""

import pytest

from sqltide._native import events_from_invocation
from sqltide.events.models import InvocationObservation, RunCompleted
from tests.unit.src.sqltide.events.helpers import build_invocation_observation
from tests.unit.src.sqltide.events.main._test_types import InvocationEventTestCase


@pytest.mark.parametrize(
    "test_case",
    [
        InvocationEventTestCase(
            description="build invocations complete as run events",
            command="build",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="audit invocations complete as run events",
            command="audit",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="test invocations complete as run events",
            command="test",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="promotions complete as run events",
            command="deployment promote",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="janitor cleanups complete as run events",
            command="janitor",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="pipeline destruction completes as a run event",
            command="destroy pipelines",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="target reset completes as a run event",
            command="reset target",
            expected_event_count=1,
        ),
        InvocationEventTestCase(
            description="unrecognized historical commands produce no events",
            command="mystery",
            expected_event_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invocation_when_deriving_events_then_commands_map_exhaustively(
    test_case: InvocationEventTestCase,
) -> None:
    row: InvocationObservation = build_invocation_observation(command=test_case.command)

    events: tuple[RunCompleted, ...] = events_from_invocation(row=row, target="uat")

    assert len(events) == test_case.expected_event_count
    assert tuple(event.id for event in events) == (row.invocation_id,) * len(events)
    assert tuple(str(event.command) for event in events) == (test_case.command,) * len(events)
    assert tuple(event.target for event in events) == ("uat",) * len(events)
