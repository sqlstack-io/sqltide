import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import run_cli
from tests.integration.src.sqltide.cli.playground.main._test_types import (
    PlaygroundLifecycleTestCase,
)
from tests.integration.src.sqltide.cli.playground.main.helpers import query_rows
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
    capsys: pytest.CaptureFixture[str],
) -> None:
    project: Path = tmp_path / "playground"
    assert run_cli(("sqt", "playground", str(project), "--no-start")) == 0
    # Point the project's own connection at the test server: no connection flags are needed.
    config: Path = project / "sqltide_project.toml"
    settings: ClickHouseConnectionSettings = clickhouse_connection_settings
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('host = "127.0.0.1"', f'host = "{settings.host}"')
        .replace("port = 18124", f"port = {settings.port}")
        .replace('username = "playground"', f'username = "{settings.username}"')
        .replace('password = "sqltide_playground"', f'password = "{settings.password}"')
        .replace('database = "sqltide_playground"', f'database = "{clickhouse_database}"'),
        encoding="utf-8",
    )
    arguments: tuple[str, ...] = ("--project-dir", str(project))
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

    def query(file: str, *options: str) -> str:
        capsys.readouterr()
        assert (
            run_cli(("sqt", "playground", "query", str(project / file), *options, *arguments)) == 0
        )
        return capsys.readouterr().out

    assert run_cli(("sqt", "plan", *arguments)) == 0
    assert run_cli(("sqt", "build", "--auto-approve", *arguments)) == 0
    assert clickhouse_client.command(f"EXISTS TABLE {clickhouse_database}.tbl__order_report") == 0
    assert run_cli(("sqt", "deployment", "promote", "--latest", *arguments)) == 0
    assert query_rows(query("consumer.sql")) == test_case.expected_initial_rows
    capsys.readouterr()
    assert run_cli(("sqt", "deployment", "list", "--json", *arguments)) == 0
    (first_deployment,) = json.loads(capsys.readouterr().out)["deployments"]

    # A filter-only change plans and builds without --full-refresh.
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
    assert run_cli(("sqt", "plan", *arguments)) == 0
    assert tuple(clickhouse_client.query(catalog_query).result_rows) == catalog_before
    assert run_cli(("sqt", "build", "--auto-approve", *arguments)) == 0
    assert run_cli(("sqt", "deployment", "show", "--latest", *arguments)) == 0
    assert run_cli(("sqt", "deployment", "diff", "--latest", *arguments)) == 0
    assert query_rows(query("consumer.sql")) == test_case.expected_initial_rows
    assert query_rows(query("consumer.sql", "--candidate")) == test_case.expected_candidate_rows

    assert "INSERT: done" in query("live.sql")
    assert query_rows(query("consumer.sql")) == test_case.expected_live_rows
    assert query_rows(query("consumer.sql", "--candidate")) == test_case.expected_promoted_rows
    assert run_cli(("sqt", "deployment", "promote", "--latest", *arguments)) == 0
    assert query_rows(query("consumer.sql")) == test_case.expected_promoted_rows
    assert (
        clickhouse_client.command(f"SELECT count() FROM {clickhouse_database}.playground_orders")
        == 5
    )
    assert (
        tuple(
            clickhouse_client.query(
                f"SELECT order_id, status, total_cents FROM {clickhouse_database}."
                f"tbl__order_report__{first_deployment['deployment_id']} ORDER BY order_id"
            ).result_rows
        )
        == test_case.expected_live_rows
    )
