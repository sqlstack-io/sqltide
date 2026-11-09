"""Execute workflow recovery statements after a failed or interrupted build."""

from dataclasses import replace

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.executor.workflow.constants import (
    RECOVER_MODEL_VIEW_STEP_PREFIX,
    RECOVER_SOURCE_STEP_PREFIX,
)
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement
from sqltide.executor.workflow.types import WorkflowEventEmitter


def execute_recovery_statements(
    *,
    statements: tuple[WarehouseStatement, ...],
    connection: AdapterConnection,
    emitter: WorkflowEventEmitter | None,
    cause: BaseException,
    start_sequence: int,
) -> tuple[WarehouseStatement, ...]:
    """Run recovery in order, keeping sources paused when a model view could not be restored."""

    recovered: list[WarehouseStatement] = []
    view_restore_failed: bool = False
    recovery_statement: WarehouseStatement
    for recovery_statement in statements:
        if view_restore_failed and recovery_statement.step_id.startswith(
            RECOVER_SOURCE_STEP_PREFIX
        ):
            cause.add_note(
                f"Managed source recovery '{recovery_statement.step_id}' was skipped: the source "
                "stays paused because a model view could not be restored, so no rows are lost"
            )
            continue
        statement: WarehouseStatement = replace(
            recovery_statement,
            sequence=start_sequence + len(recovered),
        )
        try:
            _ = execute_warehouse_workflow(
                statements=(statement,),
                connection=connection,
                emitter=emitter,
            )
        except (AdapterError, KeyboardInterrupt, WorkflowExecutionError) as recovery_error:
            recovery_cause: BaseException = (
                recovery_error.cause
                if isinstance(recovery_error, WorkflowExecutionError)
                else recovery_error
            )
            cause.add_note(
                f"Managed source recovery '{statement.step_id}' also failed: {recovery_cause}"
            )
            view_restore_failed = view_restore_failed or statement.step_id.startswith(
                RECOVER_MODEL_VIEW_STEP_PREFIX
            )
        recovered.append(statement)
    return tuple(recovered)
