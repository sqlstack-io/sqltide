"""Plan rendering shared by plan and backfill preview flows."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.cli.presentation.main._cli_style import cli_style
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.models import DeploymentPlan


def render_plan_result(
    *,
    plan: DeploymentPlan,
    desired_state: DesiredState,
    database: str,
    adapter_name: str,
    json_output: bool,
    verbose: bool = False,
) -> str:
    if json_output:
        return _native.render_plan_json(plan=plan, adapter_name=adapter_name)
    return _native.render_plan_text(
        plan=plan,
        desired_state=desired_state,
        database=database,
        verbose=verbose,
        use_color=cli_style().use_color,
    )
