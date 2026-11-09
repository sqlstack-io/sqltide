"""Render one direct plan as deterministic JSON."""

from sqltide.cli.plan._helpers.direct_rendering import (
    render_direct_plan_json as _render_direct_plan_json,
)
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange
from sqltide.executor.population.models import PopulationSourceRecreation


def render_direct_plan_json(
    *,
    plan: DirectPlan,
    adapter_name: str,
    source_recreations: tuple[PopulationSourceRecreation, ...] = (),
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = (),
) -> str:
    """Return the complete direct plan as deterministic JSON."""

    return _render_direct_plan_json(
        plan=plan,
        adapter_name=adapter_name,
        source_recreations=source_recreations,
        row_error_policy_changes=row_error_policy_changes,
    )
