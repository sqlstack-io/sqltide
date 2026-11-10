"""Behavior tests for audit event derivation and transition computation."""

import pytest

from sqltide._native import events_from_node_result
from sqltide.events.models import AuditCompleted, NodeResultObservation
from sqltide.events.types import AuditTransition
from sqltide.executor.auditing.types import QualityResultStatus
from tests.unit.src.sqltide.events.helpers import build_node_result_observation
from tests.unit.src.sqltide.events.main._test_types import (
    AuditSampleTestCase,
    AuditTransitionTestCase,
    NodeResultEventTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        AuditTransitionTestCase(
            description="first failure without history is a new failure",
            status=QualityResultStatus.FAILED,
            previous_status=None,
            expected_transition=AuditTransition.NEW_FAILURE,
        ),
        AuditTransitionTestCase(
            description="failure after a pass is a new failure",
            status=QualityResultStatus.FAILED,
            previous_status=QualityResultStatus.PASSED,
            expected_transition=AuditTransition.NEW_FAILURE,
        ),
        AuditTransitionTestCase(
            description="error after a failure is still failing",
            status=QualityResultStatus.ERROR,
            previous_status=QualityResultStatus.FAILED,
            expected_transition=AuditTransition.STILL_FAILING,
        ),
        AuditTransitionTestCase(
            description="pass after a failure is a recovery",
            status=QualityResultStatus.PASSED,
            previous_status=QualityResultStatus.ERROR,
            expected_transition=AuditTransition.RECOVERED,
        ),
        AuditTransitionTestCase(
            description="pass after a pass is still passing",
            status=QualityResultStatus.PASSED,
            previous_status=QualityResultStatus.PASSED,
            expected_transition=AuditTransition.STILL_PASSING,
        ),
        AuditTransitionTestCase(
            description="warning after a failure is still failing",
            status=QualityResultStatus.WARNING,
            previous_status=QualityResultStatus.FAILED,
            expected_transition=AuditTransition.STILL_FAILING,
        ),
        AuditTransitionTestCase(
            description="first warning without history is a new failure",
            status=QualityResultStatus.WARNING,
            previous_status=None,
            expected_transition=AuditTransition.NEW_FAILURE,
        ),
        AuditTransitionTestCase(
            description="pass after a warning is a recovery",
            status=QualityResultStatus.PASSED,
            previous_status=QualityResultStatus.WARNING,
            expected_transition=AuditTransition.RECOVERED,
        ),
        AuditTransitionTestCase(
            description="first pass without history is still passing",
            status=QualityResultStatus.PASSED,
            previous_status=None,
            expected_transition=AuditTransition.STILL_PASSING,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_previous_status_when_deriving_audit_event_then_transition_matches(
    test_case: AuditTransitionTestCase,
) -> None:
    row: NodeResultObservation = build_node_result_observation(status=test_case.status)

    events: tuple[AuditCompleted, ...] = events_from_node_result(
        row=row, previous_status=test_case.previous_status, target="uat"
    )

    assert len(events) == 1
    assert events[0].transition is test_case.expected_transition
    assert events[0].id == row.result_id
    assert events[0].audit_name == row.node_name
    assert events[0].target == "uat"
    assert events[0].previous_status is test_case.previous_status


@pytest.mark.parametrize(
    "test_case",
    [
        AuditSampleTestCase(
            description="persisted samples are exposed on the event",
            payload_json=(
                '{"sample_column_names":["race_source_key","expected_runners",'
                '"actual_runners"],"sample_rows":[["1672326",13,12],'
                '["1672261",12,11]]}'
            ),
            expected_column_names=(
                "race_source_key",
                "expected_runners",
                "actual_runners",
            ),
            expected_rows=(("1672326", 13, 12), ("1672261", 12, 11)),
        ),
        AuditSampleTestCase(
            description="malformed payloads produce an empty sample",
            payload_json="not-json",
            expected_column_names=(),
            expected_rows=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_payload_when_deriving_event_then_sample_is_safe(
    test_case: AuditSampleTestCase,
) -> None:
    row: NodeResultObservation = build_node_result_observation(payload_json=test_case.payload_json)

    events: tuple[AuditCompleted, ...] = events_from_node_result(
        row=row, previous_status=None, target="uat"
    )

    assert events[0].sample_column_names == test_case.expected_column_names
    assert events[0].sample_rows == test_case.expected_rows


@pytest.mark.parametrize(
    "test_case",
    [
        NodeResultEventTestCase(
            description="deferred audits produce no event",
            node_kind="audit",
            status=QualityResultStatus.DEFERRED,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="sql tests produce no event",
            node_kind="test",
            status=QualityResultStatus.FAILED,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="deferred history is ignored for transitions",
            node_kind="audit",
            status=QualityResultStatus.FAILED,
            previous_status=QualityResultStatus.DEFERRED,
            expected_event_count=1,
            expected_transitions=(AuditTransition.NEW_FAILURE,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_node_result_when_deriving_events_then_explicit_decisions_apply(
    test_case: NodeResultEventTestCase,
) -> None:
    row: NodeResultObservation = build_node_result_observation(
        node_kind=test_case.node_kind, status=test_case.status
    )

    events: tuple[AuditCompleted, ...] = events_from_node_result(
        row=row, previous_status=test_case.previous_status, target="uat"
    )

    assert len(events) == test_case.expected_event_count
    assert tuple(event.transition for event in events) == test_case.expected_transitions
