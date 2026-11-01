from datetime import UTC, datetime, timedelta

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE
from sqltide.adapter.exceptions import AdapterResultError
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterRelationCleanupRequest,
    AdapterStableBinding,
    AdapterStableBindingRemoval,
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.executor.janitor._helpers.workflow import assemble_janitor_workflow
from sqltide.executor.janitor.models import (
    JanitorApplyResult,
    JanitorPreviewCandidate,
    JanitorPreviewResult,
    JanitorRequest,
)
from sqltide.executor.janitor.types import (
    NativeRetainedDeployment,
    NativeRetainedMapping,
    NativeRetainedPublication,
)
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: timedelta = timedelta(microseconds=1)


def execute_janitor_for_managed_table_state(
    *,
    request: JanitorRequest,
    client: AdapterConnection,
    managed_table_state: InspectedManagedTableState,
) -> JanitorPreviewResult | JanitorApplyResult:
    if request.apply:
        return _apply_janitor(
            client=client,
            database=request.database,
            metadata_database=request.metadata_database,
            retention_days=request.retention_days,
            minimum_rollback_deployments=request.minimum_rollback_deployments,
            managed_table_state=managed_table_state,
        )
    return _preview_janitor(
        client=client,
        database=request.database,
        metadata_database=request.metadata_database,
        retention_days=request.retention_days,
        minimum_rollback_deployments=request.minimum_rollback_deployments,
        managed_table_state=managed_table_state,
    )


def _preview_janitor(
    *,
    client: AdapterConnection,
    database: str,
    metadata_database: str,
    retention_days: int,
    minimum_rollback_deployments: int,
    managed_table_state: InspectedManagedTableState,
) -> JanitorPreviewResult:
    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
    publications: list[NativeRetainedPublication] = _native_publications(inventory.publish_events)
    physical_relations: set[tuple[str, str]] = (
        _physical_relations(client=client, inventory=inventory, database=database)
        if minimum_rollback_deployments
        else set()
    )
    retention_cutoff: datetime = datetime.now(tz=UTC) - timedelta(days=retention_days)
    decisions: list[tuple[int, bool, str]] = _native.janitor_preview(
        _native_deployments(inventory.deployments),
        publications,
        _native_bindings(managed_table_state.active_bindings),
        physical_relations,
        (database, retention_days, _micros(retention_cutoff), minimum_rollback_deployments),
    )
    return JanitorPreviewResult(
        database=database,
        retention_days=retention_days,
        minimum_rollback_deployments=minimum_rollback_deployments,
        candidates=tuple(
            _preview_candidate(
                deployment=inventory.deployments[position], deletable=deletable, reason=reason
            )
            for position, deletable, reason in decisions
        ),
    )


def _preview_candidate(
    *, deployment: AdapterDeploymentRecord, deletable: bool, reason: str
) -> JanitorPreviewCandidate:
    return JanitorPreviewCandidate(
        deployment_id=deployment.deployment_id,
        created_at=deployment.created_at,
        status=deployment.status,
        logical_view_names=tuple(
            sorted(mapping.logical_key.name for mapping in _publish_mappings(deployment))
        ),
        physical_object_names=tuple(
            sorted(mapping.physical_name for mapping in deployment.prepared_object_mappings)
        ),
        deletable=deletable,
        reason=reason,
    )


