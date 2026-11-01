"""Build replay requests for shared population workflows."""

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterPhysicalRelationMapping,
    AdapterReplayBoundary,
    AdapterReplayColumns,
    AdapterReplayQuery,
    AdapterReplayRelations,
    AdapterReplayRequest,
    AdapterReplayWindow,
)
from sqltide.adapter.types import (
    AdapterReplayBoundaryMode,
    AdapterReplayLowerBoundMode,
    AdapterReplaySeedMode,
)
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.planner.main.build_adapter_replay_query import build_adapter_replay_query
from sqltide.executor.population.models import PopulationPlan, PopulationWatermark

type _NativeReplayPlan = tuple[
    int,
    str,
    str,
    tuple[str, str, str, str],
    int,
    list[tuple[str, str, str | None]],
    list[str],
    tuple[str, str, str | None, int | None],
    bool,
    list[str],
    list[tuple[str, str]],
    bool,
]

_TEMPLATE_BOUNDARY_TIME: str = "1970-01-01 00:00:00.000"


def build_population_replay_templates(
    *, plan: PopulationPlan, desired_state: DesiredState, default_database: str
) -> tuple[tuple[ObjectKey, AdapterReplayRequest], ...]:
    """Build one boundary-independent full-replay template per population root."""

    return _build_requests(
        plan=plan,
        desired_state=desired_state,
        default_database=default_database,
        watermarks=None,
        boundary_time=_TEMPLATE_BOUNDARY_TIME,
    )


def build_population_replay_requests(
    *,
    plan: PopulationPlan,
    desired_state: DesiredState,
    default_database: str,
    watermarks: tuple[PopulationWatermark, ...],
    boundary_time: str,
) -> tuple[tuple[ObjectKey, AdapterReplayRequest], ...]:
    return _build_requests(
        plan=plan,
        desired_state=desired_state,
        default_database=default_database,
        watermarks=watermarks,
        boundary_time=boundary_time,
    )


def _build_requests(
    *,
    plan: PopulationPlan,
    desired_state: DesiredState,
    default_database: str,
    watermarks: tuple[PopulationWatermark, ...] | None,
    boundary_time: str,
) -> tuple[tuple[ObjectKey, AdapterReplayRequest], ...]:
    index_by_key: dict[ObjectKey, int] = {}
    candidate_keys: list[ObjectKey] = [
        *(object_.key for object_ in desired_state.objects),
        *(prepared.logical_key for prepared in plan.objects),
        *(config.key for config in desired_state.external_source_replay_configs),
        *(root.root_key for root in plan.roots),
        *(root.upstream_boundary_key for root in plan.roots),
    ]
    for key in candidate_keys:
        index_by_key.setdefault(key, len(index_by_key))
    keys: tuple[ObjectKey, ...] = tuple(index_by_key)
    tables: list[DesiredTable] = [
        object_ for object_ in desired_state.objects if isinstance(object_, DesiredTable)
    ]
    views: list[DesiredMaterializedView] = [
        object_ for object_ in desired_state.objects if isinstance(object_, DesiredMaterializedView)
    ]
    mappings, replay_plans = _native.plan_population_replays(
        roots=[
            (
                index_by_key[root.root_key],
                index_by_key[root.upstream_boundary_key],
                str(root.replay_lineage_mode),
                str(root.execution_mode),
                root.forced_start_time,
                root.execution_lookback_seconds,
            )
            for root in plan.roots
        ],
        tables=[_native_table(table=table, index=index_by_key[table.key]) for table in tables],
        views=[(view.target_table_name, view.source_table_name) for view in views],
        key_names=[key.name for key in keys],
        physical_names=[
            (index_by_key[prepared.logical_key], prepared.physical_name)
            for prepared in plan.objects
        ],
        external_columns=[
            (
                index_by_key[config.key],
                (
                    config.partition_column_name,
                    config.offset_column_name,
                    config.timestamp_column_name,
                    config.landed_at_column_name,
                    config.cursor_column_name,
                ),
            )
            for config in desired_state.external_source_replay_configs
        ],
        replay_anchor_keys={index_by_key[key] for key in desired_state.replay_anchor_keys},
        default_database=default_database,
        boundary_time=boundary_time,
        watermarks=None
        if watermarks is None
        else [
            (index_by_key[watermark.root_key], watermark.boundary_key, watermark.cutoff_value)
            for watermark in watermarks
            if watermark.root_key in index_by_key
        ],
    )
    physical_mappings: tuple[AdapterPhysicalRelationMapping, ...] = tuple(
        AdapterPhysicalRelationMapping(logical_name=logical_name, physical_name=physical_name)
        for logical_name, physical_name in mappings
    )
    return tuple(
        _replay_request(
            keys=keys,
            views=views,
            physical_mappings=physical_mappings,
            native_plan=native_plan,
        )
        for native_plan in replay_plans
    )


def _native_table(
    *, table: DesiredTable, index: int
) -> tuple[int, str, str | None, list[str], list[tuple[str, str]]]:
    column_names: list[str] = [column.name for column in table.columns]
    return (
        index,
        table.name,
        table.key.database,
        column_names,
        list((table.replay_settings or {}).items()),
    )


def _replay_request(
    *,
    keys: tuple[ObjectKey, ...],
    views: list[DesiredMaterializedView],
    physical_mappings: tuple[AdapterPhysicalRelationMapping, ...],
    native_plan: _NativeReplayPlan,
) -> tuple[ObjectKey, AdapterReplayRequest]:
    (
        root,
        mode,
        database,
        (root_name, source_name, anchor_name, target_name),
        view_index,
        boundaries,
        columns,
        (lower_bound_mode, boundary_time, forced_start_time, lookback_seconds),
        seeds_history,
        target_column_names,
        settings,
        filter_boundaries_at_source,
    ) = native_plan
    view: DesiredMaterializedView = views[view_index]
    replay_query: AdapterReplayQuery = build_adapter_replay_query(
        query=view.query,
        database_template=view.database_template,
        source_relation_name=view.source_table_name,
        database=database,
        physical_relation_mappings=physical_mappings,
    )
    return (
        keys[root],
        AdapterReplayRequest(
            mode=AdapterReplayBoundaryMode(mode),
            database=database,
            relations=AdapterReplayRelations(
                root=root_name, source=source_name, anchor=anchor_name, target=target_name
            ),
            replay_query=replay_query,
            boundaries=tuple(
                AdapterReplayBoundary(
                    boundary_key=boundary_key,
                    cutoff_value=cutoff_value,
                    cutoff_inclusive=True,
                    partition_value=partition_value,
                )
                for boundary_key, cutoff_value, partition_value in boundaries
            ),
            columns=AdapterReplayColumns(
                partition=columns[0],
                offset=columns[1],
                timestamp=columns[2],
                landed_at=columns[3],
                cursor=columns[4],
            ),
            window=AdapterReplayWindow(
                lower_bound_mode=AdapterReplayLowerBoundMode(lower_bound_mode),
                lower_bound_inclusive=True,
                boundary_time=boundary_time,
                forced_start_time=forced_start_time,
                lookback_seconds=lookback_seconds,
            ),
            seed_mode=AdapterReplaySeedMode.HISTORY_PREFIX
            if seeds_history
            else AdapterReplaySeedMode.NONE,
            target_column_names=tuple(target_column_names),
            settings=tuple(settings),
            filter_boundaries_at_source=filter_boundaries_at_source,
        ),
    )
