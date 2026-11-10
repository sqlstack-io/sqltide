from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.dev_server._helpers.queries.message_query import read_source_messages
from sqltide.dev_server.models import MessageQueryCursor, MessagesQueryRequest
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    build_compiled_example_pipeline,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.cli.helpers import write_audit_project_files
from tests.integration.src.sqltide.executor.backfill.helpers import require_managed_source
from tests.unit.src.sqltide.compiler.audit_discovery.helpers import write_sql_audit_file
from tests.unit.src.sqltide.compiler.discovery.helpers import write_project_toml


def write_scheduled_audit_project(
    *,
    project_dir: Path,
    database: str,
    severity: str = "warning",
    audit_query: str = (
        'SELECT order_id, line_total FROM __ref("order_items") '
        "WHERE sleep(0.05) = 0 AND line_total < 0"
    ),
) -> LoadedProject:
    write_audit_project_files(project_dir)
    write_project_toml(
        project_dir=project_dir,
        contents=f"""
        name = "scheduled_audit_integration"
        default_target = "test"

        [defaults.audits]
        severity = "warning"
        every = "1h"
        warmup = "0s"

        [targets.test]
        database = "{database}"

        [targets.test.audit_scheduler]
        enabled = true
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "singular" / "order_events" / "negative_line_totals.sql",
        f"""
        AUDIT (
          name "scheduled negative line totals",
          severity {severity},
        );

        {audit_query}
        """,
    )
    return cast(LoadedProject, _native.load_project_input_for_path(path=project_dir))


MESSAGE_CORPUS_RELATION_NAME: str = "raw__orders"
MESSAGE_CORPUS_LONG_VALUE: str = '{"message_type":"Order","pad":"' + "x" * 600 + '"}'
MESSAGE_CORPUS_SETTLEMENT_VALUE: str = (
    '{"message_type":"OrderCreated","data":{"source":"partner","item_count":12}}'
)
MESSAGE_CORPUS_COLUMN_NAMES: tuple[str, ...] = (
    "kafka_key",
    "kafka_value",
    "kafka_topic",
    "_replay_partition",
    "_replay_offset",
    "kafka_header_keys",
    "kafka_header_values",
    "kafka_landed_at",
    "_replay_landed_at",
)


def landed_at_seconds_ago(seconds: float) -> str:
    landed: datetime = datetime.now(tz=UTC) - timedelta(seconds=seconds)
    return landed.strftime("%Y-%m-%d %H:%M:%S.") + f"{landed.microsecond // 1000:03d}"


def create_message_corpus(*, clickhouse_client: Client, database: str) -> None:
    """Create the raw landing table and insert one deterministic browsing corpus."""

    compiled_pipeline: CompiledPipeline = build_compiled_example_pipeline()
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=database,
        )
    )
    rows: list[list[object]] = [
        [
            "OrderCreated",
            MESSAGE_CORPUS_SETTLEMENT_VALUE,
            "source.orders.created",
            0,
            1,
            ["trace-id"],
            ["t1"],
            landed_at_seconds_ago(7200),
            landed_at_seconds_ago(7200),
        ],
        [
            "OrderCreated",
            '{"message_type":"OrderCreated","data":{"source":"other","item_count":3}}',
            "source.orders.created",
            0,
            2,
            [],
            [],
            landed_at_seconds_ago(10),
            landed_at_seconds_ago(10),
        ],
        [
            "Cancel",
            '{"message_type":"Cancel","data":{"source":"partner"}}',
            "source.orders.created",
            1,
            1,
            ["trace-id", "trace-id"],
            ["t2", "t3"],
            landed_at_seconds_ago(20),
            landed_at_seconds_ago(20),
        ],
        [
            "Order",
            MESSAGE_CORPUS_LONG_VALUE,
            "source.orders.created",
            1,
            2,
            [],
            [],
            landed_at_seconds_ago(30),
            landed_at_seconds_ago(30),
        ],
        [
            "Ping",
            "{}",
            "source.orders.created",
            2,
            5,
            [],
            [],
            landed_at_seconds_ago(10800),
            landed_at_seconds_ago(10800),
        ],
    ]
    clickhouse_client.insert(
        table=f"{database}.{MESSAGE_CORPUS_RELATION_NAME}",
        data=rows,
        column_names=list(MESSAGE_CORPUS_COLUMN_NAMES),
    )


def create_pre_header_raw_table(*, clickhouse_client: Client, database: str) -> None:
    """Create a legacy raw table that predates header capture."""

    clickhouse_client.command(
        f"CREATE TABLE {database}.raw__legacy "
        "(kafka_key String, kafka_value String, _replay_partition Int32, "
        "_replay_offset Int64, kafka_headers String, _replay_landed_at DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY (_replay_partition, _replay_offset)"
    )


def fetch_first_message_page(
    *, connection: AdapterConnection, database: str, limit: int
) -> dict[str, object]:
    return read_source_messages(
        connection=connection,
        database=database,
        relation_name=MESSAGE_CORPUS_RELATION_NAME,
        request=MessagesQueryRequest(limit=limit),
    )


def fetch_message_page_after(
    *, connection: AdapterConnection, database: str, limit: int, cursor: dict[str, object]
) -> dict[str, object]:
    return read_source_messages(
        connection=connection,
        database=database,
        relation_name=MESSAGE_CORPUS_RELATION_NAME,
        request=MessagesQueryRequest(
            limit=limit,
            cursor=MessageQueryCursor(
                landedAt=str(cursor["landedAt"]),
                partition=int(str(cursor["partition"])),
                offset=int(str(cursor["offset"])),
            ),
        ),
    )


def page_coordinates(page: dict[str, object]) -> tuple[tuple[object, object], ...]:
    rows: list[dict[str, object]] = cast("list[dict[str, object]]", page["rows"])
    return tuple((row["partition"], row["offset"]) for row in rows)


def page_cursor(page: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", page["nextCursor"])
