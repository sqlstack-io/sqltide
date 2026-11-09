"""Recover the live model views replaced by in-place row-error policy changes."""

from collections.abc import Callable

import sqltide._native as _native
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from sqltide.executor.workflow.constants import RECOVER_MODEL_VIEW_STEP_PREFIX
from sqltide.executor.workflow.types import NativeStatement, StatementIntent, WorkflowPhase


def row_error_policy_restore_statements(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    render_restore: Callable[[str], str],
) -> list[NativeStatement]:
    """Return statements recreating each captured previous view when it is missing."""

    return [
        (
            f"{RECOVER_MODEL_VIEW_STEP_PREFIX}{_native.workflow_step_segment(change.view_name)}",
            str(WorkflowPhase.FINALIZATION),
            str(StatementIntent.MUTATION),
            f"{render_restore(change.previous_view_definition).rstrip().rstrip(chr(59))};",
            False,
        )
        for change in changes
        if change.previous_view_definition is not None
    ]
