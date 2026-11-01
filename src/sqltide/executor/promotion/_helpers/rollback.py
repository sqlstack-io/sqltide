"""Resolve rollback publications from authoritative metadata and live bindings."""

from datetime import UTC, datetime, timedelta

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterPublishEventRecord,
    AdapterStableBinding,
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.executor.promotion.constants import UNRANKED_PUBLICATION_ERROR
from sqltide.executor.promotion.exceptions import PublishExecutionError
from sqltide.executor.promotion.models import RollbackPlan, RollbackRequest

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: timedelta = timedelta(microseconds=1)


def resolve_rollback_plan(*, request: RollbackRequest, client: AdapterConnection) -> RollbackPlan:
    """Resolve the active publication and requested retained rollback target."""

    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(
        request.metadata_database
    )
    managed_state: InspectedManagedTableState = client.inspect_managed_table_state(
        request.default_database
    )
    current_event: AdapterPublishEventRecord
    target_event: AdapterPublishEventRecord
    current_event, target_event = _select_publish_events(
        events=inventory.publish_events,
        active_bindings=managed_state.active_bindings,
        deployment_id=None if request.previous else request.deployment_id or "",
    )
    deployment_by_id: dict[str, AdapterDeploymentRecord] = {
        deployment.deployment_id: deployment for deployment in inventory.deployments
    }
    target_deployment: AdapterDeploymentRecord | None = deployment_by_id.get(
        target_event.deployment_id
    )
    if target_deployment is None:
        raise PublishExecutionError(
            f"Rollback target deployment '{target_event.deployment_id}' is missing "
            "deployment metadata"
        )
    expected_bindings: tuple[AdapterStableBinding, ...] = tuple(
        AdapterStableBinding(
            database=mapping.logical_key.database or request.default_database,
            logical_name=mapping.logical_key.name,
            physical_name=mapping.physical_name,
        )
        for mapping in target_deployment.prepared_object_mappings
        if mapping.logical_key.object_type in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}
    )
    if _binding_identity(target_event.bindings) != _binding_identity(expected_bindings):
        raise PublishExecutionError(
            f"Rollback target deployment '{target_event.deployment_id}' publication bindings "
            "do not match its deployment metadata"
        )
    mapping_databases: frozenset[str] = frozenset(
        mapping.logical_key.database or request.default_database
        for mapping in target_deployment.prepared_object_mappings
    )
    catalogs: dict[str, CatalogSnapshot] = {}
    for database in sorted(mapping_databases):
        catalogs[database] = client.load_catalog(database)
    missing_relations: tuple[str, ...] = tuple(
        sorted(
            f"{mapping.logical_key.database or request.default_database}.{mapping.physical_name}"
            for mapping in target_deployment.prepared_object_mappings
            if mapping.physical_name
            not in catalogs[
                mapping.logical_key.database or request.default_database
            ].relation_names()
        )
    )
    if missing_relations:
        raise PublishExecutionError(
            f"Rollback target deployment '{target_event.deployment_id}' is missing retained "
            f"relations: {', '.join(missing_relations)}"
        )
    return RollbackPlan(
        current_deployment_id=current_event.deployment_id,
        target_deployment_id=target_event.deployment_id,
        logical_view_names=tuple(
            sorted(
                mapping.logical_key.name
                for mapping in target_deployment.prepared_object_mappings
                if mapping.logical_key.object_type
                in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}
            )
        ),
    )


def _select_publish_events(
    *,
    events: tuple[AdapterPublishEventRecord, ...],
    active_bindings: tuple[InspectedActiveTableBinding, ...],
    deployment_id: str | None,
) -> tuple[AdapterPublishEventRecord, AdapterPublishEventRecord]:
    try:
        current: int
        target: int
        current, target = _native.select_rollback_publications(
            [
                (event.deployment_id, _native_event_rank(event), _native_bindings(event.bindings))
                for event in events
            ],
            _native_bindings(active_bindings),
            deployment_id,
        )
    except ValueError as error:
        if error.args[0] == UNRANKED_PUBLICATION_ERROR:
            _event_rank(events[error.args[1]])
        raise PublishExecutionError(error.args[1]) from None
    return events[current], events[target]


def _native_event_rank(event: AdapterPublishEventRecord) -> tuple[int, str] | None:
    try:
        published_at: datetime
        rank_id: str
        published_at, rank_id = _event_rank(event)
    except ValueError:
        return None
    return (published_at - _EPOCH) // _MICROSECOND, rank_id


def _native_bindings(
    bindings: tuple[AdapterStableBinding, ...] | tuple[InspectedActiveTableBinding, ...],
) -> list[tuple[str, str, str]]:
    return [(binding.database, binding.logical_name, binding.physical_name) for binding in bindings]


def _binding_identity(
    bindings: tuple[AdapterStableBinding, ...],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (binding.database, binding.logical_name, binding.physical_name) for binding in bindings
        )
    )


def _event_rank(event: AdapterPublishEventRecord) -> tuple[datetime, str]:
    published_at: datetime = datetime.fromisoformat(event.published_at.replace(" ", "T"))
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return published_at, event.publication_id or event.deployment_id
