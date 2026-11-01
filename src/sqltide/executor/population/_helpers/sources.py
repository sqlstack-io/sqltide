"""Create passive managed sources and activate landing after downstream realization."""

import sqltide._native as _native
from sqltide.adapter.models import AdapterManagedSource
from sqltide.compiler.compile.constants import RAW_TABLE_NAME_PREFIX
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
)
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.executor.population.constants import (
    SOURCE_ACTION_LAND,
    SOURCE_ACTION_PRESERVE,
    SOURCE_ACTION_SKIP,
    SOURCE_OBJECT_KAFKA,
    SOURCE_OBJECT_MATERIALIZED_VIEW,
    SOURCE_OBJECT_TABLE,
    SOURCE_OBJECT_VIEW,
)
from sqltide.executor.population.models import (
    PopulationManagedSource,
    PopulationRealization,
    PopulationSourcePreparation,
)


def plan_population_sources(
    *,
    desired_state: DesiredState,
    default_database: str,
    existing_relation_names: frozenset[str],
) -> tuple[PopulationSourcePreparation, tuple[PopulationRealization, ...]]:
    """Plan passive managed sources and deferred landing attachment without mutation."""

    actions: list[str] = _native.population_source_actions(
        [_native_source_object(desired_object) for desired_object in desired_state.objects],
        RAW_TABLE_NAME_PREFIX,
        set(existing_relation_names),
    )
    preserved_names: list[str] = []
    created_names: list[str] = []
    landing_views: list[DesiredMaterializedView] = []
    managed_sources: list[PopulationManagedSource] = []
    realizations: list[PopulationRealization] = []
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    action: str
    for desired_object, action in zip(desired_state.objects, actions, strict=True):
        if action == SOURCE_ACTION_SKIP:
            continue
        if isinstance(desired_object, DesiredKafkaTable):
            managed_resource: object = build_adapter_resource(desired_object)
            if isinstance(managed_resource, AdapterManagedSource):
                managed_sources.append(
                    PopulationManagedSource(
                        resource=managed_resource,
                        database=desired_object.key.database or default_database,
                    )
                )
        if action == SOURCE_ACTION_PRESERVE:
            preserved_names.append(desired_object.name)
            continue
        created_names.append(desired_object.name)
        if action == SOURCE_ACTION_LAND and isinstance(desired_object, DesiredMaterializedView):
            landing_views.append(desired_object)
            continue
        realizations.append(
            PopulationRealization(
                resource=build_adapter_resource(desired_object),
                database=desired_object.key.database or default_database,
            )
        )
    return (
        PopulationSourcePreparation(
            preserved_relation_names=tuple(preserved_names),
            created_relation_names=tuple(created_names),
            landing_views=tuple(landing_views),
            managed_sources=tuple(managed_sources),
        ),
        tuple(realizations),
    )


def _native_source_object(
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
) -> tuple[str, str, str | None]:
    if isinstance(desired_object, DesiredKafkaTable):
        return (SOURCE_OBJECT_KAFKA, desired_object.name, None)
    if isinstance(desired_object, DesiredTable):
        return (SOURCE_OBJECT_TABLE, desired_object.name, None)
    if isinstance(desired_object, DesiredMaterializedView):
        return (
            SOURCE_OBJECT_MATERIALIZED_VIEW,
            desired_object.name,
            desired_object.target_table_name,
        )
    return (SOURCE_OBJECT_VIEW, desired_object.name, None)
