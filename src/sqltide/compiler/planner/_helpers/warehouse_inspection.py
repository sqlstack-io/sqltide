"""Project one immutable warehouse snapshot into planner assembly inputs."""

import sqltide._native as _native
from sqltide.adapter.models import (
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.compiler.compile.constants import (
    RAW_TABLE_NAME_PREFIX,
)
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ObjectKey,
    TableSpec,
)
from sqltide.compiler.planner._helpers.warehouse_catalog import (
    active_table_specs_from_catalog,
    existing_table_names,
)
from sqltide.compiler.planner.main.inspect_root_deployment_state import (
    inspect_root_deployment_state,
)
from sqltide.compiler.planner.models import (
    ActualStateInspection,
    ObjectStateRecord,
    PlanningWarehouseSnapshot,
    RootDeploymentInspection,
)
from sqltide.compiler.planner.types import RootDeploymentStateKind


def build_inspected_managed_table_state_from_catalog(
    *, catalog: CatalogSnapshot
) -> InspectedManagedTableState:
    database: str = catalog.identity.database
    bindings, candidates = _native.managed_catalog_relations(
        [
            (relation.name, relation.query_sql is not None, relation.stable_binding_name)
            for relation in catalog.relations
        ]
    )
    return InspectedManagedTableState(
        active_bindings=tuple(
            InspectedActiveTableBinding(
                database=database,
                logical_name=logical_name,
                physical_name=physical_name,
            )
            for logical_name, physical_name in bindings
        ),
        physical_candidates=tuple(
            InspectedPhysicalTableCandidate(
                database=database,
                logical_name=logical_name,
                physical_name=physical_name,
                object_type="view" if is_view else "table",
            )
            for logical_name, physical_name, is_view in candidates
        ),
    )


def load_actual_state_inspection(
    *,
    snapshot: PlanningWarehouseSnapshot,
    desired_state: DesiredState,
    database: str,
) -> ActualStateInspection:
    catalog: CatalogSnapshot = snapshot.catalog
    inspected_state: InspectedManagedTableState = build_inspected_managed_table_state_from_catalog(
        catalog=catalog
    )
    active_deployment_by_root: dict[ObjectKey, RootDeploymentInspection] = (
        _inspect_active_deployments(
            desired_state=desired_state,
            inspected_state=inspected_state,
        )
    )
    active_deployment_ids: tuple[str, ...] = _active_deployment_ids(active_deployment_by_root)
    object_state_by_deployment_and_key: dict[tuple[str, ObjectKey], ObjectStateRecord] = (
        _index_object_state_records(
            deployment_ids=active_deployment_ids,
            records=snapshot.object_state_records,
        )
    )
    transform_view_keys: tuple[ObjectKey, ...] = _transform_view_keys(desired_state)
    active_physical_names_by_logical_name: dict[str, str] = _index_active_physical_names(
        inspected_state
    )
    active_table_names: tuple[str, ...] = _active_table_names(
        desired_state=desired_state,
        active_deployment_by_root=active_deployment_by_root,
        active_physical_names_by_logical_name=active_physical_names_by_logical_name,
    )
    active_table_specs_by_name: dict[str, TableSpec] = active_table_specs_from_catalog(
        catalog=catalog,
        database=database,
        table_names=active_table_names,
    )
    return ActualStateInspection(
        existing_names=existing_table_names(catalog),
        active_deployment_by_root=active_deployment_by_root,
        object_state_by_deployment_and_key=object_state_by_deployment_and_key,
        latest_object_state_by_key=_latest_object_state_records_by_keys(
            keys=transform_view_keys,
            records=snapshot.object_state_records,
        ),
        active_physical_names_by_logical_name=active_physical_names_by_logical_name,
        active_table_specs_by_name=active_table_specs_by_name,
    )


def _inspect_active_deployments(
    *,
    desired_state: DesiredState,
    inspected_state: InspectedManagedTableState,
) -> dict[ObjectKey, RootDeploymentInspection]:
    active_deployment_by_root: dict[ObjectKey, RootDeploymentInspection] = {}
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    for desired_object in desired_state.objects:
        if not isinstance(desired_object, (DesiredTable, DesiredView)):
            continue
        if isinstance(desired_object, DesiredTable) and desired_object.name.startswith(
            RAW_TABLE_NAME_PREFIX
        ):
            continue
        active_deployment_by_root[desired_object.key] = inspect_root_deployment_state(
            inspected_state=inspected_state,
            root_key=desired_object.key,
        )
    return active_deployment_by_root


def _active_deployment_ids(
    active_deployment_by_root: dict[ObjectKey, RootDeploymentInspection],
) -> tuple[str, ...]:
    deployment_ids: set[str] = set()
    inspection: RootDeploymentInspection
    for inspection in active_deployment_by_root.values():
        if inspection.active_deployment_id is not None:
            deployment_ids.add(inspection.active_deployment_id)
    return tuple(sorted(deployment_ids))


def _index_object_state_records(
    *,
    deployment_ids: tuple[str, ...],
    records: tuple[ObjectStateRecord, ...],
) -> dict[tuple[str, ObjectKey], ObjectStateRecord]:
    records_by_deployment_and_key: dict[tuple[str, ObjectKey], ObjectStateRecord] = {}
    record: ObjectStateRecord
    for record in records:
        if record.deployment_id in deployment_ids:
            records_by_deployment_and_key[(record.deployment_id, record.key)] = record
    return records_by_deployment_and_key


def _latest_object_state_records_by_keys(
    *,
    keys: tuple[ObjectKey, ...],
    records: tuple[ObjectStateRecord, ...],
) -> dict[ObjectKey, ObjectStateRecord]:
    index_by_key: dict[ObjectKey, int] = {key: index for index, key in enumerate(keys)}
    positions: list[int] = _native.latest_object_state_positions(
        [
            (index_by_key.get(record.key), record.recorded_at, record.observation_id)
            for record in records
        ]
    )
    return {records[position].key: records[position] for position in positions}


def _transform_view_keys(desired_state: DesiredState) -> tuple[ObjectKey, ...]:
    model_table_names: frozenset[str] = frozenset(
        object_.name
        for object_ in desired_state.objects
        if isinstance(object_, DesiredTable) and not object_.name.startswith(RAW_TABLE_NAME_PREFIX)
    )
    keys: list[ObjectKey] = []
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    for desired_object in desired_state.objects:
        if not isinstance(desired_object, DesiredMaterializedView):
            continue
        if desired_object.target_table_name in model_table_names:
            keys.append(desired_object.key)
    return tuple(keys)


def _index_active_physical_names(
    inspected_state: InspectedManagedTableState,
) -> dict[str, str]:
    return {
        binding.logical_name: binding.physical_name for binding in inspected_state.active_bindings
    }


def _active_table_names(
    *,
    desired_state: DesiredState,
    active_deployment_by_root: dict[ObjectKey, RootDeploymentInspection],
    active_physical_names_by_logical_name: dict[str, str],
) -> tuple[str, ...]:
    active_table_names: set[str] = set()
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    for desired_object in desired_state.objects:
        if not isinstance(desired_object, DesiredTable):
            continue
        if desired_object.name.startswith(RAW_TABLE_NAME_PREFIX):
            continue
        root_inspection: RootDeploymentInspection = active_deployment_by_root[desired_object.key]
        if root_inspection.state_kind != RootDeploymentStateKind.ACTIVE_VIEW_PRESENT:
            continue
        active_table_names.add(active_physical_names_by_logical_name[desired_object.name])
    return tuple(sorted(active_table_names))
