"""Primary planner entrypoints."""

import sqltide._native as _native
from sqltide.adapter.types import AdapterResourceRenderer
from sqltide.compiler.compile.models import DesiredState, ObjectKey
from sqltide.compiler.planner.models import ActualState, DeploymentPlan


def plan_deployment(
    *,
    desired_state: DesiredState,
    actual_state: ActualState,
    default_database: str,
    render_resource: AdapterResourceRenderer,
    deployment_id: str | None = None,
    full_refresh_keys: frozenset[ObjectKey] = frozenset(),
    start_time_keys: frozenset[ObjectKey] = frozenset(),
    start_time: str | None = None,
) -> DeploymentPlan:
    """Build a conservative staged deployment plan from desired and actual state."""

    return _native.plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database=default_database,
        render_resource=render_resource,
        deployment_id=deployment_id,
        full_refresh_keys=full_refresh_keys,
        start_time_keys=start_time_keys,
        start_time=start_time,
    )
