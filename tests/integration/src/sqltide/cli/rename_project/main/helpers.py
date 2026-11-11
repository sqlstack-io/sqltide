"""Build one managed Kafka project and run `sqt` against a real ClickHouse target."""

from pathlib import Path
from uuid import UUID

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import ControlStore, run_cli
from sqltide.adapter.models import AdapterManifestSnapshot
from tests.integration.src.sqltide.cli.helpers import build_managed_clickhouse_client
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings

PROJECT_CONFIG: str = (
    "# Shop analytics project.\n"
    'name = "shop"  # project identity\n'
    'default_target = "test"\n'
    "\n"
    "[defaults]\n"
    'pipeline_mode = "direct"\n'
    "\n"
    "[targets.test]\n"
    "# Overridden with --database in tests.\n"
    'database = "analytics"\n'
)
TWO_TARGET_PROJECT_CONFIG: str = (
    'name = "shop"\n'
    'default_target = "east"\n'
    "\n"
    "[defaults]\n"
    'pipeline_mode = "direct"\n'
    "\n"
    "[targets.east]\n"
    'database = "shop_east"\n'
    "\n"
    "[targets.west]\n"
    'database = "shop_west"\n'
)
SOURCE_CONFIG: str = (
    "sources:\n"
    "  - name: orders\n"
    "    kind: kafka\n"
    "    broker_list: kafka.example.com:9092\n"
    "    topic: shop.orders\n"
    "    replay_boundary:\n"
    "      mode: offsets\n"
)
MODEL_SQL: str = (
    "MODEL (\n"
    '  engine "MergeTree",\n'
    '  order_by ["order_id"],\n'
    ");\n"
    "\n"
    "SELECT\n"
    "  CAST(JSONExtractString(kafka_value, 'order_id') AS String) AS order_id,\n"
    "  CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at\n"
    'FROM __ref("orders")\n'
)


def write_shop_project(project_dir: Path, project_config: str = PROJECT_CONFIG) -> Path:
    """Write a direct-mode project with one managed Kafka source and one model."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "sqltide_project.toml").write_text(project_config, encoding="utf-8")
    (project_dir / "sources").mkdir()
    (project_dir / "sources" / "orders.yml").write_text(SOURCE_CONFIG, encoding="utf-8")
    pipeline_dir: Path = project_dir / "pipelines" / "pl__orders"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "orders_clean.sql").write_text(MODEL_SQL, encoding="utf-8")
    return project_dir


def write_project(
    project_dir: Path, *, name: str, source: str, consumer_group: str, database: str
) -> Path:
    """Write a direct-mode project whose one Kafka source declares its own consumer group."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "sqltide_project.toml").write_text(
        f'name = "{name}"\ndefault_target = "test"\n\n[defaults]\npipeline_mode = "direct"\n\n'
        f'[targets.test]\ndatabase = "{database}"\n',
        encoding="utf-8",
    )
    (project_dir / "sources").mkdir()
    (project_dir / "sources" / f"{source}.yml").write_text(
        SOURCE_CONFIG.replace("name: orders", f"name: {source}")
        .replace("shop.orders", f"{name}.{source}")
        .replace("    topic:", f"    consumer_group: {consumer_group}\n    topic:"),
        encoding="utf-8",
    )
    pipeline_dir: Path = project_dir / "pipelines" / f"pl__{source}"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / f"{source}_clean.sql").write_text(
        MODEL_SQL.replace('__ref("orders")', f'__ref("{source}")'), encoding="utf-8"
    )
    return project_dir


def connection_arguments(
    *, settings: ClickHouseConnectionSettings, project_dir: Path, database: str
) -> tuple[str, ...]:
    return (
        "--project-dir",
        str(project_dir),
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--username",
        settings.username,
        "--password",
        settings.password,
        "--database",
        database,
    )


def built_shop_project(
    *,
    tmp_path: Path,
    settings: ClickHouseConnectionSettings,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, tuple[str, ...]]:
    """A built shop project and the connection arguments every command takes."""
    monkeypatch.delenv("SQLTIDE_CONTROL_STORE_URL", raising=False)
    project_dir: Path = write_shop_project(tmp_path / "shop")
    arguments: tuple[str, ...] = connection_arguments(
        settings=settings, project_dir=project_dir, database=database
    )
    assert run_cli(("sqt", "build", "--auto-approve", *arguments)) == 0
    return project_dir, arguments


def plan_json(arguments: tuple[str, ...], capsys: pytest.CaptureFixture[str]) -> str:
    """The `sqt plan --json` output for the project."""
    _ = capsys.readouterr()
    assert run_cli(("sqt", "plan", "--json", *arguments)) == 0
    return capsys.readouterr().out


def target_arguments(
    *,
    settings: ClickHouseConnectionSettings,
    project_dir: Path,
    target: str,
    database: str,
) -> tuple[str, ...]:
    """Connection arguments for one target with its own database."""
    return (
        *connection_arguments(settings=settings, project_dir=project_dir, database=database),
        "--target",
        target,
    )


def destroy_history_ids(
    *, settings: ClickHouseConnectionSettings, database: str, identity: str, target: str
) -> tuple[str, ...]:
    """The manifest ids destroy planning reads for one project target."""
    snapshot: AdapterManifestSnapshot = build_managed_clickhouse_client(
        settings, database=database
    ).load_manifests(
        database=database,
        project_identity=identity,
        target_name=target,
        target_database=database,
    )
    return tuple(sorted(manifest.manifest_id for manifest in snapshot.manifests))


def role_names(store: ControlStore, *, user_id: UUID, project_name: str) -> tuple[str, ...]:
    return tuple(
        assignment.role_name
        for assignment in store.list_project_role_assignments(
            user_id=user_id, project_name=project_name
        )
    )


def manifest_ids(client: Client, *, database: str, identity: str) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in client.query(
            f"SELECT manifest_id FROM {database}._sqltide_manifests "
            "WHERE project_identity = {identity:String} ORDER BY manifest_id",
            parameters={"identity": identity},
        ).result_rows
    )


def kafka_group_name(client: Client, *, database: str) -> str:
    """The consumer group the live Kafka table for `orders` consumes as."""
    definition: str = str(
        client.query(
            "SELECT create_table_query FROM system.tables "
            "WHERE database = {database:String} AND name = 'kafka__orders'",
            parameters={"database": database},
        ).result_rows[0][0]
    )
    return definition.split("kafka_group_name = '", 1)[1].split("'", 1)[0]


def catalog_definitions(client: Client, *, database: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1]))
        for row in client.query(
            "SELECT name, create_table_query FROM system.tables "
            "WHERE database = {database:String} AND NOT startsWith(name, '_sqltide') "
            "ORDER BY name",
            parameters={"database": database},
        ).result_rows
    )
