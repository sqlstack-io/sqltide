"""Authoritative deployment inventory reconstruction."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.executor.deployment.models import DeploymentInventory, DeploymentSummary
from sqltide.executor.deployment.types import (
    DeploymentLifecycleState,
    NativeDeploymentSummary,
    NativeInventoryRecord,
)


def build_deployment_inventory(
    *, client: AdapterConnection, metadata_database: str, default_database: str
) -> DeploymentInventory:
    """Combine append-only lifecycle records with current warehouse evidence."""
    persisted: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
    inspected: InspectedManagedTableState = client.inspect_managed_table_state(default_database)
    catalog: CatalogSnapshot = client.load_catalog(default_database)
    rows: list[NativeDeploymentSummary] = _native.deployment_inventory_summaries(
        [_record_argument(record) for record in persisted.deployments],
        [candidate.physical_name for candidate in inspected.physical_candidates],
        [(binding.logical_name, binding.physical_name) for binding in inspected.active_bindings],
        [(event.deployment_id, event.published_at) for event in persisted.publish_events],
        sorted(catalog.relation_names()),
    )
    return DeploymentInventory(
        database=default_database,
        deployments=tuple(_summary(row) for row in rows),
    )


def _record_argument(record: AdapterDeploymentRecord) -> NativeInventoryRecord:
    return (
        record.deployment_id,
        record.created_at,
        record.status,
        [key.name for key in record.selected_root_keys],
        [
            (mapping.logical_key.name, mapping.physical_name)
            for mapping in record.prepared_object_mappings
        ],
    )


def _summary(row: NativeDeploymentSummary) -> DeploymentSummary:
    (
        deployment_id,
        state,
        created_at,
        persisted_status,
        root_names,
        physical_relation_names,
        missing_physical_relation_names,
        active_binding_names,
        latest_published_at,
    ) = row
    return DeploymentSummary(
        deployment_id=deployment_id,
        state=DeploymentLifecycleState(state),
        created_at=created_at,
        persisted_status=persisted_status,
        root_names=tuple(root_names),
        physical_relation_names=tuple(physical_relation_names),
        missing_physical_relation_names=tuple(missing_physical_relation_names),
        active_binding_names=tuple(active_binding_names),
        latest_published_at=latest_published_at,
    )
