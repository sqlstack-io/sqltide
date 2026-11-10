"""Behavior tests for isolated sensor handler evaluation."""

import pytest

from sqltide._native import events_from_node_result
from sqltide.events.models import AuditCompleted
from sqltide.sensors import EventSensorContext
from sqltide.sensors._helpers.evaluation import evaluate_sensor_handler
from sqltide.sensors.models import SensorEvaluation
from sqltide.sensors.types import SensorTickStatus
from tests.unit.src.sqltide.events.helpers import build_node_result_observation
from tests.unit.src.sqltide.sensors._helpers._test_types import EvaluationTestCase
from tests.unit.src.sqltide.sensors._helpers.helpers import (
    hanging_handler,
    quiet_handler,
    raising_handler,
    skipping_handler,
)
from tests.unit.src.sqltide.sensors.helpers import build_loaded_sensor


@pytest.mark.parametrize(
    "test_case",
    [
        EvaluationTestCase(
            description="quiet handlers succeed",
            sensor=build_loaded_sensor(declaration=quiet_handler),
            expected_status=SensorTickStatus.SUCCEEDED,
        ),
        EvaluationTestCase(
            description="skip reasons are recorded verbatim",
            sensor=build_loaded_sensor(declaration=skipping_handler),
            expected_status=SensorTickStatus.SKIPPED,
            expected_skip_reason="nothing to do",
        ),
        EvaluationTestCase(
            description="handler exceptions become failed evaluations",
            sensor=build_loaded_sensor(declaration=raising_handler),
            expected_status=SensorTickStatus.FAILED,
            expected_error_fragment="ValueError: bad webhook",
        ),
        EvaluationTestCase(
            description="hung handlers time out without stalling the dispatcher",
            sensor=build_loaded_sensor(declaration=hanging_handler),
            expected_status=SensorTickStatus.FAILED,
            expected_error_fragment="timed out after 0.05 seconds",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_handler_when_evaluating_then_outcome_is_contained(
    test_case: EvaluationTestCase,
) -> None:
    events: tuple[AuditCompleted, ...] = events_from_node_result(
        row=build_node_result_observation(), previous_status=None, target="prod"
    )
    context: EventSensorContext[object] = EventSensorContext(event=events[0], target="prod")

    evaluation: SensorEvaluation = evaluate_sensor_handler(
        sensor=test_case.sensor, context=context, providers=()
    )

    assert evaluation.status is test_case.expected_status
    assert evaluation.skip_reason == test_case.expected_skip_reason
    assert (test_case.expected_error_fragment or "") in (evaluation.error_message or "")
