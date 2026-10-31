"""Desired-vs-actual diff helpers for planning."""

from __future__ import annotations

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.compiler.compile.models import (
    Column,
    DesiredState,
    KafkaSettings,
    KafkaTableSpec,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
    ViewSpec,
)
from sqltide.compiler.planner.exceptions import DeploymentPlanError
from sqltide.compiler.planner.models import ActualState, PlannedObjectChange
from sqltide.compiler.planner.types import (
    ActualObject,
    DesiredObject,
    NativeClassification,
    NativeComparison,
    NativeSpec,
    PlannedChangeType,
    TableSchemaChangeKind,
    TableSchemaSeedCompatibility,
)


def classify_object_changes(
    *,
    desired_state: DesiredState,
    actual_state: ActualState,
    full_refresh_keys: frozenset[ObjectKey] = frozenset(),
    start_time_keys: frozenset[ObjectKey] = frozenset(),
    start_time: str | None = None,
) -> tuple[PlannedObjectChange, ...]:
    """Classify conservative desired-vs-actual object changes."""

    actual_by_key: dict[ObjectKey, ActualObject] = {
        object_.key: object_ for object_ in actual_state.objects
    }
    forced_start_times: list[str | None] = [
        start_time if object_.key in start_time_keys else None for object_ in desired_state.objects
    ]
    classifications: list[NativeClassification] = _classify(
        comparisons=[
            (
                _native_spec(object_=object_),
                _native_actual_spec(object_=actual_by_key.get(object_.key)),
                object_.key in full_refresh_keys or forced_start_time is not None,
            )
            for object_, forced_start_time in zip(
                desired_state.objects, forced_start_times, strict=True
            )
        ]
    )
    return tuple(
        PlannedObjectChange(
            key=object_.key,
            change_type=PlannedChangeType(change_type),
            force_full_refresh=object_.key in full_refresh_keys,
            forced_start_time=forced_start_time,
            schema_change_kind=_schema_change_kind(text=schema_change_kind),
            seed_compatibility=_seed_compatibility(text=seed_compatibility),
        )
        for object_, forced_start_time, (
            change_type,
            schema_change_kind,
            seed_compatibility,
        ) in zip(desired_state.objects, forced_start_times, classifications, strict=True)
    )


def classify_object_change_type(
    *,
    desired_object: DesiredObject,
    actual_object: ActualObject,
    force_full_refresh: bool = False,
    forced_start_time: str | None = None,
) -> PlannedChangeType:
    """Classify a single desired-vs-actual object comparison."""

    return PlannedChangeType(
        _classify_one(
            desired_object=desired_object,
            actual_object=actual_object,
            forced=force_full_refresh or forced_start_time is not None,
        )[0]
    )


def classify_table_schema_change_kind(
    *, desired_object: DesiredObject, actual_object: ActualObject
) -> TableSchemaChangeKind | None:
    """Classify table-column compatibility for planner policy decisions."""

    return _schema_change_kind(
        text=_classify_one(desired_object=desired_object, actual_object=actual_object, forced=True)[
            1
        ]
    )


def classify_table_seed_compatibility(
    *, desired_object: DesiredObject, actual_object: ActualObject
) -> TableSchemaSeedCompatibility | None:
    """Classify whether table schema changes can safely seed from active data."""

    return _seed_compatibility(
        text=_classify_one(desired_object=desired_object, actual_object=actual_object, forced=True)[
            2
        ]
    )


def _classify_one(
    *, desired_object: DesiredObject, actual_object: ActualObject, forced: bool
) -> NativeClassification:
    return _classify(
        comparisons=[
            (_native_spec(object_=desired_object), _native_spec(object_=actual_object), forced)
        ]
    )[0]


def _classify(*, comparisons: list[NativeComparison]) -> list[NativeClassification]:
    try:
        return _native.classify_object_changes(comparisons)
    except ValueError as error:
        raise DeploymentPlanError(str(error)) from None


def _schema_change_kind(*, text: str | None) -> TableSchemaChangeKind | None:
    return None if text is None else TableSchemaChangeKind(text)


def _seed_compatibility(*, text: str | None) -> TableSchemaSeedCompatibility | None:
    return None if text is None else TableSchemaSeedCompatibility(text)


def _native_actual_spec(*, object_: ActualObject | None) -> NativeSpec | None:
    return None if object_ is None else _native_spec(object_=object_)


def _native_spec(*, object_: DesiredObject | ActualObject) -> NativeSpec:
    spec: TableSpec | KafkaTableSpec | MaterializedViewSpec | ViewSpec = object_.spec
    if isinstance(spec, TableSpec):
        storage: TableStorage = spec.storage
        return (
            "table",
            _native_columns(columns=spec.columns),
            [storage.engine, storage.partition_by, storage.ttl],
            list(storage.order_by),
            _native_settings(settings=storage.settings),
        )
    if isinstance(spec, KafkaTableSpec):
        kafka: KafkaSettings = spec.kafka
        return (
            "kafka_table",
            _native_columns(columns=spec.columns),
            [kafka.broker_list, kafka.topic, kafka.consumer_group, kafka.format],
            [],
            _native_settings(settings=kafka.settings),
        )
    if isinstance(spec, MaterializedViewSpec):
        return (
            "materialized_view",
            [],
            [spec.source_table_name, spec.target_table_name, spec.query],
            [],
            None,
        )
    return ("view", [], [spec.query], [], None)


def _native_columns(*, columns: tuple[Column, ...]) -> list[tuple[str, str, str | None]]:
    return [(column.name, column.type, column.default) for column in columns]


def _native_settings(*, settings: Mapping[str, str] | None) -> list[tuple[str, str]] | None:
    return None if settings is None else list(settings.items())
