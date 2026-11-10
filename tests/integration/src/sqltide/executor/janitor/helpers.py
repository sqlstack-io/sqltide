from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from clickhouse_connect.driver.client import Client

from sqltide._native import execute_publish
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.executor.janitor.models import JanitorPreviewCandidate
from sqltide.executor.promotion.models import PublishRequest
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.executor.backfill.helpers import (
    build_raw_orders_row,
    build_scalar_replay_compiled_pipeline,
    build_scalar_replay_request,
    execute_backfill,
    require_managed_source,
)


@dataclass(frozen=True)
class JanitorIntegrationState:
    active_deployment_id: str
    recent_published_deployment_id: str
    old_published_deployment_id: str
    stale_unpublished_deployment_id: str
    failed_incomplete_deployment_id: str
    active_target_table_name: str
    recent_published_target_table_name: str
    old_published_target_table_name: str
    stale_unpublished_target_table_name: str
    failed_incomplete_target_table_name: str


def build_janitor_integration_state(
    *,
    clickhouse_client: Client,
    managed_client: AdapterConnection,
    database: str,
) -> JanitorIntegrationState:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table, database=database
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=database,
        )
    )
    clickhouse_client.insert(
        table=f"{database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 09:59:59.000",
                _replay_landed_at="2026-04-09 09:59:59.000",
            ),
            build_raw_orders_row(
                kafka_key="live-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 10:00:01.000",
                _replay_landed_at="2026-04-09 10:00:01.000",
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )

    old_published_deployment_id: str = "20260409T100000Z_old111"
    recent_published_deployment_id: str = "20260409T101000Z_recent1"
    active_deployment_id: str = "20260409T102000Z_active1"
    stale_unpublished_deployment_id: str = "20260409T103000Z_stale11"
    failed_incomplete_deployment_id: str = "20260409T104000Z_failed1"

    _execute_real_backfill(
        managed_client=managed_client,
        database=database,
        deployment_id=old_published_deployment_id,
        created_at="2026-04-09 10:00:00.123",
        boundary_time="2026-04-09 10:00:00.000",
    )
    _execute_real_backfill(
        managed_client=managed_client,
        database=database,
        deployment_id=recent_published_deployment_id,
        created_at="2026-04-09 10:10:00.123",
        boundary_time="2026-04-09 10:10:00.000",
    )
    _execute_real_backfill(
        managed_client=managed_client,
        database=database,
        deployment_id=active_deployment_id,
        created_at="2026-04-09 10:20:00.123",
        boundary_time="2026-04-09 10:20:00.000",
    )
    _execute_real_publish(
        managed_client=managed_client,
        database=database,
        deployment_id=active_deployment_id,
    )
    failed_incomplete_target_table_name: str = (
        f"tbl__orders_enriched__{failed_incomplete_deployment_id}"
    )
    clickhouse_client.command(
        f"CREATE TABLE {database}.{failed_incomplete_target_table_name} "
        "(order_id String) ENGINE = MergeTree ORDER BY order_id"
    )
    clickhouse_client.command(
        f"INSERT INTO {database}._sqltide_virtual_object_state "
        "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
        "logical_object_type, logical_object_name, physical_database_name, "
        "physical_relation_name, logical_model_database, logical_model_name, is_selected_root, "
        "object_fingerprint, canonical_query, observed_at) VALUES ("
        f"'{failed_incomplete_deployment_id}', 'failed-observation', 'deployment', "
        f"'{failed_incomplete_deployment_id}', '{database}', 'table', "
        f"'tbl__orders_enriched', '{database}', '{failed_incomplete_target_table_name}', NULL, "
        "'orders_enriched', true, 'failed-fingerprint', NULL, "
        "toDateTime64('2026-04-09 10:40:00.123', 3, 'UTC'))"
    )
    _execute_real_backfill(
        managed_client=managed_client,
        database=database,
        deployment_id=stale_unpublished_deployment_id,
        created_at="2026-04-09 10:30:00.123",
        boundary_time="2026-04-09 10:30:00.000",
    )
    _execute_real_publish(
        managed_client=managed_client,
        database=database,
        deployment_id=old_published_deployment_id,
    )
    _execute_real_publish(
        managed_client=managed_client,
        database=database,
        deployment_id=recent_published_deployment_id,
    )
    _execute_real_publish(
        managed_client=managed_client,
        database=database,
        deployment_id=active_deployment_id,
    )

    now: datetime = datetime.now(tz=UTC)
    _rewrite_publish_history(
        clickhouse_client=clickhouse_client,
        database=database,
        publish_rows=(
            (
                active_deployment_id,
                _format_clickhouse_time(now),
                ("tbl__orders_enriched",),
            ),
            (
                recent_published_deployment_id,
                _format_clickhouse_time(now - timedelta(days=1)),
                ("tbl__orders_enriched",),
            ),
            (
                old_published_deployment_id,
                _format_clickhouse_time(now - timedelta(days=30)),
                ("tbl__orders_enriched",),
            ),
        ),
    )

    return JanitorIntegrationState(
        active_deployment_id=active_deployment_id,
        recent_published_deployment_id=recent_published_deployment_id,
        old_published_deployment_id=old_published_deployment_id,
        stale_unpublished_deployment_id=stale_unpublished_deployment_id,
        failed_incomplete_deployment_id=failed_incomplete_deployment_id,
        active_target_table_name=f"tbl__orders_enriched__{active_deployment_id}",
        recent_published_target_table_name=(
            f"tbl__orders_enriched__{recent_published_deployment_id}"
        ),
        old_published_target_table_name=f"tbl__orders_enriched__{old_published_deployment_id}",
        stale_unpublished_target_table_name=(
            f"tbl__orders_enriched__{stale_unpublished_deployment_id}"
        ),
        failed_incomplete_target_table_name=failed_incomplete_target_table_name,
    )


