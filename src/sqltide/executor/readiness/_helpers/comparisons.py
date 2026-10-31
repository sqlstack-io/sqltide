"""Build audit decisions from adapter readiness observations."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterResultError
from sqltide.adapter.models import (
    AdapterReadinessOffsetSummary,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterReadinessRootRequest,
    AdapterReadinessScalarSummary,
    InspectedManagedTableState,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.compiler.compile.constants import DESIRED_OBJECT_TYPE_TABLE
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.types import RootDeploymentStateKind
from sqltide.executor.readiness.models import (
    DeploymentReadinessThresholds,
    OffsetCatchupSummary,
    RootAuditResult,
    ScalarCatchupSummary,
)
from sqltide.executor.readiness.types import AuditAssessment


def build_root_audit_results(
    *,
    client: AdapterConnection,
    default_database: str,
    inspected_state: InspectedManagedTableState,
    root_keys: tuple[ObjectKey, ...],
    prepared_object_mappings: tuple[tuple[ObjectKey, str], ...],
    thresholds: DeploymentReadinessThresholds,
) -> tuple[RootAuditResult, ...]:
    """Build audit decisions for each adapter-observed staged root."""

    physical_name_by_key: dict[ObjectKey, str] = dict(prepared_object_mappings)
    staged_name_by_root: dict[ObjectKey, str] = {
        root_key: physical_name_by_key[root_key]
        for root_key in root_keys
        if root_key.object_type == DESIRED_OBJECT_TYPE_TABLE
        if root_key in physical_name_by_key
    }
    ordered_root_keys: tuple[ObjectKey, ...] = tuple(
        sorted(
            staged_name_by_root,
            key=lambda value: (value.database or "", value.object_type, value.name),
        )
    )
    root_requests: tuple[AdapterReadinessRootRequest, ...] = tuple(
        AdapterReadinessRootRequest(
            database=root_key.database or default_database,
            logical_name=root_key.name,
            staged_relation_name=staged_name_by_root[root_key],
            active_exists=_has_active_binding(
                inspected_state=inspected_state,
                root_key=root_key,
            ),
        )
        for root_key in ordered_root_keys
    )
    observations: tuple[AdapterReadinessRootObservation, ...] = client.compare_readiness(
        AdapterReadinessRequest(roots=root_requests)
    )
    observation_by_root: dict[AdapterReadinessRootRequest, AdapterReadinessRootObservation] = (
        _index_observations(root_requests=root_requests, observations=observations)
    )
    return tuple(
        _build_root_audit_result(
            root_key=root_key,
            root_request=root_request,
            observation=observation_by_root[root_request],
            thresholds=thresholds,
        )
        for root_key, root_request in zip(
            ordered_root_keys,
            root_requests,
            strict=True,
        )
    )


def _index_observations(
    *,
    root_requests: tuple[AdapterReadinessRootRequest, ...],
    observations: tuple[AdapterReadinessRootObservation, ...],
) -> dict[AdapterReadinessRootRequest, AdapterReadinessRootObservation]:
    observation_by_root: dict[AdapterReadinessRootRequest, AdapterReadinessRootObservation] = {
        observation.root: observation for observation in observations
    }
    if len(observation_by_root) != len(observations):
        raise AdapterResultError("Adapter readiness returned duplicate root observations")
    if set(observation_by_root) != set(root_requests):
        raise AdapterResultError("Adapter readiness observations did not match requested roots")
    return observation_by_root


def _build_root_audit_result(
    *,
    root_key: ObjectKey,
    root_request: AdapterReadinessRootRequest,
    observation: AdapterReadinessRootObservation,
    thresholds: DeploymentReadinessThresholds,
) -> RootAuditResult:
    replay_lineage_mode: ReplayLineageMode | None = _replay_lineage_mode(
        observation.replay_boundary_mode
    )
    offset_summary: OffsetCatchupSummary | None = _offset_summary(observation.offset_summary)
    scalar_summary: ScalarCatchupSummary | None = _scalar_summary(observation.scalar_summary)
    assessment: str
    row_delta: int | None
    row_ratio: float | None
    warnings: list[str]
    assessment, row_delta, row_ratio, warnings = _native.audit_readiness(
        (
            root_key.name,
            observation.staged_exists,
            root_request.active_exists,
            observation.active_row_count,
            observation.staged_row_count,
            replay_lineage_mode == ReplayLineageMode.OFFSETS,
            None
            if offset_summary is None
            else (
                offset_summary.active_partition_count,
                offset_summary.partitions_compared,
                offset_summary.missing_staged_partition_count,
                offset_summary.missing_freshness_partition_count,
                offset_summary.lag_boundary_column is not None,
                offset_summary.max_lag_seconds,
            ),
            None if scalar_summary is None else scalar_summary.lag_seconds,
            observation.replay_source_name,
            observation.replay_source_row_count,
        ),
        (thresholds.maximum_lag_seconds, thresholds.minimum_staged_row_ratio),
    )
    return RootAuditResult(
        root_key=root_key,
        staged_physical_name=root_request.staged_relation_name,
        state=(
            RootDeploymentStateKind.ACTIVE_VIEW_PRESENT
            if root_request.active_exists
            else RootDeploymentStateKind.GREENFIELD
        ),
        replay_source_name=observation.replay_source_name,
        replay_source_row_count=observation.replay_source_row_count,
        staged_exists=observation.staged_exists,
        active_exists=root_request.active_exists,
        active_row_count=observation.active_row_count,
        staged_row_count=observation.staged_row_count,
        row_delta=row_delta,
        row_ratio=row_ratio,
        assessment=AuditAssessment(assessment),
        replay_lineage_mode=replay_lineage_mode,
        offset_catchup_summary=offset_summary,
        scalar_catchup_summary=scalar_summary,
        warnings=tuple(warnings),
    )


def _has_active_binding(
    *, inspected_state: InspectedManagedTableState, root_key: ObjectKey
) -> bool:
    return any(
        binding.database == (root_key.database or binding.database)
        and binding.logical_name == root_key.name
        for binding in inspected_state.active_bindings
    )


def _replay_lineage_mode(
    boundary_mode: AdapterReplayBoundaryMode | None,
) -> ReplayLineageMode | None:
    return None if boundary_mode is None else ReplayLineageMode(boundary_mode)


def _offset_summary(
    summary: AdapterReadinessOffsetSummary | None,
) -> OffsetCatchupSummary | None:
    if summary is None:
        return None
    return OffsetCatchupSummary(
        active_partition_count=summary.active_partition_count,
        staged_partition_count=summary.staged_partition_count,
        partitions_compared=summary.partitions_compared,
        missing_staged_partition_count=summary.missing_staged_partition_count,
        missing_freshness_partition_count=summary.missing_freshness_partition_count,
        lagging_partition_count=summary.lagging_partition_count,
        max_offset_gap=summary.max_offset_gap,
        average_offset_gap=summary.average_offset_gap,
        lag_boundary_column=summary.lag_boundary_column,
        max_lag_seconds=summary.max_lag_seconds,
        average_lag_seconds=summary.average_lag_seconds,
    )


def _scalar_summary(
    summary: AdapterReadinessScalarSummary | None,
) -> ScalarCatchupSummary | None:
    if summary is None:
        return None
    return ScalarCatchupSummary(
        active_min_value=summary.active_min_value,
        active_max_value=summary.active_max_value,
        staged_min_value=summary.staged_min_value,
        staged_max_value=summary.staged_max_value,
        lag_seconds=summary.lag_seconds,
    )
