import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterDeploymentRecord
from sqltide.executor.janitor.main.execute_janitor import execute_janitor
from sqltide.executor.janitor.models import JanitorRequest
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest
from tests.integration.src.sqltide.cli._test_types import (
    VirtualDedupeIntegrationTestCase,
    VirtualQuarantineIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    build_managed_clickhouse_client,
    insert_landing_rows,
    make_project_virtual,
    relation_names_like,
    run_direct_plan,
    run_new_virtual_environment_deployment,
    write_direct_adopted_source_project,
    write_direct_build_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings

_SOURCE_YML: str = (
    "sources:\n"
    "  - kind: stream_table\n"
    "    name: orders\n"
    "    table_name: orders_existing\n"
    "    replay_boundary:\n"
    "      mode: offsets\n"
    "      columns:\n"
    "        _replay_partition: event_partition\n"
    "        _replay_offset: event_offset\n"
    "        _replay_timestamp: event_timestamp\n"
)
_SOURCE_COLUMNS: str = (
    "order_id Nullable(String), amount String, event_partition Int32, event_offset Int64, "
    "event_timestamp DateTime64(3)"
)
_BODY: str = (
    "SELECT order_id::String AS order_id, amount::Int32 AS amount, "
    "_replay_partition::Int32 AS _replay_partition, _replay_offset::Int64 AS _replay_offset "
    'FROM __ref("orders")'
)
_TS: str = "'2026-07-28 00:00:01.000'"
_QUARANTINE: str = f'MODEL (on_row_error quarantine, order_by ["order_id"]);\n{_BODY}\n'
_FAIL: str = f'MODEL (order_by ["order_id"]);\n{_BODY}\n'
_REVISED: str = (
    'MODEL (on_row_error quarantine, order_by ["order_id"]);\n'
    "SELECT order_id::String AS order_id, amount::Int32 AS amount, "
    "toString(source.amount)::String AS amount_text, "
    "_replay_partition::Int32 AS _replay_partition, _replay_offset::Int64 AS _replay_offset "
    'FROM __ref("orders") AS source\n'
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualQuarantineIntegrationTestCase(
            description="deployments quarantine, survive promote and rollback, and clean up",
            history_values_sql=f"('order-1', '1', 0, 1, {_TS}), (NULL, '2', 0, 2, {_TS})",
            live_values_sql=f"('order-3', '3', 0, 3, {_TS}), (NULL, '4', 0, 4, {_TS})",
            expected_order_ids=("order-1", "order-3"),
            expected_failed_columns=(("order_id",),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_quarantine_when_deploying_then_lifecycle_keeps_dead_letters(
    test_case: VirtualQuarantineIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_QUARANTINE
    )
    make_project_virtual(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    dead_letters_sql: str = (
        f"SELECT model_name, failed_columns FROM {clickhouse_database}._sqltide_dead_letters"
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing "
            f"VALUES {test_case.history_values_sql}"
        )
        first: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=first.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        replay_dead_letters: int = len(clickhouse_client.query(dead_letters_sql).result_rows)
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing VALUES {test_case.live_values_sql}"
        )
        order_ids: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.tbl__orders_enriched ORDER BY order_id"
            ).result_rows
        )
        live_dead_letters: Sequence[Sequence[object]] = clickhouse_client.query(
            dead_letters_sql
        ).result_rows
        views_after_first: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )

        model_path.write_text(_FAIL, encoding="utf-8")
        plan_exit: int = run_direct_plan(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        model_path.write_text(_REVISED, encoding="utf-8")
        second: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=second.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=first.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
                operation="rollback",
                previous_deployment_id=second.deployment_id,
            ),
            client=connection,
        )
        after_rollback: int = len(clickhouse_client.query(dead_letters_sql).result_rows)
        views_after_rollback: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
        execute_janitor(
            request=JanitorRequest(
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                retention_days=0,
                apply=True,
                minimum_rollback_deployments=0,
            ),
            client=connection,
        )
        views_after_janitor: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
        after_janitor: int = len(clickhouse_client.query(dead_letters_sql).result_rows)
    finally:
        connection.close()

    assert replay_dead_letters == 0
    assert order_ids == test_case.expected_order_ids
    assert tuple(row[0] for row in live_dead_letters) == ("orders_enriched",)
    assert tuple(tuple(row[1]) for row in live_dead_letters) == test_case.expected_failed_columns
    assert views_after_first == (f"mv__orders_enriched__dead_letters__{first.deployment_id}",)
    assert plan_exit == 0
    assert len(views_after_rollback) == 2
    assert after_rollback >= len(live_dead_letters)
    assert after_janitor == after_rollback
    assert views_after_janitor == views_after_first


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualDedupeIntegrationTestCase(
            description="repeated raw offsets are read once",
            history_rows=(("order-1", 0, 1), ("order-1", 0, 1), ("order-2", 0, 2)),
            expected_order_ids=("order-1", "order-2"),
            expected_dedupe=True,
        ),
        VirtualDedupeIntegrationTestCase(
            description="no repeats keeps replay SQL unchanged",
            history_rows=(("order-1", 0, 1), ("order-2", 0, 2)),
            expected_order_ids=("order-1", "order-2"),
            expected_dedupe=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_replay_when_raw_has_repeats_then_dedupe_follows_them(
    test_case: VirtualDedupeIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path, virtual_environments=True)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=test_case.history_rows,
        )
        model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
        model_path.write_text(
            model_path.read_text(encoding="utf-8").replace(
                "_replay_partition::Int32", "(_replay_partition + 0)::Int32", 1
            ),
            encoding="utf-8",
        )
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        physical: str = deployment.prepared_object_mappings[0].physical_name
        order_ids: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.{physical} ORDER BY order_id"
            ).result_rows
        )
        statements: str = json.dumps(
            [
                str(row[0])
                for row in clickhouse_client.query(
                    f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements"
                ).result_rows
            ]
        )
    finally:
        connection.close()

    assert order_ids == test_case.expected_order_ids
    assert ("LIMIT 1 BY" in statements) is test_case.expected_dedupe


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
