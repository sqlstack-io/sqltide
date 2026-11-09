"""Execute workflow recovery statements after a failed or interrupted build."""

from dataclasses import replace

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.executor.workflow.constants import (
    RECOVER_DEAD_LETTERS_STEP_PREFIX,
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
    completed_step_ids: frozenset[str] | None,
    retry_command: str,
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
                "stays paused because a model view could not be restored, so no rows are lost. "
                f"Run `{retry_command}` once the cause is fixed to resume it"
            )
            continue
        if _new_view_may_be_live(
            step_id=recovery_statement.step_id, completed_step_ids=completed_step_ids
        ):
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


def _new_view_may_be_live(*, step_id: str, completed_step_ids: frozenset[str] | None) -> bool:
    """Return whether dropping this dead-letter view could orphan a live quarantine view."""

    if not step_id.startswith(RECOVER_DEAD_LETTERS_STEP_PREFIX):
        return False
    view_step: str = f"realize_{step_id.removeprefix(RECOVER_DEAD_LETTERS_STEP_PREFIX)}"
    return completed_step_ids is None or view_step in completed_step_ids
