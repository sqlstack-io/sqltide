from __future__ import annotations

from sqltide._native import collect_reachable_keys
from sqltide.cli.selection._helpers.selection import (
    expand_included_keys,
    filter_desired_state,
    physical_model_keys,
    resolve_replay_lineage_mode,
    resolve_selected_logical_model_keys,
)
from sqltide.cli.selection.models import SelectionResolution
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    DesiredState,
    LogicalResourceKey,
    ObjectKey,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.graph.constants import ALL_DEPENDENCY_EDGE_TYPES
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.graph.types import GraphTraversalDirection
from sqltide.compiler.pipeline.models import RealizedProject


def resolve_selection(
    *,
    realized_project: RealizedProject,
    graph: ProjectGraph,
    selectors: tuple[str, ...],
) -> SelectionResolution:
    compiled_pipelines: tuple[CompiledPipeline, ...] = realized_project.project.pipelines
    full_desired_state: DesiredState = realized_project.desired_state
    if not selectors:
        execution_logical_model_keys: frozenset[LogicalResourceKey] = frozenset(
            model.key for model in realized_project.project.models
        )
        return SelectionResolution(
            desired_state=full_desired_state,
            selected_logical_model_keys=frozenset(),
            selected_model_keys=frozenset(),
            replay_lineage_mode=resolve_replay_lineage_mode(
                compiled_pipelines=compiled_pipelines,
                selected_model_keys=frozenset(),
            ),
            execution_logical_model_keys=execution_logical_model_keys,
        )

    selected_logical_model_keys: frozenset[LogicalResourceKey] = (
        resolve_selected_logical_model_keys(
            compiled_pipelines=compiled_pipelines,
            selectors=selectors,
        )
    )
    selected_model_keys: frozenset[ObjectKey] = physical_model_keys(
        realized_project=realized_project,
        logical_model_keys=selected_logical_model_keys,
    )
    execution_logical_model_keys: frozenset[LogicalResourceKey] = frozenset(
        key
        for key in collect_reachable_keys(
            graph=graph,
            root_keys=selected_logical_model_keys,
            direction=GraphTraversalDirection.DOWNSTREAM,
            edge_types=ALL_DEPENDENCY_EDGE_TYPES,
        )
        if key.resource_type == LogicalResourceType.MODEL
    )
    execution_model_keys: frozenset[ObjectKey] = physical_model_keys(
        realized_project=realized_project,
        logical_model_keys=execution_logical_model_keys,
    )
    included_keys: frozenset[ObjectKey] = expand_included_keys(
        compiled_pipelines=compiled_pipelines,
        realized_project=realized_project,
        desired_state=full_desired_state,
        selected_model_keys=execution_model_keys,
    )
    return SelectionResolution(
        desired_state=filter_desired_state(
            desired_state=full_desired_state, included_keys=included_keys
        ),
        selected_logical_model_keys=selected_logical_model_keys,
        selected_model_keys=selected_model_keys,
        replay_lineage_mode=resolve_replay_lineage_mode(
            compiled_pipelines=compiled_pipelines,
            selected_model_keys=execution_logical_model_keys,
        ),
        execution_logical_model_keys=execution_logical_model_keys,
    )
