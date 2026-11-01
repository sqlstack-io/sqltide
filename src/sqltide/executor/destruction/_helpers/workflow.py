"""Dependency-safe destruction workflow assembly."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.executor.destruction._helpers.ordering import (
    reverse_topologically_order_relations,
)
from sqltide.executor.destruction.models import DestructionPlan
from sqltide.executor.destruction.types import DestructionRelationKind
from sqltide.executor.workflow.models import WarehouseStatement
from sqltide.executor.workflow.types import StatementIntent, WorkflowPhase


def assemble_destruction_workflow(
    *, plan: DestructionPlan, connection: AdapterConnection | None = None
) -> tuple[WarehouseStatement, ...]:
    """Drop frozen owned relations in stable reverse-dependency order."""

    drops: list[tuple[str, str, str]] = _native.destruction_drop_statements(
        [
            (str(DestructionRelationKind(relation.kind)), relation.database, relation.name)
            for relation in reverse_topologically_order_relations(plan.relations)
            if relation.exists
        ],
        plan.relation_drop_size_override,
    )
    return tuple(
        WarehouseStatement(
            sequence=sequence,
            step_id=step_id,
            phase=WorkflowPhase.TEARDOWN,
            intent=StatementIntent.MUTATION,
            display_name=display_name,
            sql=sql,
        )
        for sequence, (step_id, display_name, sql) in enumerate(drops, start=1)
    )