def _apply_janitor(
    *,
    client: AdapterConnection,
    database: str,
    metadata_database: str,
    retention_days: int,
    minimum_rollback_deployments: int,
    managed_table_state: InspectedManagedTableState,
) -> JanitorApplyResult:
    preview_result: JanitorPreviewResult = _preview_janitor(
        client=client,
        database=database,
        metadata_database=metadata_database,
        retention_days=retention_days,
        minimum_rollback_deployments=minimum_rollback_deployments,
        managed_table_state=managed_table_state,
    )
    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
    removals: list[tuple[str, str]] = _native.janitor_binding_activity(
        _native_deployments(inventory.deployments),
        _native_publications(inventory.publish_events),
        _native_bindings(managed_table_state.active_bindings),
    )[1]
    obsolete_removals: tuple[AdapterStableBindingRemoval, ...] = tuple(
        AdapterStableBindingRemoval(database=removal_database, logical_name=logical_name)
        for removal_database, logical_name in removals
    )
    binding_request: AdapterBindingReplacementRequest = AdapterBindingReplacementRequest(
        bindings=(), removals=obsolete_removals
    )
    deleted_deployment_ids: list[str] = []
    requested_object_names: list[str] = []
    candidate: JanitorPreviewCandidate
    for candidate in preview_result.candidates:
        if not candidate.deletable:
            continue
        object_name: str
        for object_name in candidate.physical_object_names:
            requested_object_names.append(object_name)
        deleted_deployment_ids.append(candidate.deployment_id)

    cleanup_request: AdapterRelationCleanupRequest = AdapterRelationCleanupRequest(
        database=database,
        relation_names=tuple(requested_object_names),
    )
    refreshed_state: InspectedManagedTableState = client.inspect_managed_table_state(database)
    refreshed_active_names: frozenset[str] = frozenset(
        binding.physical_name
        for binding in refreshed_state.active_bindings
        if AdapterStableBindingRemoval(
            database=binding.database,
            logical_name=binding.logical_name,
        )
        not in obsolete_removals
    )
    newly_active_names: frozenset[str] = refreshed_active_names.intersection(
        cleanup_request.relation_names
    )
    if newly_active_names:
        raise AdapterResultError(
            f"Refusing to clean relations that became active: {tuple(sorted(newly_active_names))!r}"
        )
    statements: tuple[WarehouseStatement, ...] = assemble_janitor_workflow(
        client=client,
        binding_request=binding_request,
        cleanup_request=cleanup_request,
    )
    _ = execute_warehouse_workflow(statements=statements, connection=client)

    return JanitorApplyResult(
        database=database,
        retention_days=retention_days,
        minimum_rollback_deployments=minimum_rollback_deployments,
        deleted_deployment_ids=tuple(deleted_deployment_ids),
        deleted_object_names=cleanup_request.relation_names,
    )


def _physical_relations(
    *, client: AdapterConnection, inventory: AdapterDeploymentInventory, database: str
) -> set[tuple[str, str]]:
    mapping_databases: set[str] = set()
    for deployment in inventory.deployments:
        for mapping in deployment.prepared_object_mappings:
            mapping_databases.add(mapping.logical_key.database or database)
    physical_relations: set[tuple[str, str]] = set()
    for mapping_database in sorted(mapping_databases):
        catalog: CatalogSnapshot = client.load_catalog(mapping_database)
        physical_relations.update(
            (mapping_database, relation_name) for relation_name in catalog.relation_names()
        )
    return physical_relations


def _publish_mappings(
    deployment: AdapterDeploymentRecord,
) -> tuple[AdapterPreparedObjectMapping, ...]:
    return tuple(
        mapping
        for mapping in deployment.prepared_object_mappings
        if mapping.logical_key.object_type in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW}
    )


def _native_deployments(
    deployments: tuple[AdapterDeploymentRecord, ...],
) -> list[NativeRetainedDeployment]:
    return [
        (
            deployment.deployment_id,
            deployment.created_at,
            deployment.status == VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE,
            _native_mappings(deployment.prepared_object_mappings),
        )
        for deployment in deployments
    ]


def _native_mappings(
    mappings: tuple[AdapterPreparedObjectMapping, ...],
) -> list[NativeRetainedMapping]:
    return [
        (
            mapping.logical_key.database,
            mapping.logical_key.object_type
            in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW},
            mapping.logical_key.name,
            mapping.physical_name,
            mapping.logical_model_name,
        )
        for mapping in mappings
    ]


def _native_publications(
    events: tuple[AdapterPublishEventRecord, ...],
) -> list[NativeRetainedPublication]:
    return [_native_publication(event) for event in events]


def _native_publication(event: AdapterPublishEventRecord) -> NativeRetainedPublication:
    parsed: datetime = datetime.fromisoformat(event.published_at.replace(" ", "T"))
    ranked: datetime = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return (
        event.deployment_id,
        (_micros(ranked), event.publication_id or event.deployment_id),
        _micros(parsed.replace(tzinfo=UTC)),
        list(event.logical_view_names),
        _native_bindings(event.bindings),
    )


def _native_bindings(
    bindings: tuple[AdapterStableBinding, ...] | tuple[InspectedActiveTableBinding, ...],
) -> list[tuple[str, str, str]]:
    return [(binding.database, binding.logical_name, binding.physical_name) for binding in bindings]


def _micros(moment: datetime) -> int:
    return (moment - _EPOCH) // _MICROSECOND
