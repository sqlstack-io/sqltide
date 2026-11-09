import pytest

from sqltide._native import render_row_error_policy_line
from sqltide.compiler.compile.models import LogicalResourceKey, LogicalResourceType
from sqltide.compiler.discovery.types import RowErrorPolicy
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from tests.unit.src.sqltide.cli.plan._test_types import RowErrorPolicyLineTestCase


@pytest.mark.parametrize(
    "test_case",
    [
        RowErrorPolicyLineTestCase(
            description="a policy switch shows the new policy",
            landing_repair_only=False,
            expected_line="view-only  orders: on_row_error -> fail, recreate mv__orders, no replay",
        ),
        RowErrorPolicyLineTestCase(
            description="a landing repair does not mention a policy",
            landing_repair_only=True,
            expected_line="repair landing view  orders: recreate mv__raw, no replay",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_policy_change_when_rendering_then_repairs_are_a_separate_line(
    test_case: RowErrorPolicyLineTestCase,
) -> None:
    change: DirectRowErrorPolicyChange = DirectRowErrorPolicyChange(
        model_key=LogicalResourceKey(name="orders", resource_type=LogicalResourceType.MODEL),
        view_name="mv__orders",
        dead_letter_view_name="mv__orders__dead_letters",
        desired_policy=RowErrorPolicy.FAIL,
        landing_view_names=("mv__raw",),
        landing_repair_only=test_case.landing_repair_only,
    )

    assert render_row_error_policy_line(change) == test_case.expected_line


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
