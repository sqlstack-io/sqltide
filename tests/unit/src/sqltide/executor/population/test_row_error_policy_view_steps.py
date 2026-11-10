import pytest

from sqltide._native import row_error_policy_view_steps
from sqltide.compiler.compile.models import LogicalResourceKey, LogicalResourceType
from sqltide.compiler.discovery.types import RowErrorPolicy
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from tests.unit.src.sqltide.executor.population._test_types import PolicyViewStepsTestCase

_VIEW: str = "mv__orders"
_DEAD_LETTERS: str = "mv__orders__dead_letters"


@pytest.mark.parametrize(
    "test_case",
    [
        PolicyViewStepsTestCase(
            description="a view cut out of the selection is never dropped",
            desired_policy=RowErrorPolicy.QUARANTINE,
            renderable_views=(),
            expected_realized_steps=(),
        ),
        PolicyViewStepsTestCase(
            description="an unrenderable dead-letter view blocks an enable",
            desired_policy=RowErrorPolicy.QUARANTINE,
            renderable_views=(_VIEW,),
            expected_realized_steps=(),
        ),
        PolicyViewStepsTestCase(
            description="an enable creates the dead-letter view first",
            desired_policy=RowErrorPolicy.QUARANTINE,
            renderable_views=(_VIEW, _DEAD_LETTERS),
            expected_realized_steps=(_DEAD_LETTERS, f"replace_{_VIEW}", _VIEW),
        ),
        PolicyViewStepsTestCase(
            description="a disable retires the dead-letter view last",
            desired_policy=RowErrorPolicy.FAIL,
            renderable_views=(_VIEW,),
            expected_realized_steps=(f"replace_{_VIEW}", _VIEW, f"retire_{_DEAD_LETTERS}"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_policy_change_when_ordering_view_steps_then_only_renderable_views_are_dropped(
    test_case: PolicyViewStepsTestCase,
) -> None:
    change: DirectRowErrorPolicyChange = DirectRowErrorPolicyChange(
        model_key=LogicalResourceKey(name="orders", resource_type=LogicalResourceType.MODEL),
        view_name=_VIEW,
        dead_letter_view_name=_DEAD_LETTERS,
        desired_policy=test_case.desired_policy,
        landing_view_names=(),
        logical_view_name=_VIEW,
        logical_dead_letter_view_name=_DEAD_LETTERS,
    )
    sql_by_view: dict[str, str] = {name: "CREATE" for name in test_case.renderable_views}

    _, realizations = row_error_policy_view_steps(
        changes=(change,), database="shop", render_view=sql_by_view.get
    )

    assert tuple(name for name, _ in realizations) == test_case.expected_realized_steps


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