def _execute_real_backfill(
    *,
    managed_client: AdapterConnection,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> None:
    execute_backfill(
        request=build_scalar_replay_request(
            database=database,
            deployment_id=deployment_id,
            created_at=created_at,
            boundary_time=boundary_time,
            replay_lineage_mode="timestamp",
        ),
        client=managed_client,
    )


def _execute_real_publish(
    *,
    managed_client: AdapterConnection,
    database: str,
    deployment_id: str,
) -> None:
    execute_publish(
        request=PublishRequest(
            deployment_id=deployment_id,
            metadata_database=database,
            default_database=database,
        ),
        client=managed_client,
    )


def _rewrite_publish_history(
    *,
    clickhouse_client: Client,
    database: str,
    publish_rows: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> None:
    clickhouse_client.command(f"TRUNCATE TABLE {database}._sqltide_virtual_publications")
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    deployment_id: str
    published_at: str
    logical_view_names: tuple[str, ...]
    for deployment_id, published_at, logical_view_names in publish_rows:
        logical_view_name: str
        for logical_view_name in logical_view_names:
            rows.append(
                (
                    f"{deployment_id}:{published_at}",
                    deployment_id,
                    database,
                    logical_view_name,
                    database,
                    f"{logical_view_name}__{deployment_id}",
                    published_at,
                )
            )
    clickhouse_client.insert(
        table=f"{database}._sqltide_virtual_publications",
        data=rows,
        column_names=[
            "publication_id",
            "deployment_id",
            "logical_database_name",
            "logical_view_name",
            "physical_database_name",
            "physical_relation_name",
            "published_at",
        ],
    )


def load_existing_table_names(*, clickhouse_client: Client, database: str) -> tuple[str, ...]:
    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT name FROM system.tables WHERE database = '{database}' ORDER BY name"
    ).result_rows
    return tuple(str(row[0]) for row in rows)


def group_candidate_deployment_ids(
    candidates: tuple[JanitorPreviewCandidate, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    deployment_ids_by_deletable: dict[bool, list[str]] = {True: [], False: []}
    candidate: JanitorPreviewCandidate
    for candidate in candidates:
        deployment_ids_by_deletable[candidate.deletable].append(candidate.deployment_id)
    return (
        tuple(deployment_ids_by_deletable[True]),
        tuple(deployment_ids_by_deletable[False]),
    )


def _format_clickhouse_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
