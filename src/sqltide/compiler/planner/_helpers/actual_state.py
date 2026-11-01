"""Assemble actual objects from desired objects and inspected state."""

import sqltide._native as _native
from sqltide.adapter.constants import VIRTUAL_OBJECT_STATE_KIND_RECONCILE
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    KafkaTableSpec,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
    ViewSpec,
)
from sqltide.compiler.planner.constants import (
    ACTUAL_ACTIVE_TABLE_SOURCE,
    ACTUAL_PHYSICAL_NAME_LOOKUP,
    DESIRED_KAFKA_TABLE_KIND,
    DESIRED_MATERIALIZED_VIEW_KIND,
    DESIRED_TABLE_KIND,
    DESIRED_VIEW_KIND,
)
from sqltide.compiler.planner.exceptions import ActualStateError
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualStateInspection,
    ActualTable,
    ActualView,
    ObjectStateRecord,
)
from sqltide.compiler.planner.types import ActualObject, DesiredObject, RootDeploymentStateKind


def build_inspected_actual_objects(
    *,
    desired_state: DesiredState,
    inspection: ActualStateInspection,
) -> tuple[ActualObject, ...]:
    """Build actual objects corresponding to the desired object graph."""

    keys: list[ObjectKey] = []
    index_by_key: dict[ObjectKey, int] = {}
    desired_object: DesiredObject
    for desired_object in desired_state.objects:
        index_by_key.setdefault(desired_object.key, len(keys))
        if index_by_key[desired_object.key] == len(keys):
            keys.append(desired_object.key)
    try:
        sources: list[tuple[str, str | None] | None] = _native.actual_state_sources(
            [
                _object_facts(desired=item, index_by_key=index_by_key)
                for item in desired_state.objects
            ],
            set(inspection.existing_names),
            {
                index_by_key[key]: (
                    root.state_kind == RootDeploymentStateKind.ACTIVE_VIEW_PRESENT,
                    root.active_deployment_id,
                )
                for key, root in inspection.active_deployment_by_root.items()
                if key in index_by_key
            },
            {
                (deployment_id, index_by_key[key]): _record_facts(record)
                for (deployment_id, key), record in (
                    inspection.object_state_by_deployment_and_key.items()
                )
                if key in index_by_key
            },
            {
                index_by_key[key]: _record_facts(record)
                for key, record in inspection.latest_object_state_by_key.items()
                if key in index_by_key
            },
            dict(inspection.active_physical_names_by_logical_name),
            set(inspection.active_table_specs_by_name),
        )
    except ValueError as error:
        _repeat_missing_lookup(lookup=error.args, keys=keys, inspection=inspection)
        raise ActualStateError(f"Actual-state lookup unexpectedly succeeded: {error}") from None
    actual_objects: list[ActualObject] = []
    source: tuple[str, str | None] | None
    for desired_object, source in zip(desired_state.objects, sources, strict=True):
        if source is not None:
            actual_objects.append(
                _actual_object(desired_object=desired_object, source=source, inspection=inspection)
            )
    return tuple(actual_objects)


def _repeat_missing_lookup(
    *, lookup: tuple[str, int | str], keys: list[ObjectKey], inspection: ActualStateInspection
) -> None:
    """Repeat the inspection lookup the native decision found missing, raising its KeyError."""

    kind, value = lookup
    if isinstance(value, int):
        inspection.active_deployment_by_root[keys[value]]
    elif kind == ACTUAL_PHYSICAL_NAME_LOOKUP:
        inspection.active_physical_names_by_logical_name[value]
    else:
        inspection.active_table_specs_by_name[value]


def _object_facts(
    *, desired: DesiredObject, index_by_key: dict[ObjectKey, int]
) -> tuple[int, str, str, str | None]:
    desired_object: DesiredObject = desired
    index: int = index_by_key[desired_object.key]
    if isinstance(desired_object, DesiredKafkaTable):
        return (index, desired_object.name, DESIRED_KAFKA_TABLE_KIND, None)
    if isinstance(desired_object, DesiredTable):
        return (index, desired_object.name, DESIRED_TABLE_KIND, None)
    if isinstance(desired_object, DesiredMaterializedView):
        return (
            index,
            desired_object.name,
            DESIRED_MATERIALIZED_VIEW_KIND,
            desired_object.target_table_name,
        )
    return (index, desired_object.name, DESIRED_VIEW_KIND, None)


def _record_facts(record: ObjectStateRecord) -> tuple[bool, str | None]:
    return (record.state_kind == VIRTUAL_OBJECT_STATE_KIND_RECONCILE, record.normalized_query)


def _actual_object(
    *,
    desired_object: DesiredObject,
    source: tuple[str, str | None],
    inspection: ActualStateInspection,
) -> ActualObject:
    kind, value = source
    if isinstance(desired_object, DesiredKafkaTable):
        return ActualKafkaTable(
            key=desired_object.key,
            spec=KafkaTableSpec(columns=desired_object.columns, kafka=desired_object.kafka),
        )
    if isinstance(desired_object, DesiredTable):
        if kind == ACTUAL_ACTIVE_TABLE_SOURCE and value is not None:
            return ActualTable(
                key=desired_object.key, spec=inspection.active_table_specs_by_name[value]
            )
        return ActualTable(
            key=desired_object.key,
            spec=TableSpec(
                columns=desired_object.columns,
                storage=TableStorage(
                    engine=desired_object.engine,
                    order_by=desired_object.order_by,
                    partition_by=desired_object.partition_by,
                    ttl=desired_object.ttl,
                    settings=desired_object.settings,
                ),
            ),
        )
    if isinstance(desired_object, DesiredMaterializedView):
        query: str = desired_object.query if value is None else value
        return ActualMaterializedView(
            key=desired_object.key,
            spec=MaterializedViewSpec(
                source_table_name=desired_object.source_table_name,
                target_table_name=desired_object.target_table_name,
                query=query,
                database_template=query,
            ),
        )
    view_query: str = str(value)
    return ActualView(
        key=desired_object.key,
        spec=ViewSpec(query=view_query, database_template=view_query),
    )
