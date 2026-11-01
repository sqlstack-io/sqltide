"""Deployment-plan step emission helpers."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.adapter.types import AdapterResourceRenderer
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ObjectKey,
)
from sqltide.compiler.planner._helpers.diff import classify_object_changes
from sqltide.compiler.planner._helpers.rebuild import emit_rebuild_subtrees_from_changes
from sqltide.compiler.planner._helpers.sql_diffs import build_planned_sql_diffs
from sqltide.compiler.planner.constants import (
    MUTABLE_REF_WARNING_CODE,
    MUTABLE_REF_WARNING_MESSAGE,
)
from sqltide.compiler.planner.models import (
    ActualState,
    DeploymentPlan,
    DeploymentStep,
    PlannedObjectChange,
    PlannedSqlDiff,
    PlannerWarning,
    PreparedShadowObject,
    RebuildSubtree,
)


def build_deployment_plan(
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

    object_changes: tuple[PlannedObjectChange, ...] = classify_object_changes(
        desired_state=desired_state,
        actual_state=actual_state,
        full_refresh_keys=full_refresh_keys,
        start_time_keys=start_time_keys,
        start_time=start_time,
    )
    rebuild_subtrees: tuple[RebuildSubtree, ...] = emit_rebuild_subtrees_from_changes(
        desired_state=desired_state,
        object_changes=object_changes,
    )
    steps, prepared_shadow_objects, warnings = stage_deployment(
        desired_state=desired_state,
        rebuild_subtrees=rebuild_subtrees,
        deployment_id=deployment_id,
    )
    sql_diffs: tuple[PlannedSqlDiff, ...] = build_planned_sql_diffs(
        desired_state=desired_state,
        actual_state=actual_state,
        object_changes=object_changes,
        default_database=default_database,
        render_resource=render_resource,
    )
    return DeploymentPlan(
        deployment_id=deployment_id,
        object_changes=object_changes,
        rebuild_subtrees=rebuild_subtrees,
        steps=steps,
        prepared_shadow_objects=prepared_shadow_objects,
        warnings=warnings,
        sql_diffs=sql_diffs,
    )


def stage_deployment(
    *,
    desired_state: DesiredState,
    rebuild_subtrees: tuple[RebuildSubtree, ...],
    deployment_id: str | None,
) -> tuple[
    tuple[DeploymentStep, ...], tuple[PreparedShadowObject, ...], tuple[PlannerWarning, ...]
]:
    """Stage rebuild subtrees into steps, shadow identities, and rollout warnings."""

    index_by_key: dict[ObjectKey, int] = {}
    native_subtrees: list[tuple[int, list[int]]] = []
    for subtree in rebuild_subtrees:
        for key in (subtree.root_key, *subtree.affected_keys):
            index_by_key.setdefault(key, len(index_by_key))
        native_subtrees.append(
            (index_by_key[subtree.root_key], [index_by_key[key] for key in subtree.affected_keys])
        )
    keys: tuple[ObjectKey, ...] = tuple(index_by_key)
    steps, shadow_objects, warnings = _native.stage_deployment(
        [
            (str(key.object_type), key.name, key in desired_state.mutable_ref_warning_keys)
            for key in keys
        ],
        native_subtrees,
        deployment_id,
    )
    logical_model_name_by_key: dict[ObjectKey, str] = {
        object_.key: object_.logical_model_name
        for object_ in desired_state.objects
        if isinstance(object_, (DesiredTable, DesiredMaterializedView, DesiredView))
        and object_.logical_model_name is not None
    }
    return (
        tuple(
            DeploymentStep(
                step_id=step_id,
                phase=phase,
                action=action,
                root_key=keys[root],
                target_key=None if target is None else keys[target],
                physical_name=physical_name,
            )
            for step_id, phase, action, root, target, physical_name in steps
        ),
        tuple(
            PreparedShadowObject(
                logical_key=keys[key],
                physical_name=physical_name,
                logical_model_name=logical_model_name_by_key[keys[key]],
            )
            for key, physical_name in shadow_objects
        ),
        tuple(
            PlannerWarning(
                warning_code=MUTABLE_REF_WARNING_CODE,
                message=MUTABLE_REF_WARNING_MESSAGE,
                root_key=keys[root],
                target_key=keys[target],
            )
            for root, target in warnings
        ),
    )
