import pytest

from sqltide._native import resolve_deployment_rollback
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.executor.promotion.exceptions import PublishExecutionError
from sqltide.executor.promotion.models import RollbackPlan, RollbackRequest
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.promotion.main._test_types import (
    RollbackInventoryErrorTestCase,
    RollbackPublicationOrderTestCase,
    RollbackResolutionErrorTestCase,
    RollbackResolutionSuccessTestCase,
)
from tests.unit.src.sqltide.executor.promotion.main.helpers import (
    rollback_active_state,
    rollback_catalog_relations,
    rollback_deployment_inventory,
    rollback_mismatched_inventory,
    rollback_tied_inventory,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RollbackResolutionSuccessTestCase(
            description="previous skips repeated active publications and selects prior bindings",
            request=RollbackRequest(
                deployment_id=None,
                previous=True,
                metadata_database="metadata",
                default_database="analytics",
            ),
            expected_plan=RollbackPlan(
                current_deployment_id="20260727T120000Z_active1",
                target_deployment_id="20260727T110000Z_middle1",
                logical_view_names=("orders",),
            ),
        ),
        RollbackResolutionSuccessTestCase(
            description="explicit target selects an older successfully published deployment",
            request=RollbackRequest(
                deployment_id="20260727T100000Z_old111",
                previous=False,
                metadata_database="metadata",
                default_database="analytics",
            ),
            expected_plan=RollbackPlan(
                current_deployment_id="20260727T120000Z_active1",
                target_deployment_id="20260727T100000Z_old111",
                logical_view_names=("orders",),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_publication_history_when_resolving_rollback_then_returns_expected_plan(
    test_case: RollbackResolutionSuccessTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=rollback_deployment_inventory(),
        managed_table_state=rollback_active_state(),
        relations=rollback_catalog_relations(),
    )

    result: RollbackPlan = resolve_deployment_rollback(
        request=test_case.request,
        client=connection,
    )

    assert result == test_case.expected_plan


@pytest.mark.parametrize(
    "test_case",
    [
        RollbackResolutionErrorTestCase(
            description="staged target has no successful publication",
            request=RollbackRequest(
                deployment_id="20260727T130000Z_staged1",
                previous=False,
                metadata_database="metadata",
                default_database="analytics",
            ),
            managed_table_state=rollback_active_state(),
            expected_error_fragment="has no successful publication",
        ),
        RollbackResolutionErrorTestCase(
            description="active target cannot be selected as rollback target",
            request=RollbackRequest(
                deployment_id="20260727T120000Z_active1",
                previous=False,
                metadata_database="metadata",
                default_database="analytics",
            ),
            managed_table_state=rollback_active_state(),
            expected_error_fragment="is already active",
        ),
        RollbackResolutionErrorTestCase(
            description="active target has no complete publication",
            request=RollbackRequest(
                deployment_id=None,
                previous=True,
                metadata_database="metadata",
                default_database="analytics",
            ),
            managed_table_state=InspectedManagedTableState(
                active_bindings=(),
                physical_candidates=(),
            ),
            expected_error_fragment="requires an active published deployment",
        ),
        RollbackResolutionErrorTestCase(
            description="published target with a missing relation is invalid",
            request=RollbackRequest(
                deployment_id="20260727T100000Z_old111",
                previous=False,
                metadata_database="metadata",
                default_database="analytics",
            ),
            managed_table_state=rollback_active_state(),
            expected_error_fragment="missing retained relations",
            relations=(rollback_catalog_relations()[0], *rollback_catalog_relations()[2:]),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_target_when_resolving_rollback_then_raises_clear_error(
    test_case: RollbackResolutionErrorTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=rollback_deployment_inventory(),
        managed_table_state=test_case.managed_table_state,
        relations=test_case.relations,
    )

    with pytest.raises(PublishExecutionError, match=test_case.expected_error_fragment):
        resolve_deployment_rollback(request=test_case.request, client=connection)


@pytest.mark.parametrize(
    "test_case",
    [
        RollbackInventoryErrorTestCase(
            description="publication graph differs from deployment metadata",
            request=RollbackRequest(
                deployment_id="20260727T100000Z_old111",
                previous=False,
                metadata_database="metadata",
                default_database="analytics",
            ),
            inventory=rollback_mismatched_inventory(),
            managed_table_state=rollback_active_state(),
            expected_error_fragment="publication bindings do not match",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_publication_graph_differs_when_rolling_back_then_rejects(
    test_case: RollbackInventoryErrorTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=test_case.inventory,
        managed_table_state=test_case.managed_table_state,
    )

    with pytest.raises(PublishExecutionError, match=test_case.expected_error_fragment):
        resolve_deployment_rollback(
            request=test_case.request,
            client=connection,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        RollbackPublicationOrderTestCase(
            description="equal timestamps use publication identity ordering",
            inventory=rollback_tied_inventory(),
            expected_target_deployment_id="20260727T110000Z_middle1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_equal_publication_times_when_rolling_back_previous_then_uses_publication_id(
    test_case: RollbackPublicationOrderTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=test_case.inventory,
        managed_table_state=rollback_active_state(),
        relations=rollback_catalog_relations(),
    )

    result: RollbackPlan = resolve_deployment_rollback(
        request=RollbackRequest(
            deployment_id=None,
            previous=True,
            metadata_database="metadata",
            default_database="analytics",
        ),
        client=connection,
    )

    assert result.target_deployment_id == test_case.expected_target_deployment_id


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
