"""Find live direct models whose row-error policy alone differs from their desired policy."""

from sqltide.adapter.models import CatalogRelation
from sqltide.compiler.compile.models import CompiledTableModel, DesiredMaterializedView
from sqltide.compiler.discovery.types import PipelineMode, RowErrorPolicy
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.constants import DIRECT_DEAD_LETTERS_VIEW_SUFFIX
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange
from sqltide.compiler.row_error_policy._helpers.detection import (
    change_pending,
    landing_view_names,
)


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
            if view is None:
                continue
            logical_dead_letters: str = f"{view.name}{DIRECT_DEAD_LETTERS_VIEW_SUFFIX}"
            landing_views: tuple[str, ...] = landing_view_names(view=view, views=views)
            if not change_pending(
                target_name=view.target_table_name,
                view_name=view.name,
                dead_letter_view_name=logical_dead_letters,
                relation_by_name=relation_by_name,
                dead_letter_name_owned_by_model=logical_dead_letters in model_view_names,
                quarantines=model.on_row_error is RowErrorPolicy.QUARANTINE,
                landing_view_names=landing_views,
            ):
                continue
            changes.append(
                DirectRowErrorPolicyChange(
                    model_key=model.key,
                    view_name=view.name,
                    dead_letter_view_name=logical_dead_letters,
                    desired_policy=model.on_row_error,
                    landing_view_names=landing_views,
                    logical_view_name=view.name,
                    logical_dead_letter_view_name=logical_dead_letters,
                    previous_view_definition=(
                        None
                        if (live_view := relation_by_name.get(view.name)) is None
                        else live_view.definition_sql
                    ),
                )
            )
    return tuple(changes)
