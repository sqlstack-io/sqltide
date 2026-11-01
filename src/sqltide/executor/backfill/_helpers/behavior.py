from __future__ import annotations

from dataclasses import replace

import sqltide._native as _native
from sqltide.adapter.models import CatalogRelation, CatalogSnapshot
from sqltide.compiler.compile.models import DesiredState, DesiredTable
from sqltide.compiler.discovery.types import BoundedReplayFallback, ReplayLineageMode
from sqltide.compiler.planner.constants import (
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
)
from sqltide.compiler.planner.models import RebuildSubtree
from sqltide.executor.backfill.types import BoundedReplayOutcome


def _resolve_subtree_behavior(
    *,
    catalog: CatalogSnapshot,
    subtree: RebuildSubtree,
    desired_state: DesiredState,
    default_database: str,
    replay_lineage_mode: ReplayLineageMode,
) -> RebuildSubtree:
    if subtree.execution_mode != REBUILD_EXECUTION_MODE_SEEDED_BOUNDED:
        return subtree

    root_table: DesiredTable | None = next(
        (
            object_
            for object_ in desired_state.objects
            if isinstance(object_, DesiredTable) and object_.key == subtree.root_key
        ),
        None,
    )
    if root_table is None:
        return subtree
    fallback: BoundedReplayFallback = BoundedReplayFallback(root_table.bounded_replay_fallback)
    outcome: BoundedReplayOutcome = BoundedReplayOutcome(
        _native.bounded_replay_outcome(
            str(replay_lineage_mode),
            subtree.forced_start_time is not None,
            _live_column_names(catalog=catalog, logical_table_name=root_table.name),
            fallback == BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
        )
    )
    if outcome == BoundedReplayOutcome.KEEP:
        return subtree
    if outcome == BoundedReplayOutcome.WITHOUT_HISTORY:
        return replace(
            subtree,
            execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
            history_preserving_bounded_supported=False,
            resolved_bounded_replay_fallback=fallback,
        )
    return replace(
        subtree,
        execution_mode=REBUILD_EXECUTION_MODE_FULL,
        requested_start_time=subtree.forced_start_time,
        forced_start_time=None,
        execution_lookback_seconds=None,
        history_preserving_bounded_supported=False,
        resolved_bounded_replay_fallback=fallback,
    )


def _live_column_names(*, catalog: CatalogSnapshot, logical_table_name: str) -> set[str] | None:
    """Columns of the physical table the logical root binds, or None without a binding."""

    logical_relation: CatalogRelation | None = catalog.relation(logical_table_name)
    if logical_relation is None or logical_relation.stable_binding_name is None:
        return None
    relation: CatalogRelation | None = catalog.relation(logical_relation.stable_binding_name)
    if relation is None:
        return set()
    return {column.name for column in relation.columns}
