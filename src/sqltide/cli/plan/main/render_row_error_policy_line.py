"""Render one in-place row-error policy change for operators."""

from sqltide.compiler.planner.models import DirectRowErrorPolicyChange


def render_row_error_policy_line(change: DirectRowErrorPolicyChange) -> str:
    """Return the plan line, keeping landing-view repairs apart from policy switches."""

    if change.landing_repair_only:
        return (
            f"repair landing view  {change.model_key.name}: "
            f"recreate {', '.join(change.landing_view_names)}, no replay"
        )
    return (
        f"view-only  {change.model_key.name}: on_row_error -> "
        f"{change.desired_policy}, recreate {change.view_name}, no replay"
    )
