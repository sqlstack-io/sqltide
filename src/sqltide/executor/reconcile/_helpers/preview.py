"""Build reconcile previews from desired and actual object state."""

from datetime import UTC, datetime

import sqltide._native as _native
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_KAFKA_TABLE,
    DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    KafkaTableSpec,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    ViewSpec,
)
from sqltide.compiler.planner.exceptions import DeploymentPlanError
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualState,
    ActualTable,
    ActualView,
    ObjectStateRecord,
)
from sqltide.executor.reconcile._helpers.persist import build_object_state_record
from sqltide.executor.reconcile.constants import RECONCILE_DEPLOYMENT_ID_PREFIX
from sqltide.executor.reconcile.models import ReconcilePreview, ReconcileRejectedTarget
from sqltide.executor.reconcile.types import (
    NativeObjectKey,
    NativeReconcileObject,
    NativeReconcileOutcome,
)


def build_reconcile_preview(
    *,
    metadata_database: str,
    desired_state: DesiredState,
    actual_state: ActualState,
    selected_model_keys: frozenset[ObjectKey],
) -> ReconcilePreview:
    """Build reconcile eligibility records and target rejections."""

    recorded_at: str = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    reconcile_id: str = (
        f"{RECONCILE_DEPLOYMENT_ID_PREFIX}{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    try:
        outcomes: list[NativeReconcileOutcome] = _native.classify_reconcile_targets(
            [_native_object(desired_object) for desired_object in desired_state.objects],
            [_native_object(actual_object) for actual_object in actual_state.objects],
            [_native_key(key) for key in selected_model_keys],
        )
    except ValueError as error:
        raise DeploymentPlanError(str(error)) from None
    eligible_records: list[ObjectStateRecord] = []
    rejected_targets: list[ReconcileRejectedTarget] = []
    for (database, object_type, name), target_name, eligible, reasons in outcomes:
        if eligible is None:
            rejected_targets.append(
                ReconcileRejectedTarget(
                    target_key=ObjectKey(database=database, object_type=object_type, name=name),
                    target_name=target_name,
                    reasons=tuple(reasons),
                )
            )
            continue
        eligible_records.extend(
            build_object_state_record(
                desired_object=desired_state.objects[position],
                reconcile_id=reconcile_id,
                recorded_at=recorded_at,
            )
            for position in eligible
            if position is not None
        )
    return ReconcilePreview(
        database=metadata_database,
        reconcile_id=reconcile_id,
        eligible_records=tuple(eligible_records),
        rejected_targets=tuple(rejected_targets),
    )


def _native_key(key: ObjectKey) -> NativeObjectKey:
    return (key.database, key.object_type, key.name)


def _native_object(
    object_: DesiredKafkaTable
    | DesiredTable
    | DesiredMaterializedView
    | DesiredView
    | ActualKafkaTable
    | ActualTable
    | ActualMaterializedView
    | ActualView,
) -> NativeReconcileObject:
    spec: TableSpec | KafkaTableSpec | MaterializedViewSpec | ViewSpec = object_.spec
    key: NativeObjectKey = _native_key(object_.key)
    if isinstance(spec, TableSpec):
        return (
            DESIRED_OBJECT_TYPE_TABLE,
            key,
            (
                spec.storage.engine,
                list(spec.storage.order_by),
                spec.storage.partition_by,
                [(column.name, column.type, column.default) for column in spec.columns],
            ),
            None,
        )
    if isinstance(spec, MaterializedViewSpec):
        return (
            DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
            key,
            None,
            (spec.source_table_name, spec.target_table_name),
        )
    if isinstance(spec, ViewSpec):
        return (DESIRED_OBJECT_TYPE_VIEW, key, None, None)
    return (DESIRED_OBJECT_TYPE_KAFKA_TABLE, key, None, None)
