"""Public entrypoint for direct-mode execution planning."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.compiler.compile.models import LogicalResourceKey
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import (
    DirectPlan,
    DirectSourceDriftSelection,
    DirectWarehouseSnapshot,
)
from sqltide.compiler.planner.types import DirectSelectionMode


def plan_direct_build(
    *,
    graph: ProjectGraph,
    realized_project: RealizedProject,
    snapshot: DirectWarehouseSnapshot,
    database: str,
    selected_model_keys: frozenset[LogicalResourceKey],
    selection_mode: DirectSelectionMode | None = None,
    include_missing_upstream: bool = False,
    effective_start_time: str | None = None,
    source_drift_selections: tuple[DirectSourceDriftSelection, ...] = (),
) -> DirectPlan:
    """Plan the complete selected downstream closure without change pruning."""

    return _native.plan_direct_build(
        graph=graph,
        realized_project=realized_project,
        snapshot=snapshot,
        database=database,
        selected_model_keys=selected_model_keys,
        selection_mode=selection_mode,
        include_missing_upstream=include_missing_upstream,
        effective_start_time=effective_start_time,
        source_drift_selections=source_drift_selections,
    )
