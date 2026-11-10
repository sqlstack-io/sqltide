import pytest

from sqltide._native import (
    inspect_root_deployment_state,
)
from sqltide.adapter.models import (
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.planner.models import (
    RootDeploymentInspection,
)
from tests.unit.src.sqltide.compiler.planner._test_types import (
    InspectRootDeploymentStateTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        InspectRootDeploymentStateTestCase(
            description="classifies active view binding as active deployment",
            active_bindings=(
                (
                    "tbl__orders_enriched",
                    "tbl__orders_enriched__20260726T180000Z_depa01",
                ),
            ),
            physical_candidates=(
                (
                    "tbl__orders_enriched",
                    "tbl__orders_enriched__20260726T180000Z_depa01",
                ),
            ),
            expected_state_kind="active_view_present",
            expected_active_deployment_id="20260726T180000Z_depa01",
        ),
        InspectRootDeploymentStateTestCase(
            description="classifies no view and no candidates as greenfield",
            active_bindings=(),
            physical_candidates=(),
            expected_state_kind="greenfield",
            expected_active_deployment_id=None,
        ),
        InspectRootDeploymentStateTestCase(
            description="classifies no view with candidates as logical view missing",
            active_bindings=(),
            physical_candidates=(
                (
                    "tbl__orders_enriched",
                    "tbl__orders_enriched__20260726T180000Z_depa01",
                ),
                (
                    "tbl__orders_enriched",
                    "tbl__orders_enriched__20260726T190000Z_depb02",
                ),
            ),
            expected_state_kind="logical_view_missing",
            expected_active_deployment_id=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_inspected_root_state_when_classifying_then_it_returns_expected_state_kind(
    test_case: InspectRootDeploymentStateTestCase,
) -> None:
    inspected_state: InspectedManagedTableState = InspectedManagedTableState(
        active_bindings=tuple(
            InspectedActiveTableBinding(
                database="analytics",
                logical_name=logical_name,
                physical_name=physical_name,
            )
            for logical_name, physical_name in test_case.active_bindings
        ),
        physical_candidates=tuple(
            InspectedPhysicalTableCandidate(
                database="analytics",
                logical_name=logical_name,
                physical_name=physical_name,
            )
            for logical_name, physical_name in test_case.physical_candidates
        ),
    )

    result: RootDeploymentInspection = inspect_root_deployment_state(
        inspected_state=inspected_state,
        root_key=ObjectKey(database="analytics", object_type="table", name="tbl__orders_enriched"),
    )

    assert result.state_kind == test_case.expected_state_kind
    assert result.active_deployment_id == test_case.expected_active_deployment_id
