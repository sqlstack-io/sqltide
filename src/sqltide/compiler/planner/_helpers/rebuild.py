"""Conservative rebuild subtree selection helpers."""

from __future__ import annotations

from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.compiler.compile.models import (
    DesiredState,
    ObjectKey,
)
from sqltide.compiler.discovery.types import ReplayOnChangeMode
from sqltide.compiler.graph.exceptions import GraphInputError
from sqltide.compiler.graph.main._build_native_desired_graph import build_native_desired_graph
from sqltide.compiler.graph.main._descendant_keys import descendant_keys
from sqltide.compiler.graph.main._nearest_upstream_replay_anchor_key import (
    nearest_upstream_replay_anchor_key,
)
from sqltide.compiler.graph.models import NativeDesiredGraph
from sqltide.compiler.planner.constants import (
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_STRATEGY_SHADOW,
)
from sqltide.compiler.planner.models import PlannedObjectChange, RebuildSubtree
from sqltide.compiler.planner.types import (
    NativeObjectChange,
    NativeRebuildSubtree,
    RebuildExecutionMode,
)


def build_rebuild_subtree(
    *,
    desired_state: DesiredState,
    root_key: ObjectKey,
    execution_mode: RebuildExecutionMode = REBUILD_EXECUTION_MODE_FULL,
    forced_full_refresh: bool = False,
    forced_start_time: str | None = None,
    configured_backfill_mode: ReplayOnChangeMode | None = None,
    execution_lookback_seconds: int | None = None,
) -> RebuildSubtree:
    """Build a conservative rebuild subtree from a changed desired object key."""

    return RebuildSubtree(
        root_key=root_key,
        affected_keys=descendant_keys(desired_state=desired_state, root_key=root_key),
        upstream_boundary_key=nearest_upstream_replay_anchor_key(
            desired_state=desired_state,
            root_key=root_key,
            allow_root_key=False,
        ),
        strategy=REBUILD_STRATEGY_SHADOW,
        replay_required=root_key.object_type != DESIRED_OBJECT_TYPE_VIEW,
        execution_mode=execution_mode,
        forced_full_refresh=forced_full_refresh,
        forced_start_time=forced_start_time,
        configured_backfill_mode=configured_backfill_mode,
        execution_lookback_seconds=execution_lookback_seconds,
    )


def emit_rebuild_subtrees_from_changes(
    *,
    desired_state: DesiredState,
    object_changes: tuple[PlannedObjectChange, ...],
) -> tuple[RebuildSubtree, ...]:
    """Build rebuild subtrees while collapsing descendant candidate roots."""

    graph: NativeDesiredGraph = build_native_desired_graph(desired_state=desired_state)
    changes: list[NativeObjectChange] = [
        _native_change(graph=graph, object_change=object_change) for object_change in object_changes
    ]
    try:
        subtrees: list[NativeRebuildSubtree] = graph.native.rebuild_subtrees(changes)
    except ValueError as error:
        raise GraphInputError(str(error)) from None
    return tuple(_rebuild_subtree(graph=graph, subtree=subtree) for subtree in subtrees)


def _native_change(
    *, graph: NativeDesiredGraph, object_change: PlannedObjectChange
) -> NativeObjectChange:
    return (
        graph.index_by_key[object_change.key],
        str(object_change.change_type),
        object_change.force_full_refresh,
        object_change.forced_start_time,
        None if object_change.schema_change_kind is None else str(object_change.schema_change_kind),
        None if object_change.seed_compatibility is None else str(object_change.seed_compatibility),
    )


def _rebuild_subtree(*, graph: NativeDesiredGraph, subtree: NativeRebuildSubtree) -> RebuildSubtree:
    (
        root,
        affected,
        boundary,
        replay_required,
        execution_mode,
        forced_full_refresh,
        forced_start_time,
        backfill_mode,
        lookback_seconds,
    ) = subtree
    return RebuildSubtree(
        root_key=graph.keys[root],
        affected_keys=tuple(graph.keys[index] for index in affected),
        upstream_boundary_key=graph.keys[boundary],
        strategy=REBUILD_STRATEGY_SHADOW,
        replay_required=replay_required,
        execution_mode=RebuildExecutionMode(execution_mode),
        forced_full_refresh=forced_full_refresh,
        forced_start_time=forced_start_time,
        configured_backfill_mode=None
        if backfill_mode is None
        else ReplayOnChangeMode(backfill_mode),
        execution_lookback_seconds=lookback_seconds,
    )
