"""Name the models affected by a failed in-place row-error policy change."""

import sqltide._native as _native
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange


def row_error_policy_failure_message(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    failed_step_id: str,
    cause: BaseException,
) -> str | None:
    """Return the failure text naming affected models, or `None` outside a policy switch."""

    models: tuple[str, ...] = tuple(
        change.model_key.name for change in changes if failed_step_id in _change_step_ids(change)
    )
    if not models:
        return None
    named: str = ", ".join(f"'{model}'" for model in sorted(models))
    details: tuple[str, ...] = (str(cause), *getattr(cause, "__notes__", ()))
    return f"Row-error policy switch failed for model {named}: {'; '.join(details)}"


def _change_step_ids(change: DirectRowErrorPolicyChange) -> frozenset[str]:
    view: str = _native.workflow_step_segment(change.view_name)
    dead_letters: str = _native.workflow_step_segment(change.dead_letter_view_name)
    landing_segments: tuple[str, ...] = tuple(
        _native.workflow_step_segment(name) for name in change.landing_view_names
    )
    return frozenset(
        {
            "wait_for_live_stabilization",
            f"drop_{dead_letters}",
            f"realize_{dead_letters}",
            f"realize_{_native.workflow_step_segment(f'replace_{change.view_name}')}",
            f"realize_{view}",
            f"realize_{_native.workflow_step_segment(f'retire_{change.dead_letter_view_name}')}",
            *(f"pause_source_{segment}" for segment in landing_segments),
            *(f"activate_source_{segment}" for segment in landing_segments),
        }
    )
