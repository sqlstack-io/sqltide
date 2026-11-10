from __future__ import annotations

from sqltide._native import load_audit_deployment
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.executor.readiness.models import (
    AuditDeploymentCandidate,
    LoadedAuditDeployment,
)


def candidate_root_names(inspected_state: InspectedManagedTableState) -> tuple[str, ...]:
    active_root_names: tuple[str, ...] = tuple(
        sorted({binding.logical_name for binding in inspected_state.active_bindings})
    )
    if active_root_names:
        return active_root_names

    return tuple(
        sorted({candidate.logical_name for candidate in inspected_state.physical_candidates})
    )


def enrich_candidates(
    *,
    client: AdapterConnection,
    metadata_database: str,
    candidates: tuple[AuditDeploymentCandidate, ...],
) -> tuple[AuditDeploymentCandidate, ...]:
    enriched_candidates: list[AuditDeploymentCandidate] = []
    candidate: AuditDeploymentCandidate
    for candidate in candidates:
        loaded: LoadedAuditDeployment = load_audit_deployment(
            client=client,
            metadata_database=metadata_database,
            deployment_id=candidate.deployment_id,
        )
        root_names: tuple[str, ...] = loaded_deployment_root_names(loaded)
        enriched_candidates.append(
            AuditDeploymentCandidate(
                deployment_id=candidate.deployment_id,
                created_at=loaded.created_at,
                deployment_status=loaded.status,
                root_names=root_names,
            )
        )

    return tuple(enriched_candidates)


def loaded_deployment_root_names(loaded: LoadedAuditDeployment) -> tuple[str, ...]:
    root_names: tuple[str, ...] = tuple(
        sorted(
            root_key.name
            for root_key in loaded.root_keys
            if root_key.object_type in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}
        )
    )
    if root_names:
        return root_names

    return tuple(
        sorted(
            logical_key.name
            for logical_key, _physical_name in loaded.prepared_object_mappings
            if logical_key.object_type in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}
        )
    )
