"""Real connection-free projects for SQL command contract tests."""

from pathlib import Path


def write_sql_project(*, root: Path, sql: str, config: str = "", header: str = "kind view") -> Path:
    """Write a cast-typed SQL view over one synthetic streaming source."""

    pipeline: Path = root / "pipelines" / "pl__orders"
    pipeline.mkdir(parents=True)
    sources: Path = root / "sources"
    sources.mkdir()
    (root / "sqltide_project.toml").write_text(
        'name = "orders_project"\ndefault_target = "test"\n'
        '[targets.test]\ndatabase = "analytics"\n' + config,
        encoding="utf-8",
    )
    (sources / "orders.yml").write_text(
        "sources:\n  - name: orders\n    kind: kafka\n"
        "    broker_list: kafka:9092\n    topic: source.orders\n"
        "    replay_boundary:\n      mode: offsets\n",
        encoding="utf-8",
    )
    (pipeline / "landing_orders.sql").write_text(
        'MODEL (engine "MergeTree()", order_by [order_id]);\n\n'
        'SELECT CAST(order_id AS UInt64) AS order_id FROM __source("orders")\n',
        encoding="utf-8",
    )
    model: Path = pipeline / "orders_view.sql"
    model.write_text(f"MODEL ({header});\n\n{sql}\n", encoding="utf-8")
    return model


def write_selector_rules_project(*, root: Path, config: str) -> Path:
    """Two models in one pipeline feed a view in another, all with unordered limits."""

    model: Path = write_sql_project(
        root=root,
        sql='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("landing_orders") LIMIT 1',
        header='engine "MergeTree()", order_by [order_id]',
        config=config,
    )
    model.with_name("orders_next.sql").write_text(
        'MODEL (engine "MergeTree()", order_by [order_id]);\n\n'
        'SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders_view") LIMIT 1\n'
    )
    reports: Path = root / "pipelines" / "pl__reports"
    reports.mkdir()
    (reports / "orders_report.sql").write_text(
        "MODEL (kind view);\n\n"
        'SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders_next") LIMIT 1\n'
    )
    return model
