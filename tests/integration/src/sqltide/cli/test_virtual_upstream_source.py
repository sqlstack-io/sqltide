from pathlib import Path
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import build_deployment_detail_payload, run_cli
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.integration.src.sqltide.cli._test_types import (
    VirtualArrivalFreeUpstreamIntegrationTestCase,
    VirtualDownstreamOnlyChangeIntegrationTestCase,
    VirtualFailedReplayPromotionIntegrationTestCase,
    VirtualStaleUpstreamPromotionIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    write_arrival_free_report,
    write_arrival_free_totals,
    write_failing_report,
    write_upstream_source_models,
    write_upstream_source_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualDownstreamOnlyChangeIntegrationTestCase(
            description="downstream versions keep receiving rows across both promotions",
            expected_after_downstream_promotion=("order-large", "order-new-large"),
            expected_after_upstream_promotion=(
                "order-large",
                "order-new-large",
                "order-newest-large",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_downstream_only_change_when_promoted_then_live_rows_keep_arriving(
    test_case: VirtualDownstreamOnlyChangeIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    project: Path = tmp_path / "project"
    write_upstream_source_project(project, clickhouse_connection_settings, clickhouse_database)
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_input (event_partition Int32, "
        "event_offset Int64, event_time DateTime64(3), order_id String, amount_cents UInt64) "
        "ENGINE = MergeTree ORDER BY (event_partition, event_offset)"
    )

    def insert(offset: int, order_id: str, amount_cents: int) -> None:
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_input VALUES "
            f"(0, {offset}, now64(3), '{order_id}', {amount_cents})"
        )

    def build_and_promote() -> None:
        options: tuple[str, ...] = ("--project-dir", str(project))
        assert run_cli(("sqt", "build", "--auto-approve", *options)) == 0
        assert run_cli(("sqt", "deployment", "promote", "--latest", *options)) == 0

    def report() -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.tbl__order_report ORDER BY order_id"
            ).result_rows
        )

    insert(1, "order-small", 75)
    insert(2, "order-large", 300)
    build_and_promote()

    # Only the downstream model changes, so its new view reads the published upstream model.
    write_upstream_source_models(project, totals_minimum=0, report_minimum=100)
    build_and_promote()
    insert(3, "order-new-small", 50)
    insert(4, "order-new-large", 500)
    assert report() == test_case.expected_after_downstream_promotion

    # The upstream model gets a new version; the rows must still reach the stable report.
    write_upstream_source_models(project, totals_minimum=10, report_minimum=100)
    build_and_promote()
    insert(5, "order-newest-small", 60)
    insert(6, "order-newest-large", 700)
    assert report() == test_case.expected_after_upstream_promotion


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualStaleUpstreamPromotionIntegrationTestCase(
            description="a candidate built on a superseded upstream is refused until rebuilt",
            upstream_deployment_id="20991231T100000Z_upstream",
            downstream_deployment_id="20991231T110000Z_downstream",
            rebuilt_deployment_id="20991231T120000Z_rebuilt",
            expected_error_fragments=(
                "Deployment 20991231T110000Z_downstream reads an upstream version",
                "order_report reads tbl__order_totals__",
                "tbl__order_totals now reads tbl__order_totals__20991231T100000Z_upstream",
                "Rebuild the candidate with `sqt build --select order_report`, "
                "then promote that build",
            ),
            expected_after_rebuild=("order-large", "order-new-large"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_superseded_upstream_when_promoting_candidate_then_promotion_is_refused(
    test_case: VirtualStaleUpstreamPromotionIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project: Path = tmp_path / "project"
    options: tuple[str, ...] = ("--project-dir", str(project))
    write_upstream_source_project(project, clickhouse_connection_settings, clickhouse_database)
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_input (event_partition Int32, "
        "event_offset Int64, event_time DateTime64(3), order_id String, amount_cents UInt64) "
        "ENGINE = MergeTree ORDER BY (event_partition, event_offset)"
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.orders_input VALUES "
        "(0, 1, now64(3), 'order-small', 75), (0, 2, now64(3), 'order-large', 300)"
    )

    def build(*arguments: str) -> int:
        return run_cli(("sqt", "build", "--auto-approve", *arguments, *options))

    def promote(*arguments: str) -> int:
        return run_cli(("sqt", "deployment", "promote", *arguments, *options))

    def report_source() -> str:
        return clickhouse_client.query(
            "SELECT create_table_query FROM system.tables "
            f"WHERE database = '{clickhouse_database}' AND name = 'tbl__order_report'"
        ).result_rows[0][0]

    assert build() == 0
    assert promote("--latest") == 0
    # One branch changes the upstream model; another, built later, changes only the downstream
    # model, so its view reads the upstream table published at the time.
    write_upstream_source_models(project, totals_minimum=10, report_minimum=0)
    assert build("--deployment-id", test_case.upstream_deployment_id) == 0
    write_upstream_source_models(project, totals_minimum=0, report_minimum=100)
    assert build("--deployment-id", test_case.downstream_deployment_id) == 0
    assert promote(test_case.upstream_deployment_id) == 0
    published_report: str = report_source()
    capsys.readouterr()

    assert promote("--latest") == 1
    error: str = capsys.readouterr().err
    assert all(fragment in error for fragment in test_case.expected_error_fragments)
    assert report_source() == published_report

    assert build("--deployment-id", test_case.rebuilt_deployment_id) == 0
    assert promote(test_case.rebuilt_deployment_id) == 0
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.orders_input VALUES "
        "(0, 3, now64(3), 'order-new-large', 500)"
    )
    rows: tuple[str, ...] = tuple(
        row[0]
        for row in clickhouse_client.query(
            f"SELECT order_id FROM {clickhouse_database}.tbl__order_report ORDER BY order_id"
        ).result_rows
    )
    assert rows == test_case.expected_after_rebuild


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualArrivalFreeUpstreamIntegrationTestCase(
            description="downstream builds replay from the source past an upstream without arrival",
            expected_after_added_model=("order-large", "order-new-large"),
            expected_after_changed_model=("order-new-large", "order-newest-large"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_upstream_without_arrival_column_when_downstream_changes_then_build_and_promote(
    test_case: VirtualArrivalFreeUpstreamIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    project: Path = tmp_path / "project"
    options: tuple[str, ...] = ("--project-dir", str(project))
    write_upstream_source_project(project, clickhouse_connection_settings, clickhouse_database)
    write_arrival_free_totals(project)
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_input (event_partition Int32, "
        "event_offset Int64, event_time DateTime64(3), order_id String, amount_cents UInt64) "
        "ENGINE = MergeTree ORDER BY (event_partition, event_offset)"
    )

    def insert(offset: int, order_id: str, amount_cents: int) -> None:
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.orders_input VALUES "
            f"(0, {offset}, now64(3), '{order_id}', {amount_cents})"
        )

    def build_and_promote() -> None:
        assert run_cli(("sqt", "build", "--auto-approve", *options)) == 0
        assert run_cli(("sqt", "deployment", "promote", "--latest", *options)) == 0

    def report() -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.tbl__order_report ORDER BY order_id"
            ).result_rows
        )

    insert(1, "order-small", 75)
    insert(2, "order-large", 300)
    build_and_promote()

    # A new downstream model of a published upstream that does not output _replay_landed_at.
    write_arrival_free_report(project, minimum=100)
    build_and_promote()
    insert(3, "order-new-large", 500)
    assert report() == test_case.expected_after_added_model

    # Only the downstream model changes; its replay still starts at the source.
    write_arrival_free_report(project, minimum=400)
    build_and_promote()
    insert(4, "order-newest-large", 450)
    assert report() == test_case.expected_after_changed_model


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualFailedReplayPromotionIntegrationTestCase(
            description="a build that fails while filling its tables cannot be promoted",
            failed_deployment_id="20991231T100000Z_failed",
            expected_refusal=(
                "Deployment 20991231T100000Z_failed has not finished building (its build failed "
                "or is still running), so it cannot be promoted. Promote a build once it "
                "finishes, by the ID it prints."
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_build_failing_during_replay_when_promoting_then_promotion_is_refused(
    test_case: VirtualFailedReplayPromotionIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project: Path = tmp_path / "project"
    options: tuple[str, ...] = ("--project-dir", str(project))
    write_upstream_source_project(project, clickhouse_connection_settings, clickhouse_database)
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_input (event_partition Int32, "
        "event_offset Int64, event_time DateTime64(3), order_id String, amount_cents UInt64) "
        "ENGINE = MergeTree ORDER BY (event_partition, event_offset)"
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.orders_input VALUES "
        "(0, 1, now64(3), 'order-small', 75), (0, 2, now64(3), 'order-large', 300)"
    )

    def build(*arguments: str) -> int:
        return run_cli(("sqt", "build", "--auto-approve", *arguments, *options))

    def promote(*arguments: str) -> int:
        return run_cli(("sqt", "deployment", "promote", *arguments, *options))

    def published_report() -> tuple[str, tuple[str, ...]]:
        source: str = clickhouse_client.query(
            "SELECT create_table_query FROM system.tables "
            f"WHERE database = '{clickhouse_database}' AND name = 'tbl__order_report'"
        ).result_rows[0][0]
        rows: tuple[str, ...] = tuple(
            row[0]
            for row in clickhouse_client.query(
                f"SELECT order_id FROM {clickhouse_database}.tbl__order_report ORDER BY order_id"
            ).result_rows
        )
        return source, rows

    assert build() == 0
    assert promote("--latest") == 0
    before: tuple[str, tuple[str, ...]] = published_report()

    # The report's replay raises after the build has recorded its deployment.
    write_failing_report(project)
    assert build("--deployment-id", test_case.failed_deployment_id) == 1
    capsys.readouterr()

    for arguments in (("--latest",), (test_case.failed_deployment_id,)):
        assert promote(*arguments) == 1
        assert test_case.expected_refusal in capsys.readouterr().err
    assert published_report() == before

    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    try:
        detail: dict[str, object] | None = build_deployment_detail_payload(
            connection=connection,
            database=clickhouse_database,
            metadata_database=clickhouse_database,
            deployment_id=test_case.failed_deployment_id,
        )
    finally:
        connection.close()
    preview: dict[str, object] = cast(
        dict[str, object], cast(dict[str, object], detail)["promotionPreview"]
    )
    assert preview["blockedReason"] == test_case.expected_refusal
