"""SQL diff helpers for deployment planning."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.adapter.types import AdapterResourceRenderer
from sqltide.compiler.compile.constants import RAW_TABLE_NAME_PREFIX
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ObjectKey,
)
from sqltide.compiler.planner.constants import (
    PLANNED_CHANGE_TYPE_CREATE,
    PLANNED_CHANGE_TYPE_NO_OP,
)
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualState,
    ActualTable,
    ActualView,
    PlannedObjectChange,
    PlannedSqlDiff,
)
from sqltide.compiler.planner.types import PlannedChangeType


def build_planned_sql_diffs(
    *,
    desired_state: DesiredState,
    actual_state: ActualState,
    object_changes: tuple[PlannedObjectChange, ...],
    default_database: str,
    render_resource: AdapterResourceRenderer,
) -> tuple[PlannedSqlDiff, ...]:
    """Build unified SQL diffs for changed desired objects."""

    desired_by_key: dict[
        ObjectKey,
        DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
    ] = {object_.key: object_ for object_ in desired_state.objects}
    actual_by_key: dict[
        ObjectKey, ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView
    ] = {object_.key: object_ for object_ in actual_state.objects}
    sql_diffs: list[PlannedSqlDiff] = []
    object_change: PlannedObjectChange
    for object_change in object_changes:
        if object_change.change_type == PLANNED_CHANGE_TYPE_NO_OP:
            continue
        desired_object: (
            DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView | None
        ) = desired_by_key.get(object_change.key)
        if desired_object is None:
            continue
        desired_sql: str = _render_desired_object_sql(
            object_=desired_object,
            default_database=default_database,
            render_resource=render_resource,
        )
        actual_object: (
            ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView | None
        ) = actual_by_key.get(object_change.key)
        current_change_type: PlannedChangeType = PlannedChangeType(object_change.change_type)
        current_sql: str = (
            ""
            if current_change_type == PLANNED_CHANGE_TYPE_CREATE or actual_object is None
            else _render_actual_object_sql(
                object_=actual_object,
                default_database=default_database,
                render_resource=render_resource,
            )
        )
        sql_diffs.append(
            PlannedSqlDiff(
                key=object_change.key,
                object_type=_display_object_type(desired_object),
                name=desired_object.name,
                diff_lines=_build_unified_diff_lines(
                    current_sql=current_sql, desired_sql=desired_sql
                ),
            )
        )
    return tuple(sql_diffs)


def _render_desired_object_sql(
    *,
    object_: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
    default_database: str,
    render_resource: AdapterResourceRenderer,
) -> str:
    database: str = object_.key.database or default_database
    return render_resource(resource=build_adapter_resource(object_), database=database)


def _render_actual_object_sql(
    *,
    object_: ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView,
    default_database: str,
    render_resource: AdapterResourceRenderer,
) -> str:
    database: str = object_.key.database or default_database
    if isinstance(object_, ActualKafkaTable):
        desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView = (
            DesiredKafkaTable(key=object_.key, deps=(), spec=object_.spec)
        )
    elif isinstance(object_, ActualTable):
        desired_object = DesiredTable(key=object_.key, deps=(), spec=object_.spec)
    elif isinstance(object_, ActualMaterializedView):
        desired_object = DesiredMaterializedView(
            key=object_.key,
            deps=(),
            spec=object_.spec,
        )
    else:
        desired_object = DesiredView(key=object_.key, deps=(), spec=object_.spec)
    return render_resource(
        resource=build_adapter_resource(desired_object),
        database=database,
    )


def _display_object_type(
    object_: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
) -> str:
    if isinstance(object_, DesiredMaterializedView) and not object_.target_table_name.startswith(
        RAW_TABLE_NAME_PREFIX
    ):
        return "transform"
    if isinstance(object_, DesiredKafkaTable):
        return "kafka table"
    if isinstance(object_, DesiredTable):
        return "table"
    if isinstance(object_, DesiredMaterializedView):
        return "materialized view"
    return "view"


def _build_unified_diff_lines(*, current_sql: str, desired_sql: str) -> tuple[str, ...]:
    return tuple(_native.unified_diff(current_sql, desired_sql, "current", "desired", False))
