import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import cast

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from docker.models.containers import Container
from testcontainers.core.waiting_utils import wait_for
from testcontainers.postgres import PostgresContainer

import sqltide._native as _native
from sqltide._native import SqlModelAnalyzer, build_adapter_resource, compile_pipeline
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterConnectionConfig,
    AdapterInvocationRecord,
    AdapterMaterializedView,
    AdapterStableView,
    AdapterTable,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


def render_create_kafka_table_ddl(
    *, table: DesiredKafkaTable, database: str, if_not_exists: bool = False
) -> str:
    return ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(table),
        database=database,
        if_not_exists=if_not_exists,
    )


def render_create_table_ddl(*, table: DesiredTable, database: str) -> str:
    return ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(table),
        database=database,
    )


def render_adapter_table_ddl(*, table: AdapterTable, database: str) -> str:
    return ClickHouseAdapter().render_resource(resource=table, database=database)


def render_create_materialized_view_ddl(
    *, materialized_view: DesiredMaterializedView, database: str
) -> str:
    return ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(materialized_view),
        database=database,
    )


def render_adapter_materialized_view_ddl(
    *, materialized_view: AdapterMaterializedView, database: str
) -> str:
    return ClickHouseAdapter().render_resource(resource=materialized_view, database=database)


def render_create_view_ddl(*, database: str, view_name: str, target_table_name: str) -> str:
    return ClickHouseAdapter().render_resource(
        resource=AdapterStableView(name=view_name, target_relation_name=target_table_name),
        database=database,
    )


def build_compiled_example_pipeline() -> CompiledPipeline:
    return compile_pipeline(
        loaded_pipeline=_native.load_pipeline_directory(
            Path("tests/fixtures/basic_project/pipelines/pl__orders")
        ),
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )


def build_raw_orders_row() -> tuple[object, ...]:
    return (
        "order-1-key",
        json.dumps(
            {
                "order_id": "order-1",
                "customer_id": "customer-7",
                "order_total": 42.5,
                "created_at": "2099-04-05 12:00:00.123",
                "updated_at": "2099-04-05 12:01:00.456",
            }
        ),
        "source.orders.created",
        0,
        1,
        "2099-04-05 12:00:00.123",
        0,
        1,
        "2099-04-05 12:00:00.123",
        ["trace-id"],
        ["order-1-trace"],
        "2099-04-05 12:00:00.789",
        "2099-04-05 12:00:00.789",
    )


def integer_rows(rows: Sequence[Sequence[object]]) -> tuple[tuple[int, ...], ...]:
    converted_rows: list[tuple[int, ...]] = []
    row: Sequence[object]
    for row in rows:
        converted_rows.append(tuple(int(str(value)) for value in row))
    return tuple(converted_rows)


def object_rows(rows: Sequence[Sequence[object]]) -> tuple[tuple[object, ...], ...]:
    converted_rows: list[tuple[object, ...]] = []
    row: Sequence[object]
    for row in rows:
        converted_rows.append(tuple(row))
    return tuple(converted_rows)


def build_invocation_record(
    *, invocation_id: str, project_identity: str, target_identity: str, completed_at: str
) -> AdapterInvocationRecord:
    return AdapterInvocationRecord(
        invocation_id=invocation_id,
        project_identity=project_identity,
        target_identity=target_identity,
        command="audit",
        mode=None,
        outcome="succeeded",
        exit_code=0,
        materialized_outcome=None,
        deployment_id=None,
        workflow_id=None,
        selected_node_count=1,
        started_at=completed_at,
        completed_at=completed_at,
        duration_ms=0,
        error_message=None,
        summary_json="{}",
        tool_version="1.2.3",
    )


def execute_rendered_statements(*, client: Client, statements: tuple[str, ...]) -> None:
    statement: str
    for statement in statements:
        _ = client.command(statement)


def run_metadata_migration(
    *,
    connection_settings: ClickHouseConnectionSettings,
    database: str,
    start_barrier: Barrier,
) -> None:
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=connection_settings.host,
            port=connection_settings.port,
            username=connection_settings.username,
            password=connection_settings.password,
            database=database,
        )
    )
    raw_client: Client = clickhouse_connect.get_client(
        host=connection_settings.host,
        port=connection_settings.port,
        username=connection_settings.username,
        password=connection_settings.password,
    )
    _ = start_barrier.wait()
    try:
        execute_rendered_statements(
            client=raw_client,
            statements=connection.render_migrate_metadata_state(database),
        )
    finally:
        connection.close()
        raw_client.close()


