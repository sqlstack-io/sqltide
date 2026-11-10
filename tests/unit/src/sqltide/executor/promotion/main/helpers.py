from dataclasses import replace

from sqltide._native import (
    render_clickhouse_stable_binding_replacement,
)
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterMetadataObjectKey,
    AdapterMetadataState,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterStableBinding,
    CatalogRelation,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse._helpers.metadata import render_clickhouse_metadata_state
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


class PublishWorkflowAdapterConnection(RecordingAdapterConnection):
    def __init__(
        self,
        *,
        managed_table_state: InspectedManagedTableState,
        deployment_inventory: AdapterDeploymentInventory,
        relations: tuple[CatalogRelation, ...],
    ) -> None:
        super().__init__(
            managed_table_state=managed_table_state,
            deployment_inventory=deployment_inventory,
            relations=relations,
        )

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        return (f"CREATE DATABASE IF NOT EXISTS {database};",)

    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]:
        return render_clickhouse_metadata_state(database=database, state=state)

    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]:
        return render_clickhouse_stable_binding_replacement(
            connection=self,
            request=request,
        )


def rollback_deployment_inventory() -> AdapterDeploymentInventory:
    """Return three complete publications plus one unpublished deployment."""

    deployment_ids: tuple[str, ...] = (
        "20260727T100000Z_old111",
        "20260727T110000Z_middle1",
        "20260727T120000Z_active1",
        "20260727T130000Z_staged1",
    )
    deployments: tuple[AdapterDeploymentRecord, ...] = (
        _rollback_deployment(deployment_id=deployment_ids[0], status="published"),
        _rollback_deployment(deployment_id=deployment_ids[1], status="published"),
        _rollback_deployment(deployment_id=deployment_ids[2], status="published"),
        _rollback_deployment(deployment_id=deployment_ids[3], status="staged"),
    )
    old_event: AdapterPublishEventRecord = _rollback_publish_event(
        deployment_id=deployment_ids[0], published_at="2026-07-27 10:05:00.000"
    )
    middle_event: AdapterPublishEventRecord = _rollback_publish_event(
        deployment_id=deployment_ids[1], published_at="2026-07-27 11:05:00.000"
    )
    active_event: AdapterPublishEventRecord = _rollback_publish_event(
        deployment_id=deployment_ids[2], published_at="2026-07-27 12:05:00.000"
    )
    repeated_active_event: AdapterPublishEventRecord = _rollback_publish_event(
        deployment_id=deployment_ids[2], published_at="2026-07-27 12:10:00.000"
    )
    return AdapterDeploymentInventory(
        deployments=deployments,
        publish_events=(old_event, middle_event, active_event, repeated_active_event),
    )


def rollback_active_state() -> InspectedManagedTableState:
    """Return live bindings matching the newest publication."""

    return InspectedManagedTableState(
        active_bindings=(
            InspectedActiveTableBinding(
                database="analytics",
                logical_name="orders",
                physical_name="orders__20260727T120000Z_active1",
            ),
        ),
        physical_candidates=(),
    )


def rollback_catalog_relations() -> tuple[CatalogRelation, ...]:
    relations: list[CatalogRelation] = []
    for deployment_id in (
        "20260727T100000Z_old111",
        "20260727T110000Z_middle1",
        "20260727T120000Z_active1",
        "20260727T130000Z_staged1",
    ):
        for name in ("orders", "mv__orders"):
            relations.append(
                CatalogRelation(name=f"{name}__{deployment_id}", engine="MergeTree", columns=())
            )
    return tuple(relations)


def rollback_mismatched_inventory() -> AdapterDeploymentInventory:
    inventory: AdapterDeploymentInventory = rollback_deployment_inventory()
    old_event: AdapterPublishEventRecord = inventory.publish_events[0]
    mismatched_event: AdapterPublishEventRecord = replace(
        old_event,
        bindings=(
            *old_event.bindings,
            AdapterStableBinding(
                database="analytics",
                logical_name="unexpected",
                physical_name="unexpected__20260727T100000Z_old111",
            ),
        ),
    )
    return replace(
        inventory,
        publish_events=(mismatched_event, *inventory.publish_events[1:]),
    )


def rollback_tied_inventory() -> AdapterDeploymentInventory:
    inventory: AdapterDeploymentInventory = rollback_deployment_inventory()
    tied_events: tuple[AdapterPublishEventRecord, ...] = tuple(
        replace(
            event,
            published_at="2026-07-27 12:10:00.000",
            publication_id=f"{index:020d}",
        )
        for index, event in enumerate(inventory.publish_events, start=1)
    )
    return replace(inventory, publish_events=tied_events)


def _rollback_deployment(*, deployment_id: str, status: str) -> AdapterDeploymentRecord:
    return AdapterDeploymentRecord(
        deployment_id=deployment_id,
        created_at="2026-07-27 10:00:00.000",
        status=status,
        replay_lineage_mode="offsets",
        selected_root_keys=(),
        warning_codes=(),
        prepared_object_mappings=(
            AdapterPreparedObjectMapping(
                logical_key=AdapterMetadataObjectKey(
                    database=None,
                    object_type="table",
                    name="orders",
                ),
                physical_name=f"orders__{deployment_id}",
                logical_model_name="orders",
            ),
            AdapterPreparedObjectMapping(
                logical_key=AdapterMetadataObjectKey(
                    database=None,
                    object_type="materialized_view",
                    name="mv__orders",
                ),
                physical_name=f"mv__orders__{deployment_id}",
                logical_model_name="orders",
            ),
        ),
    )


def _rollback_publish_event(*, deployment_id: str, published_at: str) -> AdapterPublishEventRecord:
    binding: AdapterStableBinding = AdapterStableBinding(
        database="analytics",
        logical_name="orders",
        physical_name=f"orders__{deployment_id}",
    )
    return AdapterPublishEventRecord(
        deployment_id=deployment_id,
        published_at=published_at,
        logical_view_names=("orders",),
        bindings=(binding,),
    )
