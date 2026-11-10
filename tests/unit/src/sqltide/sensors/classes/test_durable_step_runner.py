"""Behavior tests for durable step memoization and re-execution policies."""

import pytest

from sqltide._native import DurableStepRunner, InMemoryStepStore
from sqltide.sensors.exceptions import SensorStepError
from sqltide.sensors.types import StepPolicy
from tests.unit.src.sqltide.sensors.classes._test_types import StepRunnerTestCase
from tests.unit.src.sqltide.sensors.classes.helpers import CountingStep, FailingStep


@pytest.mark.parametrize(
    "test_case",
    [
        StepRunnerTestCase(
            description="at-least-once steps memoize their result across re-runs",
            expected_first_value="ticket-42",
            expected_second_value="ticket-42",
            expected_call_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_succeeded_step_when_rerunning_then_memoized_value_is_returned(
    test_case: StepRunnerTestCase,
) -> None:
    runner: DurableStepRunner = DurableStepRunner(store=InMemoryStepStore())
    step: CountingStep = CountingStep()

    first: object = runner.run("jira", step)
    second: object = runner.run("jira", step)

    assert first == test_case.expected_first_value
    assert second == test_case.expected_second_value
    assert step.calls == test_case.expected_call_count


@pytest.mark.parametrize(
    "test_case",
    [
        StepRunnerTestCase(
            description="at-most-once steps never re-invoke after a failure",
            expected_first_value=None,
            expected_second_value=None,
            expected_call_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_failed_at_most_once_step_when_rerunning_then_it_never_reinvokes(
    test_case: StepRunnerTestCase,
) -> None:
    runner: DurableStepRunner = DurableStepRunner(store=InMemoryStepStore())
    step: FailingStep = FailingStep()

    with pytest.raises(RuntimeError, match="slack unavailable"):
        _ = runner.run("pager", step, StepPolicy.AT_MOST_ONCE)
    with pytest.raises(SensorStepError, match="already attempted under at-most-once"):
        _ = runner.run("pager", step, StepPolicy.AT_MOST_ONCE)

    assert step.calls == test_case.expected_call_count


@pytest.mark.parametrize(
    "test_case",
    [
        StepRunnerTestCase(
            description="at-most-once steps memoize successful results",
            expected_first_value="ticket-42",
            expected_second_value="ticket-42",
            expected_call_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_succeeded_at_most_once_step_when_rerunning_then_memoized_value_returns(
    test_case: StepRunnerTestCase,
) -> None:
    runner: DurableStepRunner = DurableStepRunner(store=InMemoryStepStore())
    step: CountingStep = CountingStep()

    first: object = runner.run("pager", step, StepPolicy.AT_MOST_ONCE)
    second: object = runner.run("pager", step, StepPolicy.AT_MOST_ONCE)

    assert first == test_case.expected_first_value
    assert second == test_case.expected_second_value
    assert step.calls == test_case.expected_call_count


@pytest.mark.parametrize(
    "test_case",
    [
        StepRunnerTestCase(
            description="steps must return JSON-serializable values",
            expected_first_value=None,
            expected_second_value=None,
            expected_call_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_non_json_result_when_running_step_then_a_step_error_is_raised(
    test_case: StepRunnerTestCase,
) -> None:
    runner: DurableStepRunner = DurableStepRunner(store=InMemoryStepStore())
    step: CountingStep = CountingStep(value=object())

    with pytest.raises(SensorStepError, match="not JSON-serializable"):
        _ = runner.run("jira", step)

    assert step.calls == test_case.expected_call_count
