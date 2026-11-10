"""Inventory test: every closed observation surface has an explicit event decision.

Growing any of these enums breaks this test until the new member is classified in
the event catalog and enumerated here with its explicit event-or-no-event decision.
"""

import pytest

from sqltide._native import events_from_node_result
from sqltide.compiler.quality.types import QualityNodeKind
from sqltide.events.models import AuditCompleted, NodeResultObservation
from sqltide.events.types import AuditTransition, ObservedCommand
from sqltide.executor.auditing.types import QualityResultStatus
from tests.unit.src.sqltide.events._test_types import (
    CatalogClosureTestCase,
    NodeResultEventTestCase,
)
from tests.unit.src.sqltide.events.helpers import build_node_result_observation


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogClosureTestCase(
            description="the catalog inventory covers exactly the closed enums",
            expected_node_kinds=frozenset({"audit", "test"}),
            expected_statuses=frozenset({"passed", "warning", "failed", "error", "deferred"}),
            expected_commands=frozenset(
                {
                    "audit",
                    "test",
                    "build",
                    "deployment promote",
                    "janitor",
                    "destroy pipelines",
                    "reset target",
                }
            ),
            expected_transitions=frozenset(
                {"new_failure", "still_failing", "recovered", "still_passing"}
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_closed_enums_when_inventorying_then_every_member_is_classified(
    test_case: CatalogClosureTestCase,
) -> None:
    assert frozenset(str(kind) for kind in QualityNodeKind) == test_case.expected_node_kinds
    assert frozenset(str(status) for status in QualityResultStatus) == test_case.expected_statuses
    assert frozenset(str(command) for command in ObservedCommand) == test_case.expected_commands
    assert (
        frozenset(str(transition) for transition in AuditTransition)
        == test_case.expected_transitions
    )


@pytest.mark.parametrize(
    "test_case",
    [
        NodeResultEventTestCase(
            description="audit passed maps to one AuditCompleted",
            node_kind="audit",
            status=QualityResultStatus.PASSED,
            previous_status=None,
            expected_event_count=1,
            expected_transitions=(AuditTransition.STILL_PASSING,),
        ),
        NodeResultEventTestCase(
            description="audit warning maps to one AuditCompleted",
            node_kind="audit",
            status=QualityResultStatus.WARNING,
            previous_status=None,
            expected_event_count=1,
            expected_transitions=(AuditTransition.NEW_FAILURE,),
        ),
        NodeResultEventTestCase(
            description="audit failed maps to one AuditCompleted",
            node_kind="audit",
            status=QualityResultStatus.FAILED,
            previous_status=None,
            expected_event_count=1,
            expected_transitions=(AuditTransition.NEW_FAILURE,),
        ),
        NodeResultEventTestCase(
            description="audit error maps to one AuditCompleted",
            node_kind="audit",
            status=QualityResultStatus.ERROR,
            previous_status=None,
            expected_event_count=1,
            expected_transitions=(AuditTransition.NEW_FAILURE,),
        ),
        NodeResultEventTestCase(
            description="audit deferred is an explicit no-event decision",
            node_kind="audit",
            status=QualityResultStatus.DEFERRED,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="test passed is an explicit no-event decision",
            node_kind="test",
            status=QualityResultStatus.PASSED,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="test warning is an explicit no-event decision",
            node_kind="test",
            status=QualityResultStatus.WARNING,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="test failed is an explicit no-event decision",
            node_kind="test",
            status=QualityResultStatus.FAILED,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="test error is an explicit no-event decision",
            node_kind="test",
            status=QualityResultStatus.ERROR,
            previous_status=None,
            expected_event_count=0,
        ),
        NodeResultEventTestCase(
            description="test deferred is an explicit no-event decision",
            node_kind="test",
            status=QualityResultStatus.DEFERRED,
            previous_status=None,
            expected_event_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_every_kind_status_combination_when_deriving_then_decision_is_explicit(
    test_case: NodeResultEventTestCase,
) -> None:
    row: NodeResultObservation = build_node_result_observation(
        node_kind=test_case.node_kind, status=test_case.status
    )

    events: tuple[AuditCompleted, ...] = events_from_node_result(
        row=row, previous_status=test_case.previous_status, target="uat"
    )

    assert len(events) == test_case.expected_event_count
