"""Direct plan text rendering shared by plan and build flows."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.cli.presentation.main._cli_style import cli_style
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange
from sqltide.executor.population.models import PopulationSourceRecreation


def render_direct_plan_text(
    *,
    plan: DirectPlan,
    adapter_name: str,
    verbose: bool = False,
    source_recreations: tuple[PopulationSourceRecreation, ...] = (),
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = (),
) -> str:
    """Render one direct plan as operator-facing text in the terminal's colour mode."""

    return _native.render_direct_plan_text(
        plan=plan,
        adapter_name=adapter_name,
        use_color=cli_style().use_color,
        verbose=verbose,
        source_recreations=source_recreations,
        row_error_policy_changes=row_error_policy_changes,
    )
