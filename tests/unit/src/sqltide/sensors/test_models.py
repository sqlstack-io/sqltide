"""Behavior tests for the authored sensor policy shapes."""

import pytest

from sqltide.sensors import SensorRetryPolicy
from sqltide.sensors.exceptions import SensorError
from tests.unit.src.sqltide.sensors._test_types import SensorRetryPolicyErrorTestCase


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRetryPolicyErrorTestCase(
            description="a retry policy needs at least one attempt",
            max_attempts=0,
            backoff_seconds=0.0,
            expected_message="SensorRetryPolicy.max_attempts must be at least 1",
        ),
        SensorRetryPolicyErrorTestCase(
            description="a retry policy rejects a negative backoff",
            max_attempts=3,
            backoff_seconds=-0.5,
            expected_message="SensorRetryPolicy.backoff_seconds must not be negative",
        ),
        SensorRetryPolicyErrorTestCase(
            description="the attempt count is checked before the backoff",
            max_attempts=0,
            backoff_seconds=-1.0,
            expected_message="SensorRetryPolicy.max_attempts must be at least 1",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_retry_policy_when_constructing_then_sensor_error_names_the_field(
    test_case: SensorRetryPolicyErrorTestCase,
) -> None:
    with pytest.raises(SensorError) as error_info:
        _ = SensorRetryPolicy(
            max_attempts=test_case.max_attempts, backoff_seconds=test_case.backoff_seconds
        )

    assert error_info.value.args == (test_case.expected_message,)
    assert str(error_info.value) == test_case.expected_message
    assert error_info.value.location is None
