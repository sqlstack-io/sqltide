"""Recover the live model views replaced by in-place row-error policy changes."""

import re
from collections.abc import Callable

import sqltide._native as _native
from sqltide.adapter.constants import QUARANTINE_LIVE_VIEW_PATTERN
from sqltide.compiler.discovery.types import RowErrorPolicy
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from sqltide.executor.workflow.constants import (
    RECOVER_DEAD_LETTERS_STEP_PREFIX,
    RECOVER_MODEL_VIEW_STEP_PREFIX,
)
from sqltide.executor.workflow.types import NativeStatement, StatementIntent, WorkflowPhase


def row_error_policy_restore_statements(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    render_restore: Callable[[str], str],
    database: str,
) -> list[NativeStatement]:
    """Return statements restoring previous views and dropping dead-letter views a switch made."""

    switched: tuple[DirectRowErrorPolicyChange, ...] = tuple(
        change
        for change in changes
        if change.previous_view_definition is not None and not change.landing_repair_only
    )
    restores: list[NativeStatement] = [
        (
            f"{RECOVER_MODEL_VIEW_STEP_PREFIX}{_native.workflow_step_segment(change.view_name)}",
            str(WorkflowPhase.FINALIZATION),
            str(StatementIntent.MUTATION),
            f"{render_restore(change.previous_view_definition or '').rstrip().rstrip(chr(59))};",
            False,
        )
        for change in switched
    ]
    created_dead_letter_views: list[NativeStatement] = [
        (
            f"{RECOVER_DEAD_LETTERS_STEP_PREFIX}{_native.workflow_step_segment(change.view_name)}",
            str(WorkflowPhase.FINALIZATION),
            str(StatementIntent.MUTATION),
            f"DROP TABLE IF EXISTS {database}.{change.dead_letter_view_name} SYNC;",
            False,
        )
        for change in switched
        if change.desired_policy is RowErrorPolicy.QUARANTINE
        and re.search(QUARANTINE_LIVE_VIEW_PATTERN, change.previous_view_definition or "") is None
    ]
    return [*restores, *created_dead_letter_views]
