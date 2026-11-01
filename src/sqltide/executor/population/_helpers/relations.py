"""Realize one population plan through explicit physical mappings."""

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterStableView,
    AdapterTable,
    AdapterView,
)
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ObjectKey,
)
from sqltide.compiler.planner.main.build_shadow_adapter_resource import (
    build_shadow_adapter_resource,
)
from sqltide.executor.population.exceptions import PopulationScopeError
from sqltide.executor.population.main._index_population_keys import index_population_keys
from sqltide.executor.population.models import PopulationPlan, PopulationRealization


def plan_population_objects(
    *, plan: PopulationPlan, desired_state: DesiredState, default_database: str
) -> tuple[PopulationRealization, ...]:
    """Plan all mapped tables and views once in dependency order without mutation."""

    object_by_key: dict[
        ObjectKey, DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    ] = {object_.key: object_ for object_ in desired_state.objects}
    physical_name_by_key: dict[ObjectKey, str] = {
        prepared.logical_key: prepared.physical_name for prepared in plan.objects
    }
    index_by_key: dict[ObjectKey, int] = index_population_keys(
        (*_object_keys(desired_state), *physical_name_by_key)
    )
    keys: tuple[ObjectKey, ...] = tuple(index_by_key)
    native_objects: list[tuple[int, list[int]]] = [
        (
            index_by_key[object_.key],
            _dependency_indexes(dependencies=object_.deps, index_by_key=index_by_key),
        )
        for object_ in desired_state.objects
    ]
    planned: set[int] = {index_by_key[key] for key in physical_name_by_key}
    ordered_indexes: list[int] = []
    try:
        ordered_indexes = _native.population_creation_order(native_objects, planned)
    except ValueError as error:
        raise PopulationScopeError.from_native(error=error, keys=keys) from None
    realizations: list[PopulationRealization] = []
    target_index: int
    for target_index in ordered_indexes:
        target_key: ObjectKey = keys[target_index]
        desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView = (
            object_by_key[target_key]
        )
        if isinstance(desired_object, DesiredKafkaTable):
            continue
        resource: (
            AdapterManagedSource
            | AdapterTable
            | AdapterMaterializedView
            | AdapterView
            | AdapterStableView
        ) = build_shadow_adapter_resource(
            desired_object=desired_object,
            physical_name=physical_name_by_key[target_key],
            physical_name_by_key=physical_name_by_key,
        )
        realizations.append(
            PopulationRealization(
                resource=resource,
                database=desired_object.key.database or default_database,
            )
        )
    return tuple(realizations)


def _object_keys(desired_state: DesiredState) -> list[ObjectKey]:
    keys: list[ObjectKey] = []
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    for desired_object in desired_state.objects:
        keys.append(desired_object.key)
        keys.extend(desired_object.deps)
    return keys


def _dependency_indexes(
    *, dependencies: tuple[ObjectKey, ...], index_by_key: dict[ObjectKey, int]
) -> list[int]:
    return [index_by_key[key] for key in dependencies]
