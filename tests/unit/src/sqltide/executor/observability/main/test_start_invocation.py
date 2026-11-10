import pytest

from sqltide._native import start_invocation
from tests.unit.src.sqltide.executor.observability.main._test_types import (
    StartInvocationTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        StartInvocationTestCase(
            description="parent identity is retained",
            expected_invocation_id="parent-assigned-invocation",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_parent_identity_when_starting_invocation_then_preserves_identity(
    test_case: StartInvocationTestCase,
) -> None:
    invocation_id: str = test_case.expected_invocation_id

    started: tuple[str, str, int] = start_invocation(invocation_id=invocation_id)

    assert started[0] == test_case.expected_invocation_id
