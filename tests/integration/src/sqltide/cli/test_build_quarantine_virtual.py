import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterDeploymentRecord
from sqltide.executor.janitor.main.execute_janitor import execute_janitor
from sqltide.executor.janitor.models import JanitorRequest
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest
from tests.integration.src.sqltide.cli._test_types import (
    CancelledPausedSourceIntegrationTestCase,
    InPlaceDeadLetterCleanupIntegrationTestCase,
    MissingLandingRepairIntegrationTestCase,
    PolicySwitchFailureIntegrationTestCase,
    VirtualAdoptedChainIntegrationTestCase,
    VirtualDedupeIntegrationTestCase,
    VirtualPolicyRepairIntegrationTestCase,
    VirtualPolicySwitchIntegrationTestCase,
    VirtualQuarantineIntegrationTestCase,
    VirtualSelectedPolicyIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    break_row_error_policy_view,
    build_managed_clickhouse_client,
    insert_landing_rows,
    make_project_virtual,
    relation_names_like,
    run_direct_plan,
    run_new_virtual_environment_deployment,
    run_virtual_environment_build,
    view_is_quarantined,
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
_LANDING_VIEW_PATTERN: str = "mv\\_\\_orders"
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
    capsys: pytest.CaptureFixture[str],
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
        capsys.readouterr()
        plan_exit: int = run_direct_plan(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        policy_only_plan: dict[str, object] = json.loads(capsys.readouterr().out)
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
    assert policy_only_plan["rebuild_subtrees"] == []
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


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualPolicySwitchIntegrationTestCase(
            description="plain build switches policy on the live deployment only",
            bad_values_sql=f"(NULL, '2', 0, 2, {_TS}), ('order-3', '3', 0, 3, {_TS})",
            expected_order_ids=("order-1", "order-3"),
            expected_failed_columns=(("order_id",),),
            expected_deployment_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_model_when_policy_changes_then_views_switch_in_place(
    test_case: VirtualPolicySwitchIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL
    )
    make_project_virtual(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
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
            f"VALUES ('order-1', '1', 0, 1, {_TS})"
        )
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        physical: str = f"tbl__orders_enriched__{deployment.deployment_id}"
        uuid_query: str = (
            f"SELECT uuid FROM system.tables WHERE database = '{clickhouse_database}' "
            f"AND name = '{physical}'"
        )
        table_uuid: object = clickhouse_client.query(uuid_query).result_rows[0][0]
        model_path.write_text(_QUARANTINE, encoding="utf-8")
        capsys.readouterr()
        plan_exit: int = run_direct_plan(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        plan_output: str = capsys.readouterr().out
        enable_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing VALUES {test_case.bad_values_sql}"
        )
        order_ids: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.{physical} ORDER BY order_id"
            ).result_rows
        )
        failed: tuple[tuple[str, ...], ...] = tuple(
            tuple(row[0])
            for row in clickhouse_client.query(
                f"SELECT failed_columns FROM {clickhouse_database}._sqltide_dead_letters"
            ).result_rows
        )
        dead_letter_views_on: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
        uuid_after_enable: object = clickhouse_client.query(uuid_query).result_rows[0][0]
        model_path.write_text(_FAIL, encoding="utf-8")
        disable_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        dead_letter_views_off: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
        deployments: int = len(
            connection.load_deployment_inventory(clickhouse_database).deployments
        )
        uuid_after_disable: object = clickhouse_client.query(uuid_query).result_rows[0][0]
    finally:
        connection.close()

    payload: dict[str, object] = json.loads(plan_output)
    assert (plan_exit, enable_exit, disable_exit) == (0, 0, 0)
    assert payload["rebuild_subtrees"] == []
    assert "view_only" in plan_output
    assert order_ids == test_case.expected_order_ids
    assert failed == test_case.expected_failed_columns
    assert len(dead_letter_views_on) == 1
    assert dead_letter_views_off == ()
    assert table_uuid == uuid_after_enable == uuid_after_disable
    assert deployments == test_case.expected_deployment_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualPolicyRepairIntegrationTestCase(
            description="enable left the model view missing",
            desired_model=_QUARANTINE,
            live_model=_QUARANTINE,
            dropped_view_template="mv__orders_enriched__{deployment_id}",
            expected_dead_letter_view_count=1,
        ),
        VirtualPolicyRepairIntegrationTestCase(
            description="enable left the dead-letter view missing",
            desired_model=_QUARANTINE,
            live_model=_QUARANTINE,
            dropped_view_template="mv__orders_enriched__dead_letters__{deployment_id}",
            expected_dead_letter_view_count=1,
        ),
        VirtualPolicyRepairIntegrationTestCase(
            description="disable failed with the model view missing",
            desired_model=_FAIL,
            live_model=_QUARANTINE,
            dropped_view_template="mv__orders_enriched__{deployment_id}",
            expected_dead_letter_view_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_half_applied_virtual_switch_when_building_then_it_repairs(
    test_case: VirtualPolicyRepairIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=test_case.live_model
    )
    make_project_virtual(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        dropped: str = test_case.dropped_view_template.format(
            deployment_id=deployment.deployment_id
        )
        clickhouse_client.command(f"DROP TABLE {clickhouse_database}.{dropped} SYNC")
        model_path.write_text(test_case.desired_model, encoding="utf-8")
        repair_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        model_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern=f"mv\\_\\_orders\\_enriched\\_\\_{deployment.deployment_id}",
        )
        dead_letter_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
    finally:
        connection.close()

    assert repair_exit == 0
    assert len(model_views) == 1
    assert len(dead_letter_views) == test_case.expected_dead_letter_view_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualSelectedPolicyIntegrationTestCase(
            description="an unselected model with a pending change keeps its live view",
            selectors=("orders_enriched",),
            expected_selected_quarantined=True,
            expected_unselected_quarantined=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_selection_when_policy_changes_pending_then_only_selected_views_switch(
    test_case: VirtualSelectedPolicyIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL
    )
    make_project_virtual(project_root=tmp_path)
    pipeline_root: Path = tmp_path / "pipelines" / "pl__orders"
    (pipeline_root / "orders_other.sql").write_text(_FAIL, encoding="utf-8")
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        (pipeline_root / "orders_enriched.sql").write_text(_QUARANTINE, encoding="utf-8")
        (pipeline_root / "orders_other.sql").write_text(_QUARANTINE, encoding="utf-8")
        build_exit: int = run_virtual_environment_build(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            selectors=test_case.selectors,
        )
        selected: bool = view_is_quarantined(
            client=clickhouse_client,
            database=clickhouse_database,
            view_name=f"mv__orders_enriched__{deployment.deployment_id}",
        )
        unselected: bool = view_is_quarantined(
            client=clickhouse_client,
            database=clickhouse_database,
            view_name=f"mv__orders_other__{deployment.deployment_id}",
        )
    finally:
        connection.close()

    assert build_exit == 0
    assert selected is test_case.expected_selected_quarantined
    assert unselected is test_case.expected_unselected_quarantined


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PolicySwitchFailureIntegrationTestCase(
            description="enable fails creating the dead-letter view after the pause",
            live_header="MODEL (",
            desired_header="MODEL (on_row_error quarantine, ",
            broken_view="mv__orders_enriched__dead_letters",
            expected_live_quarantined_after_failure=False,
            expected_live_quarantined_after_rerun=True,
            expected_dead_letter_view_count_after_failure=0,
        ),
        PolicySwitchFailureIntegrationTestCase(
            description="enable fails between the drop and the recreate",
            live_header="MODEL (",
            desired_header="MODEL (on_row_error quarantine, ",
            broken_view="mv__orders_enriched",
            expected_live_quarantined_after_failure=False,
            expected_live_quarantined_after_rerun=True,
            expected_dead_letter_view_count_after_failure=0,
        ),
        PolicySwitchFailureIntegrationTestCase(
            description="disable fails between the drop and the recreate",
            live_header="MODEL (on_row_error quarantine, ",
            desired_header="MODEL (",
            broken_view="mv__orders_enriched",
            expected_live_quarantined_after_failure=True,
            expected_live_quarantined_after_rerun=False,
            expected_dead_letter_view_count_after_failure=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_switch_fails_when_building_then_old_view_keeps_consuming(
    test_case: PolicySwitchFailureIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path, virtual_environments=True)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    authored: str = model_path.read_text(encoding="utf-8")
    model_path.write_text(authored.replace("MODEL (", test_case.live_header, 1), encoding="utf-8")
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        model_view: str = f"mv__orders_enriched__{deployment.deployment_id}"
        model_path.write_text(
            authored.replace("MODEL (", test_case.desired_header, 1), encoding="utf-8"
        )
        break_row_error_policy_view(
            monkeypatch=monkeypatch,
            module_path="sqltide._native",
            broken_view=test_case.broken_view,
        )
        capsys.readouterr()
        failed_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        failure_output: str = capsys.readouterr().err
        monkeypatch.undo()
        quarantined_after_failure: bool = view_is_quarantined(
            client=clickhouse_client, database=clickhouse_database, view_name=model_view
        )
        landing_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client, database=clickhouse_database, pattern=_LANDING_VIEW_PATTERN
        )
        dead_letter_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched\\_\\_dead\\_letters%",
        )
        insert_landing_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            rows=(("order-late", 0, 9),),
        )
        order_ids: tuple[str, ...] = tuple(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}."
                f"tbl__orders_enriched__{deployment.deployment_id}"
            ).result_rows
        )
        rerun_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        quarantined_after_rerun: bool = view_is_quarantined(
            client=clickhouse_client, database=clickhouse_database, view_name=model_view
        )
    finally:
        connection.close()

    assert (failed_exit, rerun_exit) == (1, 0)
    assert "orders_enriched" in failure_output
    assert quarantined_after_failure is test_case.expected_live_quarantined_after_failure
    assert len(landing_views) == 1
    assert len(dead_letter_views) == test_case.expected_dead_letter_view_count_after_failure
    assert "order-late" in order_ids
    assert quarantined_after_rerun is test_case.expected_live_quarantined_after_rerun


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingLandingRepairIntegrationTestCase(
            description="a paused consumer on a quarantine model is resumed",
            model_header="MODEL (on_row_error quarantine, ",
            expected_landing_view_count=1,
            expected_operation="repair_landing_view",
        ),
        MissingLandingRepairIntegrationTestCase(
            description="a paused consumer on a default model is shown as a repair",
            model_header="MODEL (",
            expected_landing_view_count=1,
            expected_operation="repair_landing_view",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_missing_landing_view_when_building_virtual_then_it_is_repaired(
    test_case: MissingLandingRepairIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path, virtual_environments=True)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    model_path.write_text(
        model_path.read_text(encoding="utf-8").replace("MODEL (", test_case.model_header, 1),
        encoding="utf-8",
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        landing_view: str = relation_names_like(
            client=clickhouse_client, database=clickhouse_database, pattern=_LANDING_VIEW_PATTERN
        )[0]
        clickhouse_client.command(f"DROP TABLE {clickhouse_database}.{landing_view} SYNC")
        capsys.readouterr()
        plan_exit: int = run_direct_plan(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        plan_payload: dict[str, list[dict[str, object]]] = json.loads(capsys.readouterr().out)
        repair_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        landing_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client, database=clickhouse_database, pattern=_LANDING_VIEW_PATTERN
        )
    finally:
        connection.close()

    assert repair_exit == 0
    assert plan_exit == 0
    assert plan_payload["row_error_policy_changes"][0]["operation"] == test_case.expected_operation
    assert len(landing_views) == test_case.expected_landing_view_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        InPlaceDeadLetterCleanupIntegrationTestCase(
            description="a view created by a policy switch is dropped with its deployment",
            expected_retired_view_count=0,
            expected_active_view_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_in_place_dead_letter_view_when_deployment_retires_then_janitor_drops_it(
    test_case: InPlaceDeadLetterCleanupIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL
    )
    make_project_virtual(project_root=tmp_path)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
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
        model_path.write_text(_QUARANTINE, encoding="utf-8")
        switch_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        created: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern=f"mv\\_\\_orders\\_enriched\\_\\_dead\\_letters\\_\\_{first.deployment_id}",
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
        retired: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern=f"mv\\_\\_orders\\_enriched\\_\\_dead\\_letters\\_\\_{first.deployment_id}",
        )
        active: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern=f"mv\\_\\_orders\\_enriched\\_\\_dead\\_letters\\_\\_{second.deployment_id}",
        )
    finally:
        connection.close()

    assert switch_exit == 0
    assert len(created) == 1
    assert len(retired) == test_case.expected_retired_view_count
    assert len(active) == test_case.expected_active_view_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualAdoptedChainIntegrationTestCase(
            description="a healthy chain over an adopted source plans and touches nothing",
            expected_logical_view_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_healthy_chain_on_adopted_source_when_planning_then_no_view_is_touched(
    test_case: VirtualAdoptedChainIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_adopted_source_project(
        project_root=tmp_path, source_yml=_SOURCE_YML, model_sql=_FAIL
    )
    make_project_virtual(project_root=tmp_path)
    (tmp_path / "pipelines" / "pl__orders" / "orders_child.sql").write_text(
        'MODEL (order_by ["order_id"]);\n'
        "SELECT order_id::String AS order_id, _replay_partition::Int32 AS _replay_partition, "
        '_replay_offset::Int64 AS _replay_offset FROM __ref("orders_enriched")\n',
        encoding="utf-8",
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        clickhouse_client.command(
            f"CREATE TABLE {clickhouse_database}.orders_existing ({_SOURCE_COLUMNS}) "
            "ENGINE = MergeTree() ORDER BY event_offset"
        )
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        capsys.readouterr()
        plan_exit: int = run_direct_plan(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        plan_payload: dict[str, object] = json.loads(capsys.readouterr().out)
        build_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        logical_views: tuple[str, ...] = relation_names_like(
            client=clickhouse_client,
            database=clickhouse_database,
            pattern="mv\\_\\_orders\\_enriched",
        )
        statements: str = " ".join(
            str(row[0])
            for row in clickhouse_client.query(
                f"SELECT sql FROM {clickhouse_database}._sqltide_run_statements"
            ).result_rows
        )
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_existing "
            f"VALUES ('order-1', '1', 0, 1, {_TS})"
        )
    finally:
        connection.close()

    assert (plan_exit, build_exit) == (0, 0)
    assert "row_error_policy_changes" not in plan_payload
    assert len(logical_views) == test_case.expected_logical_view_count
    assert f"{clickhouse_database}.mv__orders_enriched TO" not in statements


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CancelledPausedSourceIntegrationTestCase(
            description="a cancelled switch names the command that resumes the source",
            expected_note_fragment="Run `sqt build` once the cause is fixed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cancelled_switch_when_restore_fails_then_note_names_the_command(
    test_case: CancelledPausedSourceIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_direct_build_project(project_root=tmp_path, virtual_environments=True)
    model_path: Path = tmp_path / "pipelines" / "pl__orders" / "orders_enriched.sql"
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )
    try:
        deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        model_path.write_text(
            model_path.read_text(encoding="utf-8").replace(
                "MODEL (", "MODEL (on_row_error quarantine, ", 1
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            connection, "render_restore_materialized_view", lambda _: "SELECT throwIf(1)"
        )
        monkeypatch.setattr(
            "sqltide.cli.build._helpers.virtual_command.execute_build_workflow",
            Mock(side_effect=KeyboardInterrupt),
        )
        capsys.readouterr()
        cancelled_exit: int = run_virtual_environment_build(
            project_root=tmp_path, database=clickhouse_database, connection=connection
        )
        errors: str = capsys.readouterr().err
    finally:
        connection.close()

    assert cancelled_exit == 130
    assert "stays paused" in errors
    assert test_case.expected_note_fragment in errors


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
