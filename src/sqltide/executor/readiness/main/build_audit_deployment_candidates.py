"""Build the selectable audit deployment candidates for a target."""

from sqltide._native import (
    deployment_id_from_physical_name,
    is_deployment_physical_name,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    InspectedManagedTableState,
)
from sqltide.executor.readiness.models import AuditDeploymentCandidate


def build_audit_deployment_candidates(
    *,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
) -> tuple[AuditDeploymentCandidate, ...]:
    """Build candidate staged deployments visible to audit default resolution."""

    inspected_state: InspectedManagedTableState = client.inspect_managed_table_state(
        default_database
    )
    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
    existing_names: frozenset[str] = client.load_catalog(default_database).relation_names()
    persisted_deployment_ids: set[str] = {
        deployment.deployment_id
        for deployment in inventory.deployments
        if _deployment_has_existing_mapping(deployment=deployment, existing_names=existing_names)
    }
    inspected_deployment_ids: set[str] = {
        deployment_id_from_physical_name(candidate.physical_name)
        for candidate in inspected_state.physical_candidates
        if is_deployment_physical_name(candidate.physical_name)
    }
    all_deployment_ids: tuple[str, ...] = tuple(
        sorted(persisted_deployment_ids | inspected_deployment_ids)
    )
    active_deployment_ids: set[str] = {
        deployment_id_from_physical_name(binding.physical_name)
        for binding in inspected_state.active_bindings
        if is_deployment_physical_name(binding.physical_name)
    }
    if not active_deployment_ids:
        return tuple(AuditDeploymentCandidate(deployment_id=value) for value in all_deployment_ids)
    if len(active_deployment_ids) != 1:
        return tuple(AuditDeploymentCandidate(deployment_id=value) for value in all_deployment_ids)

    active_deployment_id: str = next(iter(active_deployment_ids))
    return tuple(
        AuditDeploymentCandidate(deployment_id=value)
        for value in all_deployment_ids
        if value > active_deployment_id
    )


def _deployment_has_existing_mapping(
    *, deployment: AdapterDeploymentRecord, existing_names: frozenset[str]
) -> bool:
    return any(
        mapping.physical_name in existing_names for mapping in deployment.prepared_object_mappings
    )
