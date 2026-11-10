"""Audit backfill execution entrypoint."""

from sqltide._native import (
    deployment_id_from_physical_name,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.compiler.compile.constants import DESIRED_OBJECT_TYPE_TABLE
from sqltide.compiler.compile.models import ObjectKey
from sqltide.executor.readiness._helpers.comparisons import build_root_audit_results
from sqltide.executor.readiness._helpers.resolution import resolve_audit_deployment_id
from sqltide.executor.readiness.main.load_audit_deployment import load_audit_deployment
from sqltide.executor.readiness.models import (
    DeploymentAuditRequest,
    DeploymentAuditResult,
    LoadedAuditDeployment,
    RootAuditResult,
)
from sqltide.executor.readiness.types import AuditAssessment


def execute_deployment_audit(
    *,
    request: DeploymentAuditRequest,
    client: AdapterConnection,
) -> DeploymentAuditResult:
    """Audit a staged backfill deployment by explicit deployment id."""

    resolved_deployment_id: str = resolve_audit_deployment_id(
        client=client,
        metadata_database=request.metadata_database,
        default_database=request.default_database,
        deployment_id=request.deployment_id,
    )
    loaded_deployment: LoadedAuditDeployment = load_audit_deployment(
        client=client,
        metadata_database=request.metadata_database,
        deployment_id=resolved_deployment_id,
    )
    inspected_state: InspectedManagedTableState = client.inspect_managed_table_state(
        request.default_database
    )
    root_keys: tuple[ObjectKey, ...] = loaded_deployment.root_keys
    prepared_object_mappings: tuple[tuple[ObjectKey, str], ...] = (
        loaded_deployment.prepared_object_mappings
    )
    if not prepared_object_mappings:
        prepared_object_mappings = _inspected_audit_mappings(
            inspected_state=inspected_state,
            deployment_id=resolved_deployment_id,
        )
        root_keys = tuple(mapping[0] for mapping in prepared_object_mappings)
    root_results: tuple[RootAuditResult, ...] = build_root_audit_results(
        client=client,
        default_database=request.default_database,
        inspected_state=inspected_state,
        root_keys=root_keys,
        prepared_object_mappings=prepared_object_mappings,
        thresholds=request.thresholds,
    )
    return DeploymentAuditResult(
        deployment_id=resolved_deployment_id,
        deployment_status=loaded_deployment.status,
        assessment=_build_overall_assessment(root_results),
        replay_lineage_mode=loaded_deployment.replay_lineage_mode,
        warning_codes=loaded_deployment.warning_codes,
        root_results=root_results,
        thresholds=request.thresholds,
    )


def _inspected_audit_mappings(
    *, inspected_state: InspectedManagedTableState, deployment_id: str
) -> tuple[tuple[ObjectKey, str], ...]:
    return tuple(
        (
            ObjectKey(
                database=candidate.database,
                object_type=DESIRED_OBJECT_TYPE_TABLE,
                name=candidate.logical_name,
            ),
            candidate.physical_name,
        )
        for candidate in inspected_state.physical_candidates
        if candidate.object_type == DESIRED_OBJECT_TYPE_TABLE
        and deployment_id_from_physical_name(candidate.physical_name) == deployment_id
    )


def _build_overall_assessment(root_results: tuple[RootAuditResult, ...]) -> AuditAssessment:
    if any(root_result.assessment == AuditAssessment.NOT_READY for root_result in root_results):
        return AuditAssessment(AuditAssessment.NOT_READY)
    if any(root_result.assessment == AuditAssessment.CAUTION for root_result in root_results):
        return AuditAssessment(AuditAssessment.CAUTION)
    return AuditAssessment(AuditAssessment.READY)
