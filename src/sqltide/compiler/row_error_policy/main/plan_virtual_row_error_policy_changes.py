"""Find live virtual deployment models whose row-error policy differs from their desired policy."""

from sqltide.adapter.models import CatalogRelation
from sqltide.compiler.compile.models import CompiledTableModel, DesiredMaterializedView
from sqltide.compiler.discovery.types import PipelineMode, RowErrorPolicy
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.constants import DIRECT_DEAD_LETTERS_VIEW_SUFFIX
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from sqltide.compiler.row_error_policy._helpers.detection import (
    change_pending,
    landing_view_names,
)


def plan_virtual_row_error_policy_changes(
    *,
    analysis: CompileAnalysis,
    planned_model_names: frozenset[str],
    relations: tuple[CatalogRelation, ...],
) -> tuple[DirectRowErrorPolicyChange, ...]:
    """Return view-only changes on each live deployment for models not otherwise rebuilt."""

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
    changes: list[DirectRowErrorPolicyChange] = []
    for pipeline in analysis.compiled_project.pipelines:
        if PipelineMode(pipeline.pipeline.mode) is not PipelineMode.VIRTUAL:
            continue
        for model in pipeline.models:
            if not isinstance(model, CompiledTableModel) or model.key.name in planned_model_names:
                continue
            view: DesiredMaterializedView | None = next(
                (
                    candidate
                    for candidate in views
                    if candidate.logical_model_name == model.key.name and not candidate.dead_letters
                ),
                None,
            )
            binding: CatalogRelation | None = (
                None if view is None else relation_by_name.get(view.target_table_name)
            )
            physical_table: str | None = None if binding is None else binding.stable_binding_name
            if view is None or physical_table is None:
                continue
            suffix: str = physical_table.removeprefix(view.target_table_name)
            logical_dead_letters: str = f"{view.name}{DIRECT_DEAD_LETTERS_VIEW_SUFFIX}"
            if not change_pending(
                target_name=physical_table,
                view_name=f"{view.name}{suffix}",
                dead_letter_view_name=f"{logical_dead_letters}{suffix}",
                relation_by_name=relation_by_name,
                dead_letter_name_owned_by_model=logical_dead_letters in model_view_names,
                quarantines=model.on_row_error is RowErrorPolicy.QUARANTINE,
            ):
                continue
            changes.append(
                DirectRowErrorPolicyChange(
                    model_key=model.key,
                    view_name=f"{view.name}{suffix}",
                    dead_letter_view_name=f"{logical_dead_letters}{suffix}",
                    desired_policy=model.on_row_error,
                    landing_view_names=landing_view_names(view=view, views=views),
                    logical_view_name=view.name,
                    logical_dead_letter_view_name=logical_dead_letters,
                )
            )
    return tuple(changes)
