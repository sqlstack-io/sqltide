"""Expand replay roots where direct catch-up prevents fan-in data loss."""

from dataclasses import replace

import sqltide._native as _native
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.executor.population.exceptions import PopulationScopeError
from sqltide.executor.population.main._index_population_keys import index_population_keys
from sqltide.executor.population.models import PopulationPlan, PopulationRoot


def expand_fan_in_roots(*, plan: PopulationPlan, desired_state: DesiredState) -> PopulationPlan:
    """Add direct catch-up roots for in-scope side-reference fan-in targets."""

    views: list[DesiredMaterializedView] = [
        object_ for object_ in desired_state.objects if isinstance(object_, DesiredMaterializedView)
    ]
    tables: list[DesiredTable] = [
        object_ for object_ in desired_state.objects if isinstance(object_, DesiredTable)
    ]
    index_by_key: dict[ObjectKey, int] = index_population_keys(
        (
            *(object_.logical_key for object_ in plan.objects),
            *_root_keys(plan.roots),
            *(table.key for table in tables),
            *_view_keys(views),
        )
    )
    keys: tuple[ObjectKey, ...] = tuple(index_by_key)
    planned: set[int] = {index_by_key[object_.logical_key] for object_ in plan.objects}
    native_roots: list[tuple[int, list[int]]] = [
        (index_by_key[root.root_key], _indexes(keys=root.affected_keys, index_by_key=index_by_key))
        for root in plan.roots
    ]
    native_tables: list[tuple[str, int]] = [
        (table.name, index_by_key[table.key]) for table in tables
    ]
    native_views: list[tuple[int, str, str, list[int]]] = [
        (
            index_by_key[view.key],
            view.source_table_name,
            view.target_table_name,
            _indexes(keys=view.deps, index_by_key=index_by_key),
        )
        for view in views
    ]
    added: list[tuple[int, int, tuple[int, int], int]] = []
    try:
        added = _native.population_fan_in_roots(planned, native_roots, native_tables, native_views)
    except ValueError as error:
        raise PopulationScopeError.from_native(error=error, keys=keys) from None
    return replace(
        plan,
        roots=(
            *plan.roots,
            *(
                replace(
                    plan.roots[owner],
                    root_key=keys[root_key],
                    affected_keys=(keys[target_key], keys[view_key]),
                    upstream_boundary_key=keys[source_key],
                    persist_watermarks=False,
                )
                for owner, root_key, (target_key, view_key), source_key in added
            ),
        ),
    )


def _indexes(*, keys: tuple[ObjectKey, ...], index_by_key: dict[ObjectKey, int]) -> list[int]:
    return [index_by_key[key] for key in keys]


def _root_keys(roots: tuple[PopulationRoot, ...]) -> list[ObjectKey]:
    keys: list[ObjectKey] = []
    root: PopulationRoot
    for root in roots:
        keys.append(root.root_key)
        keys.extend(root.affected_keys)
    return keys


def _view_keys(views: list[DesiredMaterializedView]) -> list[ObjectKey]:
    keys: list[ObjectKey] = []
    view: DesiredMaterializedView
    for view in views:
        keys.append(view.key)
        keys.extend(view.deps)
    return keys
