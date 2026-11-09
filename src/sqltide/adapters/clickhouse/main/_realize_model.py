"""Realize one logical model as ClickHouse resources."""

from sqltide.adapter.constants import METADATA_DEAD_LETTERS_TABLE_NAME
from sqltide.adapter.models import (
    AdapterMaterializedView,
    AdapterModelRealization,
    AdapterModelRealizationRequest,
    AdapterTable,
    AdapterView,
    AdapterViewRealizationRequest,
)
from sqltide.adapters.clickhouse.constants import CLICKHOUSE_MATERIALIZED_VIEW_NAME_PREFIX

_DEAD_LETTERS_VIEW_SUFFIX: str = "__dead_letters"


def realize_clickhouse_model(
    *, request: AdapterModelRealizationRequest | AdapterViewRealizationRequest
) -> AdapterModelRealization:
    """Realize one logical model as its ClickHouse resources."""

    if isinstance(request, AdapterViewRealizationRequest):
        return AdapterModelRealization(
            relation_name=request.target_relation_name,
            resources=(
                AdapterView(
                    name=request.target_relation_name,
                    query=request.resolved_query,
                    database_template=request.resolved_database_template,
                ),
            ),
        )

    view_name: str = f"{CLICKHOUSE_MATERIALIZED_VIEW_NAME_PREFIX}{request.logical_name}"
    dead_letter_views: tuple[AdapterMaterializedView, ...] = (
        ()
        if request.dead_letter_query is None or request.dead_letter_database_template is None
        else (
            AdapterMaterializedView(
                name=f"{view_name}{_DEAD_LETTERS_VIEW_SUFFIX}",
                source_relation_name=request.source_relation_name,
                target_relation_name=METADATA_DEAD_LETTERS_TABLE_NAME,
                query=request.dead_letter_query,
                database_template=request.dead_letter_database_template,
                dead_letters=True,
            ),
        )
    )
    return AdapterModelRealization(
        relation_name=request.target_relation_name,
        resources=(
            AdapterTable(
                name=request.target_relation_name,
                columns=request.columns,
                engine=request.engine,
                order_by=request.order_by,
                partition_by=request.partition_by,
                ttl=request.ttl,
                settings=request.settings,
            ),
            AdapterMaterializedView(
                name=view_name,
                source_relation_name=request.source_relation_name,
                target_relation_name=request.target_relation_name,
                query=request.resolved_query,
                database_template=request.resolved_database_template,
                refresh=request.refresh,
                append=request.append,
            ),
            *dead_letter_views,
        ),
    )
