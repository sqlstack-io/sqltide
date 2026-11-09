import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterWarehouseHealth
from sqltide.adapters.clickhouse.classes.warehouse_health_reader import (
    ClickHouseWarehouseHealthReader,
)
from tests.integration.src.sqltide.cli._test_types import (
    FanOutDedupeIntegrationTestCase,
    QuarantineFailModeIntegrationTestCase,
    QuarantineInPlaceIntegrationTestCase,
    QuarantineLiveConsumerIntegrationTestCase,
    QuarantineLiveIntegrationTestCase,
    QuarantineMetadataDatabaseIntegrationTestCase,
    QuarantineRepairIntegrationTestCase,
    QuarantineReplayIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    build_managed_clickhouse_client,
    direct_build_order_ids,
    insert_landing_rows,
    run_direct_build,
    run_direct_plan,
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
_MODEL_BODY: str = (
    "SELECT order_id::String AS order_id, amount::Int32 AS amount, "
    "_replay_partition::Int32 AS _replay_partition, _replay_offset::Int64 AS _replay_offset "
    'FROM __ref("orders")\n'
)
_QUARANTINE_MODEL: str = f'MODEL (on_row_error quarantine, order_by ["order_id"]);\n{_MODEL_BODY}'
_FAIL_MODEL: str = f'MODEL (order_by ["order_id"]);\n{_MODEL_BODY}'
_TS: str = "'2026-07-28 00:00:01.000'"
_DEDUPE_FRAGMENT: str = "LIMIT 1 BY"
_DROP_MODEL_VIEW: str = "DROP TABLE {database}.mv__orders_enriched SYNC"
_DROP_DEAD_LETTER_VIEW: str = "DROP TABLE {database}.mv__orders_enriched__dead_letters SYNC"
_RECREATE_FAIL_VIEW: str = "DROP TABLE {database}.mv__orders_enriched SYNC"


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineLiveIntegrationTestCase(
            description="a NULL into a non-Nullable column lands the good rows",
            values_sql=f"('order-1', '1', 0, 1, {_TS}), (NULL, '2', 0, 2, {_TS})",
            expected_order_ids=("order-1",),
            expected_failed_columns=(("order_id",),),
            expected_offsets=(2,),
        ),
        QuarantineLiveIntegrationTestCase(
            description="unconvertible values are recorded while wrapped values stay good",
            values_sql=(
                f"('order-1', '1', 0, 1, {_TS}), ('order-2', 'not-a-number', 0, 2, {_TS}), "
                f"('order-3', '99999999999', 0, 3, {_TS})"
            ),
            expected_order_ids=("order-1", "order-3"),
            expected_failed_columns=(("amount",),),
            expected_offsets=(2,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_quarantine_model_when_bad_rows_arrive_then_they_become_dead_letters(
    test_case: QuarantineLiveIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_QUARANTINE_MODEL
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        first_exit_code: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing VALUES {test_case.values_sql}"
        )
        order_ids: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        dead_letters: Sequence[Sequence[object]] = clickhouse_client.query(
            "SELECT model_name, failed_columns, kafka_offset "
            f"FROM {clickhouse_database}._sqltide_dead_letters ORDER BY kafka_offset"
        ).result_rows
    finally:
        connection.close()

    assert first_exit_code == 0
    assert order_ids == test_case.expected_order_ids
    assert {row[0] for row in dead_letters} == {"orders_enriched"}
    assert tuple(tuple(row[1]) for row in dead_letters) == test_case.expected_failed_columns
    assert tuple(row[2] for row in dead_letters) == test_case.expected_offsets


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineFailModeIntegrationTestCase(
            description="the default still fails the insert",
            values_sql=f"(NULL, '2', 0, 2, {_TS})",
            expected_error_fragment="non-Nullable",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_default_policy_when_a_bad_row_arrives_then_the_insert_errors(
    test_case: QuarantineFailModeIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL_MODEL
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        exit_code: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        with pytest.raises(DatabaseError, match=test_case.expected_error_fragment):
            clickhouse_client.command(
                f"INSERT INTO {clickhouse_database}.orders_existing VALUES {test_case.values_sql}"
            )
    finally:
        connection.close()

    assert exit_code == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineReplayIntegrationTestCase(
            description="adopted anchors are not deduplicated and bad rows are skipped",
            history_values_sql=(
                f"('order-1', '1', 0, 1, {_TS}), ('order-1', '1', 0, 1, {_TS}), "
                f"(NULL, '2', 0, 2, {_TS}), ('order-3', '3', 0, 3, {_TS})"
            ),
            expected_order_ids=("order-1", "order-1", "order-3"),
            expected_dead_letter_count=0,
            expected_dedupe=False,
        ),
        QuarantineReplayIntegrationTestCase(
            description="replay without repeated offsets stays undeduplicated",
            history_values_sql=(f"('order-1', '1', 0, 1, {_TS}), ('order-3', '3', 0, 2, {_TS})"),
            expected_order_ids=("order-1", "order-3"),
            expected_dead_letter_count=0,
            expected_dedupe=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_retained_history_when_building_quarantine_model_then_replay_uses_kept_rows(
    test_case: QuarantineReplayIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_QUARANTINE_MODEL
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
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
        exit_code: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            selectors=("orders_enriched",),
            start_time="2026-07-28T00:00:00Z",
        )
        order_ids: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        dead_letter_count: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM {clickhouse_database}._sqltide_dead_letters"
            ).result_rows[0][0]
        )
        replay_statements: str = "\n".join(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows
        )
        second_exit_code: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        order_ids_after_rerun: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
    finally:
        connection.close()

    assert (exit_code, second_exit_code) == (0, 0)
    assert order_ids == test_case.expected_order_ids
    assert order_ids_after_rerun == test_case.expected_order_ids
    assert dead_letter_count == test_case.expected_dead_letter_count
    assert (_DEDUPE_FRAGMENT in replay_statements) is test_case.expected_dedupe


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineInPlaceIntegrationTestCase(
            description="switching policy recreates views only and keeps target rows",
            history_values_sql=f"('order-1', '1', 0, 1, {_TS}), ('order-2', '2', 0, 2, {_TS})",
            bad_row_values_sql=f"(NULL, '3', 0, 3, {_TS})",
            good_row_values_sql=f"('order-4', '4', 0, 4, {_TS})",
            expected_after_enable=("order-1", "order-2", "order-4"),
            expected_failed_columns=(("order_id",),),
            expected_after_disable=("order-1", "order-2", "order-4"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_built_model_when_policy_changes_then_only_its_views_are_recreated(
    test_case: QuarantineInPlaceIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL_MODEL
    )
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    uuid_query: str = (
        f"SELECT uuid FROM system.tables WHERE database = '{clickhouse_database}' "
        "AND name = 'tbl__orders_enriched'"
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
        build_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            selectors=("orders_enriched",),
            start_time="2026-07-28T00:00:00Z",
        )
        table_uuid: object = clickhouse_client.query(uuid_query).result_rows[0][0]
        recorded_before: str = str(
            clickhouse_client.query(
                f"SELECT max(recorded_at) FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows[0][0]
        )
        capsys.readouterr()
        model_path.write_text(_QUARANTINE_MODEL, encoding="utf-8")
        plan_exit: int = run_direct_plan(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        enable_output: str = capsys.readouterr().out
        enable_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        enable_statements: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements "
                f"WHERE recorded_at > toDateTime64('{recorded_before}', 3)"
            ).result_rows
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing "
            f"VALUES {test_case.bad_row_values_sql}, {test_case.good_row_values_sql}"
        )
        after_enable: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        failed: tuple[tuple[str, ...], ...] = tuple(
            tuple(row[0])
            for row in clickhouse_client.query(
                f"SELECT failed_columns FROM {clickhouse_database}._sqltide_dead_letters"
            ).result_rows
        )
        uuid_after_enable: object = clickhouse_client.query(uuid_query).result_rows[0][0]
        model_path.write_text(_FAIL_MODEL, encoding="utf-8")
        disable_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        dead_letter_views: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM system.tables WHERE database = '{clickhouse_database}' "
                "AND name = 'mv__orders_enriched__dead_letters'"
            ).result_rows[0][0]
        )
        after_disable: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        uuid_after_disable: object = clickhouse_client.query(uuid_query).result_rows[0][0]
    finally:
        connection.close()

    assert (build_exit, plan_exit, enable_exit, disable_exit) == (0, 0, 0, 0)
    plan_payload: dict[str, object] = json.loads(enable_output)
    assert plan_payload["replay_roots"] == []
    assert "view_only" in enable_output
    assert enable_statements
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "TRUNCATE"))
        or f"DROP TABLE IF EXISTS {clickhouse_database}.tbl__orders_enriched" in statement
        for statement in enable_statements
    )
    assert table_uuid == uuid_after_enable == uuid_after_disable
    assert after_enable == test_case.expected_after_enable
    assert failed == test_case.expected_failed_columns
    assert after_disable == test_case.expected_after_disable
    assert dead_letter_views == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineLiveConsumerIntegrationTestCase(
            description="a managed source is paused and resumed around the view swap",
            history_rows=(("order-1", 0, 1),),
            late_rows=(("order-2", 0, 2),),
            expected_order_ids=("order-1", "order-2"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_managed_source_when_policy_changes_then_consumer_is_paused_and_resumed(
    test_case: QuarantineLiveConsumerIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        first_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=test_case.history_rows,
        )
        second_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        recorded_before: str = str(
            clickhouse_client.query(
                f"SELECT max(recorded_at) FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows[0][0]
        )
        model_path.write_text(
            model_path.read_text(encoding="utf-8").replace(
                "MODEL (", "MODEL (on_row_error quarantine, ", 1
            ),
            encoding="utf-8",
        )
        change_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        step_ids: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT step_id FROM {clickhouse_database}._sqltide_run_statements "
                f"WHERE recorded_at > toDateTime64('{recorded_before}', 3)"
            ).result_rows
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=test_case.late_rows,
        )
        order_ids: tuple[str, ...] = direct_build_order_ids(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
    finally:
        connection.close()

    assert (first_exit, second_exit, change_exit) == (0, 0, 0)
    assert any(step_id.startswith("pause_source_") for step_id in step_ids)
    assert any(step_id.startswith("activate_source_") for step_id in step_ids)
    assert not any(step_id.startswith("capture_replay") for step_id in step_ids)
    assert order_ids == test_case.expected_order_ids


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineMetadataDatabaseIntegrationTestCase(
            description="dead letters land in and are read from a separate metadata database",
            metadata_database_suffix="_metadata",
            values_sql=f"('order-1', '1', 0, 1, {_TS}), (NULL, '2', 0, 2, {_TS})",
            expected_failed_columns=(("order_id",),),
            expected_health_row_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_separate_metadata_database_when_rows_fail_then_dead_letters_live_there(
    test_case: QuarantineMetadataDatabaseIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    metadata_database: str = f"{clickhouse_database}{test_case.metadata_database_suffix}"
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_QUARANTINE_MODEL
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    clickhouse_client.command(f"CREATE DATABASE {metadata_database}")
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        exit_code: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            metadata_database=metadata_database,
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing VALUES {test_case.values_sql}"
        )
        failed: tuple[tuple[str, ...], ...] = tuple(
            tuple(row[0])
            for row in clickhouse_client.query(
                f"SELECT failed_columns FROM {metadata_database}._sqltide_dead_letters"
            ).result_rows
        )
        health: AdapterWarehouseHealth = ClickHouseWarehouseHealthReader(
            connection=connection
        ).read(database=clickhouse_database, include_dead_letters=True)
    finally:
        connection.close()
        clickhouse_client.command(f"DROP DATABASE IF EXISTS {metadata_database} SYNC")

    assert exit_code == 0
    assert failed == test_case.expected_failed_columns
    assert health.dead_letters is not None
    assert health.dead_letters[0].row_count == test_case.expected_health_row_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineRepairIntegrationTestCase(
            description="enable failed after creating the dead-letter view",
            desired_model=_QUARANTINE_MODEL,
            live_model=_FAIL_MODEL,
            break_statements=(_DROP_MODEL_VIEW,),
            expected_dead_letter_view_count=1,
        ),
        QuarantineRepairIntegrationTestCase(
            description="enable never created the dead-letter view",
            desired_model=_QUARANTINE_MODEL,
            live_model=_QUARANTINE_MODEL,
            break_statements=(_DROP_DEAD_LETTER_VIEW,),
            expected_dead_letter_view_count=1,
        ),
        QuarantineRepairIntegrationTestCase(
            description="disable left the dead-letter view behind",
            desired_model=_FAIL_MODEL,
            live_model=_QUARANTINE_MODEL,
            break_statements=(_RECREATE_FAIL_VIEW,),
            expected_dead_letter_view_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_half_applied_policy_switch_when_building_changed_then_it_repairs(
    test_case: QuarantineRepairIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=test_case.live_model
    )
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    views: str = (
        f"SELECT name FROM system.tables WHERE database = '{clickhouse_database}' "
        "AND name IN ('mv__orders_enriched', 'mv__orders_enriched__dead_letters') ORDER BY name"
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        first_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        for statement in test_case.break_statements:
            clickhouse_client.command(statement.format(database=clickhouse_database))
        model_path.write_text(test_case.desired_model, encoding="utf-8")
        repair_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            changed=True,
        )
        names: tuple[str, ...] = tuple(
            str(row[0]) for row in clickhouse_client.query(views).result_rows
        )
    finally:
        connection.close()

    assert (first_exit, repair_exit) == (0, 0)
    assert "mv__orders_enriched" in names
    assert len(names) == 1 + test_case.expected_dead_letter_view_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        FanOutDedupeIntegrationTestCase(
            description="raw anchor is read once per offset before arrayJoin fans out",
            expected_row_count=2,
            expected_dedupe=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_fan_out_model_on_raw_anchor_when_replaying_repeats_then_no_rows_are_lost(
    test_case: FanOutDedupeIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    model_path.write_text(
        model_path.read_text(encoding="utf-8").replace(
            "kafka_key::String AS order_id",
            "arrayJoin(['a', 'b'])::String AS order_id",
            1,
        ),
        encoding="utf-8",
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        first_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=(("order-1", 0, 1), ("order-1", 0, 1)),
        )
        second_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        row_count: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM {clickhouse_database}.tbl__orders_enriched"
            ).result_rows[0][0]
        )
        statements: str = " ".join(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows
        )
    finally:
        connection.close()

    assert (first_exit, second_exit) == (0, 0)
    assert row_count == test_case.expected_row_count
    assert ("LIMIT 1 BY" in statements) is test_case.expected_dedupe


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        FanOutDedupeIntegrationTestCase(
            description="a downstream model anchor keeps every fanned-out row",
            expected_row_count=4,
            expected_dedupe=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_upstream_model_anchor_when_replaying_then_fan_out_rows_are_kept(
    test_case: FanOutDedupeIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path)
    pipeline_root: Path = tmp_path / "pipelines" / "pl__orders"
    model_path: Path = pipeline_root / "orders_enriched.sql"
    model_path.write_text(
        model_path.read_text(encoding="utf-8").replace(
            "kafka_key::String AS order_id",
            "arrayJoin(['a', 'b'])::String AS order_id",
            1,
        ),
        encoding="utf-8",
    )
    (pipeline_root / "orders_child.sql").write_text(
        'MODEL (order_by ["order_id"]);\n'
        "SELECT order_id::String AS order_id, "
        "_replay_partition::Int32 AS _replay_partition, "
        '_replay_offset::Int64 AS _replay_offset FROM __ref("orders_enriched")\n',
        encoding="utf-8",
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        first_exit: int = run_direct_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=(("order-1", 0, 1), ("order-2", 0, 2)),
        )
        clickhouse_client.command(f"TRUNCATE TABLE {clickhouse_database}._sqltide_run_statements")
        second_exit: int = run_direct_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            selectors=("orders_child",),
        )
        row_count: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM {clickhouse_database}.tbl__orders_child"
            ).result_rows[0][0]
        )
        statements: str = " ".join(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows
        )
    finally:
        connection.close()

    assert (first_exit, second_exit) == (0, 0)
    assert row_count == test_case.expected_row_count
    assert ("LIMIT 1 BY" in statements) is test_case.expected_dedupe


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
