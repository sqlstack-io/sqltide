import pytest

from sqltide._native import build_root_audit_results
from sqltide.adapter.models import (
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterReadinessRootRequest,
    AdapterReadinessScalarSummary,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.compile.types import DesiredObjectType
from sqltide.executor.readiness.models import (
    DeploymentReadinessThresholds,
    RootAuditResult,
)
from sqltide.executor.readiness.types import AuditAssessment
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.readiness._test_types import (
    ReadinessComparisonConstructionTestCase,
    ReadinessThresholdTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ReadinessComparisonConstructionTestCase(
            description="correlates reordered neutral observations with their requested roots",
            deployment_id="20260726T180000Z_ab12cd",
            inspected_state=InspectedManagedTableState(
                active_bindings=(
                    InspectedActiveTableBinding(
                        database="analytics",
                        logical_name="tbl__orders_enriched",
                        physical_name="tbl__orders_enriched__20260726T170000Z_prev01",
                    ),
                ),
                physical_candidates=(
                    InspectedPhysicalTableCandidate(
                        database="analytics",
                        logical_name="tbl__orders_enriched",
                        physical_name="tbl__orders_enriched__20260726T180000Z_ab12cd",
                    ),
                    InspectedPhysicalTableCandidate(
                        database="analytics",
                        logical_name="tbl__payments_enriched",
                        physical_name="tbl__payments_enriched__20260726T180000Z_ab12cd",
                    ),
                ),
            ),
            observations=(
                AdapterReadinessRootObservation(
                    root=AdapterReadinessRootRequest(
                        database="analytics",
                        logical_name="tbl__payments_enriched",
                        staged_relation_name="tbl__payments_enriched__20260726T180000Z_ab12cd",
                        active_exists=False,
                    ),
                    staged_exists=True,
                    active_row_count=None,
                    staged_row_count=10,
                    replay_source_name="raw__payments",
                    replay_source_row_count=10,
                    replay_boundary_mode=AdapterReplayBoundaryMode.TIMESTAMP,
                    offset_summary=None,
                    scalar_summary=None,
                ),
                AdapterReadinessRootObservation(
                    root=AdapterReadinessRootRequest(
                        database="analytics",
                        logical_name="tbl__orders_enriched",
                        staged_relation_name="tbl__orders_enriched__20260726T180000Z_ab12cd",
                        active_exists=True,
                    ),
                    staged_exists=True,
                    active_row_count=41,
                    staged_row_count=42,
                    replay_source_name="raw__orders",
                    replay_source_row_count=42,
                    replay_boundary_mode=AdapterReplayBoundaryMode.TIMESTAMP,
                    offset_summary=None,
                    scalar_summary=AdapterReadinessScalarSummary(
                        active_min_value="2026-07-26 17:00:00.000",
                        active_max_value="2026-07-26 17:59:59.000",
                        staged_min_value="2026-07-26 17:00:00.000",
                        staged_max_value="2026-07-26 18:00:00.000",
                        lag_seconds=-1.0,
                    ),
                ),
            ),
            expected_request=AdapterReadinessRequest(
                roots=(
                    AdapterReadinessRootRequest(
                        database="analytics",
                        logical_name="tbl__orders_enriched",
                        staged_relation_name="tbl__orders_enriched__20260726T180000Z_ab12cd",
                        active_exists=True,
                    ),
                    AdapterReadinessRootRequest(
                        database="analytics",
                        logical_name="tbl__payments_enriched",
                        staged_relation_name="tbl__payments_enriched__20260726T180000Z_ab12cd",
                        active_exists=False,
                    ),
                )
            ),
            expected_root_names=("tbl__orders_enriched", "tbl__payments_enriched"),
            expected_assessments=(AuditAssessment.READY, AuditAssessment.READY),
            expected_row_deltas=(1, None),
            expected_lag_seconds=-1.0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_staged_state_when_building_readiness_then_adapter_request_and_policy_are_neutral(
    test_case: ReadinessComparisonConstructionTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        readiness_observations=test_case.observations
    )

    results: tuple[RootAuditResult, ...] = build_root_audit_results(
        client=connection,
        default_database="analytics",
        inspected_state=test_case.inspected_state,
        root_keys=tuple(
            ObjectKey(
                database="analytics",
                object_type=DesiredObjectType.TABLE,
                name=root.logical_name,
            )
            for root in test_case.expected_request.roots
        ),
        prepared_object_mappings=tuple(
            (
                ObjectKey(
                    database="analytics",
                    object_type=DesiredObjectType.TABLE,
                    name=root.logical_name,
                ),
                root.staged_relation_name,
            )
            for root in test_case.expected_request.roots
        ),
        thresholds=DeploymentReadinessThresholds(),
    )

    assert connection.readiness_requests == [test_case.expected_request]
    assert tuple(result.root_key.name for result in results) == test_case.expected_root_names
    assert tuple(result.assessment for result in results) == test_case.expected_assessments
    assert tuple(result.row_delta for result in results) == test_case.expected_row_deltas
    assert results[0].scalar_catchup_summary is not None
    assert results[0].scalar_catchup_summary.lag_seconds == test_case.expected_lag_seconds


@pytest.mark.parametrize(
    "test_case",
    [
        ReadinessThresholdTestCase(
            description="custom maximum lag marks a caught-up row count not ready",
            maximum_lag_seconds=10.0,
            minimum_staged_row_ratio=0.5,
            active_row_count=100,
            staged_row_count=100,
            lag_seconds=20.0,
            expected_assessment=AuditAssessment.NOT_READY,
        ),
        ReadinessThresholdTestCase(
            description="custom row ratio marks a low staged count caution",
            maximum_lag_seconds=30.0,
            minimum_staged_row_ratio=0.9,
            active_row_count=100,
            staged_row_count=80,
            lag_seconds=5.0,
            expected_assessment=AuditAssessment.CAUTION,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_custom_thresholds_when_building_readiness_then_assessment_uses_them(
    test_case: ReadinessThresholdTestCase,
) -> None:
    root_key: ObjectKey = ObjectKey(
        database="analytics",
        object_type=DesiredObjectType.TABLE,
        name="tbl__orders_enriched",
    )
    root_request: AdapterReadinessRootRequest = AdapterReadinessRootRequest(
        database="analytics",
        logical_name=root_key.name,
        staged_relation_name="tbl__orders_enriched__20260726T180000Z_ab12cd",
        active_exists=True,
    )
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        readiness_observations=(
            AdapterReadinessRootObservation(
                root=root_request,
                staged_exists=True,
                active_row_count=test_case.active_row_count,
                staged_row_count=test_case.staged_row_count,
                replay_source_name="raw__orders",
                replay_source_row_count=test_case.staged_row_count,
                replay_boundary_mode=AdapterReplayBoundaryMode.TIMESTAMP,
                offset_summary=None,
                scalar_summary=AdapterReadinessScalarSummary(
                    active_min_value="2026-07-26 17:00:00.000",
                    active_max_value="2026-07-26 18:00:00.000",
                    staged_min_value="2026-07-26 17:00:00.000",
                    staged_max_value="2026-07-26 18:00:00.000",
                    lag_seconds=test_case.lag_seconds,
                ),
            ),
        )
    )

    results: tuple[RootAuditResult, ...] = build_root_audit_results(
        client=connection,
        default_database="analytics",
        inspected_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name=root_key.name,
                    physical_name="tbl__orders_enriched__20260726T170000Z_prev01",
                ),
            ),
            physical_candidates=(),
        ),
        root_keys=(root_key,),
        prepared_object_mappings=((root_key, root_request.staged_relation_name),),
        thresholds=DeploymentReadinessThresholds(
            maximum_lag_seconds=test_case.maximum_lag_seconds,
            minimum_staged_row_ratio=test_case.minimum_staged_row_ratio,
        ),
    )

    assert results[0].assessment == test_case.expected_assessment
