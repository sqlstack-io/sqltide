"""Publish execution entrypoint."""

from sqltide._native import build_publish_binding_request, resolve_publish_deployment_id
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import AdapterBindingReplacementRequest, AdapterMetadataState
from sqltide.executor.promotion._helpers.metadata import build_publish_metadata_state
from sqltide.executor.promotion._helpers.workflow import assemble_publish_workflow
from sqltide.executor.promotion.models import PublishedView, PublishRequest, PublishResult
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock
from sqltide.executor.workflow.models import WarehouseStatement
from sqltide.executor.workflow.types import WorkflowEventEmitter


def execute_publish(
    *,
    request: PublishRequest,
    client: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
) -> PublishResult:
    """Publish a staged deployment by creating or replacing stable logical views."""

    _validate_publish_capabilities(client)
    with target_mutation_lock(connection=client, database=request.default_database):
        resolved_deployment_id: str = resolve_publish_deployment_id(
            client=client,
            metadata_database=request.metadata_database,
            default_database=request.default_database,
            deployment_id=request.deployment_id,
        )
        binding_request: AdapterBindingReplacementRequest = build_publish_binding_request(
            client=client,
            metadata_database=request.metadata_database,
            default_database=request.default_database,
            deployment_id=resolved_deployment_id,
        )
        published_views: tuple[PublishedView, ...] = tuple(
            PublishedView(
                view_name=binding.logical_name,
                target_table_name=binding.physical_name,
            )
            for binding in binding_request.bindings
        )
        published_at: str = client.capture_warehouse_timestamp()
        metadata_state: AdapterMetadataState = build_publish_metadata_state(
            deployment_id=resolved_deployment_id,
            published_at=published_at,
            published_views=published_views,
            database=request.default_database,
            operation=request.operation,
            previous_deployment_id=request.previous_deployment_id,
        )
        statements: tuple[WarehouseStatement, ...] = assemble_publish_workflow(
            client=client,
            metadata_database=request.metadata_database,
            binding_request=binding_request,
            metadata_state=metadata_state,
        )
        _ = execute_warehouse_workflow(statements=statements, connection=client, emitter=emitter)
        return PublishResult(
            deployment_id=resolved_deployment_id,
            published_views=published_views,
            per_relation_atomic_replace=client.capabilities.per_relation_atomic_replace,
            graph_atomic_publish=client.capabilities.graph_atomic_publish,
            operation=request.operation,
            previous_deployment_id=request.previous_deployment_id,
        )


def _validate_publish_capabilities(client: AdapterConnection) -> None:
    if not client.capabilities.stable_logical_bindings:
        raise AdapterCapabilityError(
            f"Adapter '{client.adapter_identity.name}' does not support stable logical bindings"
        )
