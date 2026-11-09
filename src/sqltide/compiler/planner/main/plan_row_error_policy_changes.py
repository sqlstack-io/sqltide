"""Find live direct models whose row-error policy alone differs from their desired policy."""

import re

from sqltide.adapter.constants import QUARANTINE_LIVE_VIEW_PATTERN
from sqltide.adapter.models import CatalogRelation
from sqltide.compiler.compile.models import CompiledTableModel, DesiredMaterializedView
from sqltide.compiler.discovery.types import PipelineMode, RowErrorPolicy
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.constants import DIRECT_DEAD_LETTERS_VIEW_SUFFIX
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange


def plan_row_error_policy_changes(
    *, analysis: CompileAnalysis, plan: DirectPlan, relations: tuple[CatalogRelation, ...]
) -> tuple[DirectRowErrorPolicyChange, ...]:
    """Return view-only changes, reading each live policy from its view definition."""

    relation_by_name: dict[str, CatalogRelation] = {
        relation.name: relation for relation in relations
    }
    views: tuple[DesiredMaterializedView, ...] = tuple(
        desired
        for desired in analysis.realized_project.desired_state.objects
        if isinstance(desired, DesiredMaterializedView)
    )
    model_view_names: frozenset[str] = frozenset(
        candidate.name for candidate in views if not candidate.dead_letters
    )
    scoped: frozenset[object] = frozenset(plan.execution_scope)
    changes: list[DirectRowErrorPolicyChange] = []
    for pipeline in analysis.compiled_project.pipelines:
        if PipelineMode(pipeline.pipeline.mode) is not PipelineMode.DIRECT:
            continue
        for model in pipeline.models:
            if not isinstance(model, CompiledTableModel) or model.key in scoped:
                continue
            view: DesiredMaterializedView | None = next(
                (
                    candidate
                    for candidate in views
                    if candidate.logical_model_name == model.key.name and not candidate.dead_letters
                ),
                None,
            )
            if view is None or not _change_pending(
                view=view,
                dead_letter_view_name=f"{view.name}{DIRECT_DEAD_LETTERS_VIEW_SUFFIX}",
                relation_by_name=relation_by_name,
                model_view_names=model_view_names,
                quarantines=model.on_row_error is RowErrorPolicy.QUARANTINE,
            ):
                continue
            changes.append(
                DirectRowErrorPolicyChange(
                    model_key=model.key,
                    view_name=view.name,
                    dead_letter_view_name=f"{view.name}{DIRECT_DEAD_LETTERS_VIEW_SUFFIX}",
                    desired_policy=model.on_row_error,
                    landing_view_names=_landing_view_names(view=view, views=views),
                )
            )
    return tuple(changes)


def _change_pending(
    *,
    view: DesiredMaterializedView,
    dead_letter_view_name: str,
    relation_by_name: dict[str, CatalogRelation],
    model_view_names: frozenset[str],
    quarantines: bool,
) -> bool:
    """Return whether the live views differ from the policy, repairing a half-applied switch."""

    if view.target_table_name not in relation_by_name:
        return False
    live_view: CatalogRelation | None = relation_by_name.get(view.name)
    has_dead_letter_view: bool = (
        dead_letter_view_name in relation_by_name and dead_letter_view_name not in model_view_names
    )
    live_quarantines: bool = (
        live_view is not None
        and re.search(QUARANTINE_LIVE_VIEW_PATTERN, live_view.query_sql or "") is not None
    )
    if quarantines:
        return live_view is None or not live_quarantines or not has_dead_letter_view
    return has_dead_letter_view or live_quarantines


def _landing_view_names(
    *, view: DesiredMaterializedView, views: tuple[DesiredMaterializedView, ...]
) -> tuple[str, ...]:
    current: DesiredMaterializedView = view
    visited: set[str] = {current.name}
    while True:
        parent: DesiredMaterializedView | None = next(
            (
                candidate
                for candidate in views
                if candidate.target_table_name == current.source_table_name
                and not candidate.dead_letters
                and candidate.name not in visited
            ),
            None,
        )
        if parent is None:
            return () if current is view else (current.name,)
        visited.add(parent.name)
        current = parent
