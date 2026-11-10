"""Repair execution entrypoint."""

from sqltide._native import (
    build_deployment_physical_name,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import AdapterStableBinding
from sqltide.executor.repair._helpers.workflow import assemble_repair_workflow
from sqltide.executor.repair.models import RepairActiveViewRequest, RepairActiveViewResult
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock
from sqltide.executor.workflow.models import WarehouseStatement


def execute_repair_active_view(
    *,
    request: RepairActiveViewRequest,
    client: AdapterConnection,
) -> RepairActiveViewResult:
    """Explicitly rebind a stable active view to a chosen deployment table."""

    target_table_name: str = build_deployment_physical_name(
        logical_name=request.table_name, deployment_id=request.deployment_id
    )
    if not client.capabilities.stable_logical_bindings:
        raise AdapterCapabilityError(
            f"Adapter '{client.adapter_identity.name}' does not support stable logical bindings"
        )
    with target_mutation_lock(connection=client, database=request.default_database):
        binding: AdapterStableBinding = AdapterStableBinding(
            database=request.default_database,
            logical_name=request.table_name,
            physical_name=target_table_name,
        )
        statements: tuple[WarehouseStatement, ...] = assemble_repair_workflow(
            binding=binding,
            client=client,
        )
        _ = execute_warehouse_workflow(statements=statements, connection=client)
        return RepairActiveViewResult(
            table_name=request.table_name,
            target_table_name=target_table_name,
        )
