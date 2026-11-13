from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import run_cli
from tests.integration.src.sqltide.cli._test_types import (
    VirtualDownstreamOnlyChangeIntegrationTestCase,
    VirtualStaleUpstreamPromotionIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
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
