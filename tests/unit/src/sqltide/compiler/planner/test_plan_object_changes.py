import pytest

from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.main.plan_deployment import plan_deployment
from sqltide.compiler.planner.models import ActualState, PlannedObjectChange
from tests.unit.src.sqltide.compiler.planner._test_types import (
    PlannerObjectChangeTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_example_actual_state,
    build_example_desired_state,
    key_parts,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerObjectChangeTestCase(
            description="classifies conservative desired versus actual object changes",
            expected_changes=(
                ((None, "kafka_table", "kafka__orders"), "no_op"),
                ((None, "materialized_view", "mv__orders"), "no_op"),
                ((None, "materialized_view", "mv__orders_enriched"), "create"),
                ((None, "table", "raw__orders"), "rebuild"),
                ((None, "table", "tbl__orders_enriched"), "create"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_desired_and_actual_state_when_planning_changes_then_it_returns_expected_changes(
    test_case: PlannerObjectChangeTestCase,
) -> None:
    desired_state: DesiredState = build_example_desired_state()
    actual_state: ActualState = build_example_actual_state()

    object_changes: tuple[PlannedObjectChange, ...] = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    ).object_changes

    assert tuple((key_parts(change.key), change.change_type) for change in object_changes) == (
        test_case.expected_changes
    )
