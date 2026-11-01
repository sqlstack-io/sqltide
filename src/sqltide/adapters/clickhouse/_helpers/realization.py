"""Realize one logical source as ClickHouse resources."""

import sqltide._native as _native
from sqltide.adapter.exceptions import AdapterConfigurationError
from sqltide.adapter.models import (
    AdapterAdoptedSourceRealizationRequest,
    AdapterColumn,
    AdapterManagedSource,
    AdapterManagedSourceRealizationRequest,
    AdapterMaterializedView,
    AdapterSourceRealization,
    AdapterTable,
)

_REPLAY_PARTITION_COLUMN_NAME: str = "_replay_partition"
_REPLAY_OFFSET_COLUMN_NAME: str = "_replay_offset"


def realize_clickhouse_source(
    *,
    request: AdapterManagedSourceRealizationRequest | AdapterAdoptedSourceRealizationRequest,
) -> AdapterSourceRealization:
    """Realize one logical source using ClickHouse resource conventions."""

    if isinstance(request, AdapterAdoptedSourceRealizationRequest):
        return AdapterSourceRealization(relation_name=request.relation_name, resources=())
    kafka_name: str
    raw_name: str
    view_name: str
    consumer_group: str
    landing_query: str
    try:
        kafka_name, raw_name, view_name, consumer_group, landing_query = (
            _native.realize_clickhouse_managed_source(
                request.logical_name,
                request.source_kind,
                request.format,
                request.consumer_group,
                request.project_name,
                request.target_name,
                [key for key, _value in request.settings],
            )
        )
    except ValueError as error:
        raise AdapterConfigurationError(str(error)) from None
    return AdapterSourceRealization(
        relation_name=raw_name,
        resources=(
            AdapterManagedSource(
                source_kind=request.source_kind,
                name=kafka_name,
                columns=(AdapterColumn(name="message", type="String"),),
                broker_list=request.broker_list,
                topic=request.topic,
                consumer_group=consumer_group,
                format=request.format,
                settings=request.settings,
                naming_macro_fingerprint=request.naming_macro_fingerprint,
            ),
            AdapterTable(
                name=raw_name,
                columns=tuple(
                    AdapterColumn(name=name, type=column_type)
                    for name, column_type in _native.clickhouse_raw_landing_columns()
                ),
                engine="MergeTree()",
                order_by=(_REPLAY_PARTITION_COLUMN_NAME, _REPLAY_OFFSET_COLUMN_NAME),
                ttl=request.ttl,
            ),
            AdapterMaterializedView(
                name=view_name,
                source_relation_name=kafka_name,
                target_relation_name=raw_name,
                query=landing_query,
                database_template=landing_query,
            ),
        ),
    )
