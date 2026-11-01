from collections.abc import Sequence
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide.cli.entry.main.main import main
from tests.integration.src.sqltide.cli.playground.main._test_types import (
    PlaygroundLifecycleTestCase,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PlaygroundLifecycleTestCase(
            description="candidate stays isolated until unchanged consumer is promoted",
            expected_initial_rows=(
                (1, "completed", 2500),
                (2, "pending", 1800),
                (3, "completed", 4200),
            ),
            expected_candidate_rows=((1, "completed", 2500), (3, "completed", 4200)),
            expected_live_rows=(
                (1, "completed", 2500),
                (2, "pending", 1800),
                (3, "completed", 4200),
                (4, "completed", 3100),
                (5, "pending", 900),
            ),
            expected_promoted_rows=(
                (1, "completed", 2500),
                (3, "completed", 4200),
                (4, "completed", 3100),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_generated_playground_when_promoting_candidate_then_consumer_switches(
    test_case: PlaygroundLifecycleTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    project: Path = tmp_path / "playground"
    assert main(("sqt", "playground", str(project))) == 0
    arguments: tuple[str, ...] = (
        "--project-dir",
        str(project),
        "--host",
        clickhouse_connection_settings.host,
        "--port",
        str(clickhouse_connection_settings.port),
        "--username",
        clickhouse_connection_settings.username,
        "--password",
        clickhouse_connection_settings.password,
        "--database",
        clickhouse_database,
    )
    seed_statements: list[str] = (
        (project / "seed.sql")
        .read_text(encoding="utf-8")
        .replace("sqltide_playground.", f"{clickhouse_database}.")
        .strip()
        .rstrip(";")
        .split(";")
    )
    for statement in seed_statements:
        clickhouse_client.command(statement)
    consumer_query: str = (
        (project / "consumer.sql")
        .read_text(encoding="utf-8")
        .replace("sqltide_playground.", f"{clickhouse_database}.")
        .strip()
        .rstrip(";")
    )
    candidate_query: str = (
        (project / "candidate.sql")
        .read_text(encoding="utf-8")
        .replace("sqltide_playground.", f"{clickhouse_database}.")
        .strip()
        .rstrip(";")
    )
    initial_options: tuple[str, ...] = (
        "--select",
        "order_report",
        "--deployment-id",
        "20260101T100000Z_a",
        *arguments,
    )
    assert main(("sqt", "plan", *initial_options)) == 0
    assert main(("sqt", "build", "--auto-approve", *initial_options)) == 0
    assert clickhouse_client.command(f"EXISTS TABLE {clickhouse_database}.tbl__order_report") == 0
    assert main(("sqt", "deployment", "promote", "20260101T100000Z_a", *arguments)) == 0
    assert (
        tuple(clickhouse_client.query(consumer_query).result_rows)
        == test_case.expected_initial_rows
    )

    model: Path = project / "pipelines" / "pl__orders" / "order_report.sql"
    model.write_text(
        (project / "revisions" / "completed_orders.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    catalog_query: str = (
        "SELECT name, create_table_query FROM system.tables "
        f"WHERE database = '{clickhouse_database}' ORDER BY name"
    )
    catalog_before: tuple[Sequence[object], ...] = tuple(
        clickhouse_client.query(catalog_query).result_rows
    )
    candidate_options: tuple[str, ...] = (
        "--select",
        "order_report",
        "--full-refresh",
        "--deployment-id",
        "20260101T110000Z_b",
        *arguments,
    )
    assert main(("sqt", "plan", *candidate_options)) == 0
    assert tuple(clickhouse_client.query(catalog_query).result_rows) == catalog_before
    assert main(("sqt", "build", "--auto-approve", *candidate_options)) == 0
    assert main(("sqt", "deployment", "show", "20260101T110000Z_b", *arguments)) == 0
    assert main(("sqt", "deployment", "diff", "20260101T110000Z_b", *arguments)) == 0
    assert (
        tuple(clickhouse_client.query(consumer_query).result_rows)
        == test_case.expected_initial_rows
    )
    assert (
        tuple(clickhouse_client.query(candidate_query).result_rows)
        == test_case.expected_candidate_rows
    )

    clickhouse_client.command(
        (project / "live.sql")
        .read_text(encoding="utf-8")
        .replace("sqltide_playground.", f"{clickhouse_database}.")
    )
    assert (
        tuple(clickhouse_client.query(consumer_query).result_rows) == test_case.expected_live_rows
    )
    assert (
        tuple(clickhouse_client.query(candidate_query).result_rows)
        == test_case.expected_promoted_rows
    )
    assert main(("sqt", "deployment", "promote", "20260101T110000Z_b", *arguments)) == 0
    assert (
        tuple(clickhouse_client.query(consumer_query).result_rows)
        == test_case.expected_promoted_rows
    )
    assert (
        clickhouse_client.command(f"SELECT count() FROM {clickhouse_database}.playground_orders")
        == 5
    )
    assert (
        tuple(
            clickhouse_client.query(
                f"SELECT order_id, status, total_cents FROM "
                f"{clickhouse_database}.tbl__order_report__20260101T100000Z_a ORDER BY order_id"
            ).result_rows
        )
        == test_case.expected_live_rows
    )
