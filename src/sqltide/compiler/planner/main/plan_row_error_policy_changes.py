"""Find live direct models whose row-error policy alone differs from their desired policy."""

from sqltide.adapter.constants import QUARANTINE_VALUE_COLUMN_PREFIX
from sqltide.adapter.models import CatalogRelation
from sqltide.compiler.compile.models import CompiledTableModel, DesiredMaterializedView
from sqltide.compiler.discovery.types import PipelineMode, RowErrorPolicy
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange


def plan_row_error_policy_changes(
    *, analysis: CompileAnalysis, plan: DirectPlan, relations: tuple[CatalogRelation, ...]
) -> tuple[DirectRowErrorPolicyChange, ...]:
    """Return view-only changes for models the plan does not otherwise rebuild.

    The policy a live model was built with is read from its materialized view definition, because
    that definition is what the warehouse actually runs.
    """

    query_by_view: dict[str, str] = {
        relation.name: relation.query_sql or ""
        for relation in relations
        if relation.query_sql is not None
    }
    views: tuple[DesiredMaterializedView, ...] = tuple(
        desired
        for desired in analysis.realized_project.desired_state.objects
        if isinstance(desired, DesiredMaterializedView)
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
            if view is None or view.name not in query_by_view:
                continue
            live_quarantines: bool = QUARANTINE_VALUE_COLUMN_PREFIX in query_by_view[view.name]
            if live_quarantines == (model.on_row_error is RowErrorPolicy.QUARANTINE):
                continue
            changes.append(
                DirectRowErrorPolicyChange(
                    model_key=model.key,
                    view_name=view.name,
                    dead_letter_view_name=f"{view.name}__dead_letters",
                    desired_policy=model.on_row_error,
                    landing_view_names=_landing_view_names(view=view, views=views),
                )
            )
    return tuple(changes)


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
