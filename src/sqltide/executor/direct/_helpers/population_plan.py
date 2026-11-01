"""Normalize a direct closure into the shared population contract."""

import sqltide._native as _native
from sqltide.adapter.models import AdapterMaterializedView, AdapterTable, AdapterView
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    LogicalResourceKey,
    ObjectKey,
)
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import DirectPlan, DirectReplayRoot
from sqltide.executor.population.constants import (
    SOURCE_OBJECT_MATERIALIZED_VIEW,
    SOURCE_OBJECT_TABLE,
    SOURCE_OBJECT_VIEW,
)
from sqltide.executor.population.exceptions import PopulationScopeError
from sqltide.executor.population.main._index_population_keys import index_population_keys
from sqltide.executor.population.models import PopulationObject, PopulationPlan, PopulationRoot

_OTHER_RESOURCE: tuple[str, str] = ("other", "")


def build_direct_population_plan(
    *, plan: DirectPlan, realized_project: RealizedProject
) -> PopulationPlan:
    """Map direct logical scope to identity-named physical population."""

    desired_state: DesiredState = realized_project.desired_state
    desired: list[DesiredTable | DesiredMaterializedView | DesiredView] = [
        object_
        for object_ in desired_state.objects
        if isinstance(object_, DesiredTable | DesiredMaterializedView | DesiredView)
    ]
    index_by_key: dict[ObjectKey, int] = index_population_keys(
        (
            *(object_.key for object_ in desired),
            *(config.key for config in desired_state.external_source_replay_configs),
        )
    )
    keys: tuple[ObjectKey, ...] = tuple(index_by_key)
    model_keys: tuple[LogicalResourceKey, ...] = tuple(
        dict.fromkeys((*plan.execution_scope, *_root_model_keys(plan.replay_roots)))
    )
    model_index: dict[LogicalResourceKey, int] = {
        key: index for index, key in enumerate(model_keys)
    }
    native_desired: list[tuple[str, int]] = [
        (object_.name, index_by_key[object_.key]) for object_ in desired
    ]
    native_resources: list[list[tuple[str, str]]] = [
        _native_resources(key=key, realized_project=realized_project) for key in model_keys
    ]
    execution_scope: list[int] = [model_index[key] for key in plan.execution_scope]
    native_roots: list[tuple[int, list[int], str]] = [
        (
            model_index[root.model_key],
            _model_indexes(keys=root.propagated_model_keys, model_index=model_index),
            root.driving_input_relation_name,
        )
        for root in plan.replay_roots
    ]
    external_tables: list[tuple[str, int]] = [
        (config.table_name, index_by_key[config.key])
        for config in desired_state.external_source_replay_configs
    ]
    planned_indexes: list[int] = []
    scoped_roots: list[tuple[int, list[int], int]] = []
    try:
        planned_indexes, scoped_roots = _native.direct_population_scope(
            native_desired, native_resources, execution_scope, native_roots, external_tables
        )
    except ValueError as error:
        raise PopulationScopeError.from_native(error=error, keys=keys) from None
    return PopulationPlan(
        execution_id="direct",
        roots=tuple(
            PopulationRoot(
                root_key=keys[root_key],
                affected_keys=_keys_at(indexes=affected, keys=keys),
                upstream_boundary_key=keys[anchor],
                replay_lineage_mode=root.replay_boundary_mode,
                forced_start_time=plan.effective_start_time,
            )
            for root, (root_key, affected, anchor) in zip(
                plan.replay_roots, scoped_roots, strict=True
            )
        ),
        objects=tuple(
            PopulationObject(logical_key=keys[index], physical_name=keys[index].name)
            for index in planned_indexes
        ),
    )


def _root_model_keys(roots: tuple[DirectReplayRoot, ...]) -> list[LogicalResourceKey]:
    keys: list[LogicalResourceKey] = []
    root: DirectReplayRoot
    for root in roots:
        keys.append(root.model_key)
        keys.extend(root.propagated_model_keys)
    return keys


def _native_resources(
    *, key: LogicalResourceKey, realized_project: RealizedProject
) -> list[tuple[str, str]]:
    resources: list[tuple[str, str]] = []
    resource: object
    for resource in realized_project.resources_by_logical_key[key]:
        if isinstance(resource, AdapterTable):
            resources.append((SOURCE_OBJECT_TABLE, resource.name))
        elif isinstance(resource, AdapterMaterializedView):
            resources.append((SOURCE_OBJECT_MATERIALIZED_VIEW, resource.name))
        elif isinstance(resource, AdapterView):
            resources.append((SOURCE_OBJECT_VIEW, resource.name))
        else:
            resources.append(_OTHER_RESOURCE)
    return resources


def _model_indexes(
    *, keys: tuple[LogicalResourceKey, ...], model_index: dict[LogicalResourceKey, int]
) -> list[int]:
    return [model_index[key] for key in keys]


def _keys_at(*, indexes: list[int], keys: tuple[ObjectKey, ...]) -> tuple[ObjectKey, ...]:
    return tuple(keys[index] for index in indexes)
