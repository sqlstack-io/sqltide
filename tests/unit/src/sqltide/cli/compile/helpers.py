from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from shutil import copytree

import sqltide._native as _native
from sqltide._native import build_compiler_adapter_profile, run_compile
from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterTable,
    AdapterView,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject


def copy_basic_project(*, project_dir: Path) -> None:
    _ = copytree(Path("tests/fixtures/basic_project"), project_dir)


def copy_streaming_playground(*, project_dir: Path) -> None:
    _ = copytree(Path("src/sqltide/playground_templates/streaming"), project_dir)


def write_typed_source_retention(*, project_dir: Path) -> None:
    (project_dir / "sqltide_project.toml").write_text(
        """name = "basic_project"
default_target = "test"

[defaults]
pipeline_mode = "virtual"

[defaults.sources.kafka.retention]
duration = "12h"
timestamp = "broker"
cap_at = "landed"

[targets.test]
database = "analytics"
""",
        encoding="utf-8",
    )


def write_adopted_source(*, project_dir: Path) -> None:
    (project_dir / "sources" / "orders.yml").write_text(
        """sources:
  - name: orders
    kind: stream_table
    table_name: existing_orders
    replay_boundary:
      mode: offsets
      columns:
        _replay_partition: event_partition
        _replay_offset: event_offset
        _replay_timestamp: event_timestamp
""",
        encoding="utf-8",
    )


def write_invalid_model(*, project_dir: Path) -> None:
    (project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql").write_text(
        """MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT *
FROM __ref("orders")
""",
        encoding="utf-8",
    )


def write_invalid_model_header(*, project_dir: Path) -> None:
    (project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql").write_text(
        "SELECT 1 AS order_id\n",
        encoding="utf-8",
    )


def write_invalid_reference_model(*, project_dir: Path) -> None:
    (project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql").write_text(
        """MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT CAST(1 AS UInt64) AS order_id FROM __ref("orders)
""",
        encoding="utf-8",
    )


def write_cross_pipeline_model_reference(*, project_dir: Path) -> None:
    project_path: Path = project_dir / "sqltide_project.toml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8")
        + '\n[dependencies]\nmodel_reference_scope = "pipeline"\n',
        encoding="utf-8",
    )
    pipeline_dir: Path = project_dir / "pipelines" / "pl__consumer"
    pipeline_dir.mkdir()
    (pipeline_dir / "beta.sql").write_text(
        """MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT order_id::UInt64 AS order_id FROM __ref("orders_enriched")
""",
        encoding="utf-8",
    )


def write_empty_project(*, project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pipelines").mkdir()
    (project_dir / "sqltide_project.toml").write_text(
        """name = "empty_project"
default_target = "test"

[targets.test]
database = "analytics"
""",
        encoding="utf-8",
    )


def write_view_project(*, project_dir: Path) -> None:
    pipeline_dir: Path = project_dir / "pipelines" / "pl__consumer"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "view_project"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (pipeline_dir / "customer_orders.sql").write_text(
        "MODEL (kind view, relation_name customer_orders);\nSELECT 1::UInt64 AS order_id\n",
        encoding="utf-8",
    )


def write_secret_source(*, project_dir: Path) -> None:
    (project_dir / "sources" / "orders.yml").write_text(
        """sources:
  - name: orders
    kind: kafka
    broker_list: "${ENV:BROKER_LIST}"
    topic: source.orders
    settings:
      kafka_sasl_password: "${ENV:KAFKA_SASL_PASSWORD}"
    replay_boundary:
      mode: offsets
""",
        encoding="utf-8",
    )


def write_artifact_leaf_model(*, project_dir: Path) -> None:
    (project_dir / "pipelines" / "pl__order_events" / "artifact_leaf.sql").write_text(
        """MODEL (
  engine "MergeTree()",
  order_by ["region_code"],
);

SELECT region_code::String AS region_code
FROM __ref("order_events")
""",
        encoding="utf-8",
    )


def compile_project(
    *,
    project_dir: Path,
    target_dir: Path,
    environment: Mapping[str, str] | None = None,
) -> int:
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(
        path=project_dir,
        environment=environment,
    )
    return run_compile(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        target_dir=target_dir,
    )


def compile_project_with_render_failure(*, project_dir: Path, target_dir: Path) -> int:
    profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    failing_profile: CompilerAdapterProfile = replace(
        profile,
        render_resource=_raise_render_failure,
    )
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(path=project_dir)
    return run_compile(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=failing_profile,
        target_dir=target_dir,
    )


def target_file_paths(*, target_dir: Path) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(target_dir).as_posix() for path in target_dir.glob("**/*.*"))
    )


def target_snapshot(*, target_dir: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        sorted(
            (path.relative_to(target_dir).as_posix(), path.read_bytes())
            for path in target_dir.glob("**/*.*")
        )
    )


def static_target_snapshot(*, target_dir: Path) -> tuple[tuple[str, bytes], ...]:
    static_paths: tuple[Path, ...] = (
        target_dir / "manifest.json",
        target_dir / "sqltide_dag.json",
        *tuple((target_dir / "compiled").glob("**/*.*")),
    )
    return tuple(
        sorted(
            (path.relative_to(target_dir).as_posix(), path.read_bytes()) for path in static_paths
        )
    )


def _raise_render_failure(
    *,
    resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
    database: str,
    if_not_exists: bool = False,
) -> str:
    raise RuntimeError(
        f"artifact render failure for {resource.name} in {database} ({if_not_exists})"
    )