def connect_clickhouse(
    *, connection_settings: ClickHouseConnectionSettings, database: str
) -> AdapterConnection:
    return ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=connection_settings.host,
            port=connection_settings.port,
            username=connection_settings.username,
            password=connection_settings.password,
            database=database,
        )
    )


POSTGRES_IMAGE: str = "postgres:16-alpine"
POSTGRES_USER: str = "sqltide"
POSTGRES_PASSWORD: str = "sqltide"
POSTGRES_DATABASE: str = "inventory"
POSTGRES_SEED_SQL: str = (
    "CREATE TABLE course (course_key text primary key, source text, country_normalised text);"
    "INSERT INTO course VALUES "
    "('partner_orders::10', 'partner_orders', 'JPN'), ('web_orders::7', 'web_orders', 'GBR');"
)
REFRESH_WAIT_SECONDS: int = 60


@dataclass(frozen=True)
class PostgresConnectionSettings:
    """Connection identity for a Postgres reachable from sibling containers."""

    container_host: str
    container_port: int
    database: str
    user: str
    password: str
    table: str


@contextmanager
def start_postgres_container() -> Iterator[PostgresConnectionSettings]:
    """Run Postgres with one seeded table and yield its container-network identity."""

    container: PostgresContainer = PostgresContainer(
        POSTGRES_IMAGE,
        username=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DATABASE,
    )
    with container:
        container.exec(
            ["psql", "-U", POSTGRES_USER, "-d", POSTGRES_DATABASE, "-c", POSTGRES_SEED_SQL]
        )
        yield PostgresConnectionSettings(
            container_host=_container_ip(container=container),
            container_port=5432,
            database=POSTGRES_DATABASE,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            table="course",
        )


def _container_ip(*, container: PostgresContainer) -> str:
    wrapped: Container = container.get_wrapped_container()
    wrapped.reload()
    attributes: dict[str, object] = dict(wrapped.attrs or {})
    settings: dict[str, object] = cast(dict[str, object], attributes.get("NetworkSettings", {}))
    networks: dict[str, dict[str, object]] = cast(
        dict[str, dict[str, object]], settings.get("Networks", {})
    )
    addresses: list[str] = [str(network.get("IPAddress", "")) for network in networks.values()]
    return str(max(addresses, key=len))


def refreshed_rows(
    *, client: Client, database: str, table: str, expected_count: int
) -> Sequence[Sequence[object]]:
    """Wait for a scheduled refresh to land, then return the refreshed rows."""

    statement: str = (
        f"SELECT course_key, country_normalised FROM {database}.{table} ORDER BY course_key"
    )
    wait_for(lambda: len(client.query(statement).result_rows) >= expected_count)
    return client.query(statement).result_rows


def create_streambuild_metadata(
    *,
    connection: AdapterConnection,
    client: Client,
    database: str,
    extra_tables: tuple[str, ...],
) -> tuple[str, ...]:
    execute_rendered_statements(
        client=client, statements=connection.render_migrate_metadata_state(database)
    )
    metadata_tables: tuple[str, ...] = tuple(
        str(row[0])
        for row in client.query(
            f"SELECT name FROM system.tables WHERE database = '{database}'"
        ).result_rows
    )
    client.command(
        "RENAME TABLE "
        + ", ".join(
            f"{database}.{name} TO {database}._streambuild_{name.removeprefix('_sqltide_')}"
            for name in metadata_tables
        )
    )
    for extra_table in extra_tables:
        client.command(f"CREATE TABLE {database}.{extra_table} (guard UInt8) ENGINE = TinyLog")
    return metadata_tables


def metadata_table_state(
    *, client: Client, database: str, table_prefix: str, extra_tables: tuple[str, ...]
) -> tuple[frozenset[str], tuple[tuple[int, ...], ...]]:
    table_names: frozenset[str] = frozenset(
        str(row[0])
        for row in client.query(
            f"SELECT name FROM system.tables WHERE database = '{database}'"
        ).result_rows
    ) - frozenset(extra_tables)
    version_rows: Sequence[Sequence[object]] = client.query(
        f"SELECT version FROM {database}.{table_prefix}schema_versions"
    ).result_rows
    return table_names, integer_rows(version_rows)


def expected_metadata_table_state(
    *, metadata_tables: tuple[str, ...], table_prefix: str
) -> tuple[frozenset[str], tuple[tuple[int, ...], ...]]:
    return (
        frozenset(table_prefix + name.removeprefix("_sqltide_") for name in metadata_tables),
        ((8,),),
    )
