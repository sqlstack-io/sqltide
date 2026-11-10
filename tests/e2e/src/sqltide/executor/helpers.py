import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree
from textwrap import dedent
from typing import Any, cast
from urllib.request import urlopen

from clickhouse_connect.driver.client import Client
from kafka import KafkaProducer

import sqltide._native as _native
from sqltide._native import SqlModelAnalyzer, compile_pipeline
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledSource,
    CompiledTableModel,
    DesiredState,
)
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    LoadedPipeline,
    ReplayOnChangePolicy,
    ReplayOnChangeRule,
)
from sqltide.compiler.discovery.types import ReplayLineageMode, ReplayOnChangeMode
from sqltide.compiler.sql_analysis.models import SqlModelAnalysis
from sqltide.dev_server._helpers.payloads.activity_payload import (
    build_query_views_activity_query,
)
from sqltide.executor.backfill.models import BackfillBootstrapRequest
from tests.e2e.src.sqltide.conftest import (
    E2EClickHouseConnectionSettings,
    E2EKafkaConnectionSettings,
)
from tests.e2e.src.sqltide.executor._test_types import (
    KafkaLiveShadowScenarioResult,
    KafkaLiveShadowWorkflowE2ETestCase,
    VirtualManagedManualWorkflowSnapshot,
)
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.compiler.planner.helpers import (
    build_changed_schema_variant_compiled_pipeline,
)
from tests.integration.src.sqltide.executor.backfill.helpers import (
    build_desired_state,
    build_offset_replay_compiled_pipeline,
    build_scalar_replay_compiled_pipeline,
    require_managed_source,
    require_model_resources,
)

E2E_KAFKA_TIMESTAMP_PROJECT_DIR: Path = Path("tests/fixtures/e2e_kafka_timestamp_project")
E2E_KAFKA_OFFSET_PROJECT_DIR: Path = Path("tests/fixtures/e2e_kafka_offset_project")
E2E_KAFKA_LANDED_AT_PROJECT_DIR: Path = Path("tests/fixtures/e2e_kafka_landed_at_project")
REPO_ROOT: Path = Path(__file__).resolve().parents[5]
_EXTERNAL_SOURCE_SETTINGS_BY_MODE: dict[bool, str] = {
    False: "",
    True: '\n[defaults]\npipeline_mode = "virtual"\n',
}


def build_greenfield_workflow_compiled_pipeline(
    *, kafka_broker_list: str, topic_suffix: str | None = None
) -> CompiledPipeline:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline(
        ReplayLineageMode.TIMESTAMP
    )
    return _with_kafka_broker_list_and_topic(
        compiled_pipeline=compiled_pipeline,
        kafka_broker_list=kafka_broker_list,
        topic_suffix=topic_suffix,
    )


def build_authored_greenfield_workflow_compiled_pipeline(*, project_dir: Path) -> CompiledPipeline:
    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(project_dir / "pipelines")
    assert not (len(loaded_pipelines) != 1), "Expected exactly one authored e2e pipeline fixture"
    return compile_pipeline(
        loaded_pipeline=loaded_pipelines[0],
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )


def prepare_authored_e2e_project(
    *,
    fixture_project_dir: Path,
    tmp_path: Path,
    kafka_broker_list: str,
    topic_suffix: str,
) -> Path:
    project_dir: Path = tmp_path / fixture_project_dir.name
    copytree(fixture_project_dir, project_dir)
    source_file: Path = project_dir / "sources" / "order_events.yml"
    source_contents: str = source_file.read_text(encoding="utf-8")
    source_contents = source_contents.replace(
        "broker_list: kafka:9092", f"broker_list: {kafka_broker_list}"
    )
    source_contents = source_contents.replace(
        "topic: source.order_events.created",
        (
            f"topic: source.order_events.created_{topic_suffix}\n"
            "    consumer_group: sqltide_order_events"
        ),
    )
    source_file.write_text(source_contents, encoding="utf-8")
    return project_dir


def run_kafka_live_shadow_scenario(
    *,
    test_case: KafkaLiveShadowWorkflowE2ETestCase,
    clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    kafka_connection_settings: E2EKafkaConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
) -> KafkaLiveShadowScenarioResult:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=E2E_KAFKA_TIMESTAMP_PROJECT_DIR,
        tmp_path=tmp_path,
        kafka_broker_list=kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )

    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=kafka_connection_settings.bootstrap_server
    )
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=tuple(
                (order_id, json.dumps({"order_id": order_id}))
                for order_id in test_case.initial_order_ids
            ),
        )
    finally:
        producer.close()

    wait_for_row_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=len(test_case.initial_order_ids),
    )
    run_sqltide_virtual_build_cli(
        project_dir=project_dir,
        host=clickhouse_connection_settings.host,
        port=clickhouse_connection_settings.port,
        username=clickhouse_connection_settings.username,
        password=clickhouse_connection_settings.password,
        database=clickhouse_database,
        deployment_id=test_case.deployment_id,
    )

    staged_table_name: str = f"{target_table_name}__{test_case.deployment_id}"
    wait_for_live_shadow_row_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        raw_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        staged_table_name=staged_table_name,
        expected_count=len(test_case.initial_order_ids),
    )

    producer = build_kafka_producer(bootstrap_server=kafka_connection_settings.bootstrap_server)
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=tuple(
                (order_id, json.dumps({"order_id": order_id}))
                for order_id in test_case.live_order_ids
            ),
        )
    finally:
        producer.close()

    total_order_count: int = len(test_case.initial_order_ids) + len(test_case.live_order_ids)
    wait_for_row_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=total_order_count,
    )
    wait_for_live_shadow_row_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        raw_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        staged_table_name=staged_table_name,
        expected_count=total_order_count,
    )
    staged_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {clickhouse_database}.{staged_table_name} ORDER BY order_id"
    ).result_rows

    run_sqltide_deployment_promote_cli(
        project_dir=project_dir,
        host=clickhouse_connection_settings.host,
        port=clickhouse_connection_settings.port,
        username=clickhouse_connection_settings.username,
        password=clickhouse_connection_settings.password,
        database=clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    final_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {clickhouse_database}.{target_table_name} ORDER BY order_id"
    ).result_rows
    return KafkaLiveShadowScenarioResult(
        staged_table_name=staged_table_name,
        staged_order_ids=tuple(str(row[0]) for row in staged_rows),
        deployment_id=test_case.deployment_id,
        final_rows=tuple(tuple(row) for row in final_rows),
    )


def prepare_external_source_e2e_project(*, tmp_path: Path) -> Path:
    project_dir: Path = tmp_path / "external_source_project"
    pipeline_dir: Path = project_dir / "pipelines" / "pl__orders"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    _write_external_source_project_config(project_dir=project_dir)
    _write_external_source(
        project_dir=project_dir,
        contents="""
sources:
  - kind: stream_table
    name: orders
    table_name: orders_existing
    replay_boundary:
      mode: timestamp
      columns:
        _replay_timestamp: event_timestamp
""",
    )
    (pipeline_dir / "orders_enriched.sql").write_text(
        """
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT
  CAST(order_id AS String) AS order_id,
  CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp
FROM __ref("orders")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_dir


def prepare_external_source_offset_e2e_project(
    *, tmp_path: Path, virtual_environments: bool = True
) -> Path:
    project_dir: Path = tmp_path / "external_source_offset_project"
    pipeline_dir: Path = project_dir / "pipelines" / "pl__orders"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    _write_external_source_project_config(
        project_dir=project_dir, virtual_environments=virtual_environments
    )
    _write_external_source(
        project_dir=project_dir,
        contents="""
sources:
  - kind: stream_table
    name: orders
    table_name: orders_existing
    replay_boundary:
      mode: offsets
      columns:
        _replay_partition: event_partition
        _replay_offset: event_offset
        _replay_timestamp: event_timestamp
""",
    )
    (pipeline_dir / "orders_enriched.sql").write_text(
        """
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT
  CAST(order_id AS String) AS order_id,
  CAST(_replay_partition AS Int32) AS _replay_partition,
  CAST(_replay_offset AS Int64) AS _replay_offset
FROM __ref("orders")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_dir


def prepare_external_source_cursor_e2e_project(*, tmp_path: Path) -> Path:
    project_dir: Path = tmp_path / "external_source_cursor_project"
    pipeline_dir: Path = project_dir / "pipelines" / "pl__orders"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    _write_external_source_project_config(project_dir=project_dir)
    _write_external_source(
        project_dir=project_dir,
        contents="""
sources:
  - kind: stream_table
    name: orders
    table_name: orders_existing
    replay_boundary:
      mode: cursor
      columns:
        _replay_cursor: event_cursor
        _replay_timestamp: event_timestamp
""",
    )
    (pipeline_dir / "orders_enriched.sql").write_text(
        """
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT
  CAST(order_id AS String) AS order_id,
  CAST(_replay_cursor AS UInt64) AS _replay_cursor
FROM __ref("orders")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_dir


def _write_external_source_project_config(
    *, project_dir: Path, virtual_environments: bool = True
) -> None:
    (project_dir / "sqltide_project.toml").write_text(
        'name = "external_source_project"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n'
        f"{_EXTERNAL_SOURCE_SETTINGS_BY_MODE[virtual_environments]}",
        encoding="utf-8",
    )


def _write_external_source(*, project_dir: Path, contents: str) -> None:
    source_dir: Path = project_dir / "sources"
    source_dir.mkdir()
    (source_dir / "orders.yml").write_text(
        dedent(contents).strip() + "\n",
        encoding="utf-8",
    )


def run_sqltide_virtual_build_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
    deployment_id: str,
) -> None:
    _run_sqltide_cli(
        command=(
            "build",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--deployment-id",
            deployment_id,
            "--auto-approve",
        )
    )


def run_sqltide_build_cli(
    *, project_dir: Path, host: str, port: int, username: str, password: str, database: str
) -> None:
    """Run the direct build command through the installed CLI surface."""

    _run_sqltide_cli(
        command=(
            "build",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--auto-approve",
        )
    )


def run_sqltide_deployment_audit_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
    deployment_id: str,
) -> dict[str, object]:
    return _run_sqltide_cli_json(
        command=(
            "deployment",
            "audit",
            deployment_id,
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--json",
        )
    )


def run_sqltide_deployment_promote_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
    deployment_id: str,
) -> None:
    _run_sqltide_cli(
        command=(
            "deployment",
            "promote",
            deployment_id,
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
        )
    )


def run_sqltide_deployment_rollback_previous_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
) -> dict[str, object]:
    return _run_sqltide_cli_json(
        command=(
            "deployment",
            "rollback",
            "--previous",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--json",
            "--auto-approve",
        )
    )


def run_sqltide_doctor_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
) -> dict[str, object]:
    return _run_sqltide_cli_json(
        command=(
            "doctor",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
        )
    )


def run_sqltide_repair_active_view_cli(
    *,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
    table_name: str,
    deployment_id: str,
) -> dict[str, object]:
    return _run_sqltide_cli_json(
        command=(
            "repair",
            "active-view",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--table",
            table_name,
            "--deployment-id",
            deployment_id,
        )
    )


def _run_sqltide_cli(*, command: tuple[str, ...]) -> None:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["uv", "run", "sqt", *command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not (result.returncode != 0), (
        f"sqt {' '.join(command)} failed with code {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _run_sqltide_cli_json(*, command: tuple[str, ...]) -> dict[str, object]:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["uv", "run", "sqt", *command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not (result.returncode != 0), (
        f"sqt {' '.join(command)} failed with code {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


def build_changed_greenfield_workflow_compiled_pipeline(
    *, kafka_broker_list: str, topic_suffix: str | None = None
) -> CompiledPipeline:
    compiled_pipeline: CompiledPipeline = build_greenfield_workflow_compiled_pipeline(
        kafka_broker_list=kafka_broker_list,
        topic_suffix=topic_suffix,
    )
    original_model: CompiledModel = compiled_pipeline.models[0]
    changed_model: CompiledModel = _analyzed_model_with_query(
        model=original_model,
        query=f"{original_model.query} WHERE 1 = 1",
    )
    return replace(compiled_pipeline, models=(changed_model,))


def build_greenfield_workflow_request(
    *,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=clickhouse_database,
        metadata_database=clickhouse_database,
        replay_lineage_mode=cast(
            ReplayLineageMode,
            compiled_pipeline.effective_replay_lineage_mode,
        ),
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_offset_workflow_compiled_pipeline(
    *, kafka_broker_list: str, topic_suffix: str | None = None
) -> CompiledPipeline:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
    return _with_kafka_broker_list_and_topic(
        compiled_pipeline=compiled_pipeline,
        kafka_broker_list=kafka_broker_list,
        topic_suffix=topic_suffix,
    )


SCHEMA_CHANGE_PIPELINE_BUILDERS: dict[str, Callable[[], CompiledPipeline]] = {
    "base": lambda: build_scalar_replay_compiled_pipeline(ReplayLineageMode.TIMESTAMP),
    "add_column": lambda: build_changed_schema_variant_compiled_pipeline("add_column"),
    "remove_column": lambda: build_changed_schema_variant_compiled_pipeline("remove_column"),
    "type_change": lambda: _build_non_boundary_type_change_compiled_pipeline(),
}


def build_schema_change_workflow_compiled_pipeline(
    *, kafka_broker_list: str, pipeline_kind: str, topic_suffix: str | None = None
) -> CompiledPipeline:
    builder: Callable[[], CompiledPipeline] = SCHEMA_CHANGE_PIPELINE_BUILDERS[pipeline_kind]
    compiled_pipeline: CompiledPipeline = builder()
    return _with_kafka_broker_list_and_topic(
        compiled_pipeline=compiled_pipeline,
        kafka_broker_list=kafka_broker_list,
        topic_suffix=topic_suffix,
    )


def with_replay_on_change_policy(
    *,
    compiled_pipeline: CompiledPipeline,
    breaking_mode: ReplayOnChangeMode | str,
    breaking_lookback_seconds: int | None,
    non_breaking_mode: ReplayOnChangeMode | str,
    non_breaking_lookback_seconds: int | None,
) -> CompiledPipeline:
    original_model: CompiledModel = compiled_pipeline.models[0]
    return replace(
        compiled_pipeline,
        models=(
            replace(
                original_model,
                replay_on_change=ReplayOnChangePolicy(
                    breaking=ReplayOnChangeRule(
                        mode=ReplayOnChangeMode(breaking_mode),
                        lookback_seconds=breaking_lookback_seconds,
                    ),
                    non_breaking=ReplayOnChangeRule(
                        mode=ReplayOnChangeMode(non_breaking_mode),
                        lookback_seconds=non_breaking_lookback_seconds,
                    ),
                ),
            ),
        ),
    )


def build_near_replay_times(*, seconds_from_now: int) -> tuple[str, str]:
    replay_time: datetime = datetime.now(tz=UTC) + timedelta(seconds=seconds_from_now)
    boundary_time: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.000")
    created_at: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.123")
    return created_at, boundary_time


def build_bounded_replay_times(*, lookback_seconds: int) -> tuple[int, int, str, str]:
    frontier_time: datetime = datetime.now(tz=UTC)
    historical_time: datetime = frontier_time - timedelta(seconds=lookback_seconds + 4)
    replay_time: datetime = frontier_time + timedelta(seconds=2)
    created_at: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.123")
    boundary_time: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.000")
    return (
        int(historical_time.timestamp() * 1000),
        int(frontier_time.timestamp() * 1000),
        created_at,
        boundary_time,
    )


def _with_kafka_broker_list_and_topic(
    *,
    compiled_pipeline: CompiledPipeline,
    kafka_broker_list: str,
    topic_suffix: str | None,
) -> CompiledPipeline:
    compiled_source: CompiledSource = cast(CompiledSource, compiled_pipeline.source)
    source: KafkaLandingStep = cast(KafkaLandingStep, compiled_source.source)
    base_topic_name: str = source.kafka.topic
    topic_parts: tuple[str, ...] = tuple(filter(None, (base_topic_name, topic_suffix)))
    topic_name: str = "_".join(topic_parts)
    updated_source: KafkaLandingStep = replace(
        source,
        kafka=replace(source.kafka, broker_list=kafka_broker_list, topic=topic_name),
    )
    return replace(
        compiled_pipeline,
        pipeline=replace(compiled_pipeline.pipeline, source=updated_source),
        source=replace(compiled_source, source=updated_source),
    )


def _build_non_boundary_type_change_compiled_pipeline() -> CompiledPipeline:
    base_pipeline: CompiledPipeline = build_changed_schema_variant_compiled_pipeline("add_column")
    original_model: CompiledModel = base_pipeline.models[0]
    changed_query: str = original_model.query.replace(
        "CAST(kafka_topic AS String) AS kafka_topic",
        "CAST(kafka_topic AS FixedString(128)) AS kafka_topic",
    )
    return replace(
        base_pipeline,
        models=(
            _analyzed_model_with_query(
                model=original_model,
                query=changed_query,
            ),
        ),
    )


def _analyzed_model_with_query(*, model: CompiledModel, query: str) -> CompiledModel:
    table_model: CompiledTableModel = cast(CompiledTableModel, model)
    sql_analysis: SqlModelAnalysis = SqlModelAnalyzer(dialect="clickhouse").analyze(
        sql=query,
        engine=table_model.transform.engine,
        order_by=tuple(table_model.transform.order_by),
        partition_by=table_model.transform.partition_by,
        ttl=table_model.transform.ttl,
    )
    return replace(
        table_model,
        transform=replace(table_model.transform, query=query, sql_file=None),
        sql_analysis=sql_analysis,
    )


def build_offset_workflow_request(
    *,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=clickhouse_database,
        metadata_database=clickhouse_database,
        replay_lineage_mode="offsets",
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_kafka_producer(*, bootstrap_server: str) -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=bootstrap_server,
        retries=10,
        retry_backoff_ms=500,
    )


def build_future_replay_times(*, seconds_from_now: int) -> tuple[str, str]:
    replay_time: datetime = datetime.now(tz=UTC) + timedelta(minutes=5, seconds=seconds_from_now)
    boundary_time: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.000")
    created_at: str = replay_time.strftime("%Y-%m-%d %H:%M:%S.123")
    return created_at, boundary_time


def produce_kafka_messages(
    *,
    producer: KafkaProducer,
    topic: str,
    messages: tuple[tuple[str, str], ...],
    timestamp_ms: int | None = None,
    headers: tuple[tuple[str, bytes], ...] = (),
) -> None:
    message_key: str
    message_value: str
    for message_key, message_value in messages:
        producer.send(
            topic,
            key=message_key.encode("utf-8"),
            value=message_value.encode("utf-8"),
            timestamp_ms=timestamp_ms,
            headers=list(headers),
        ).get(timeout=30)
    producer.flush()


def execute_e2e_clickhouse_client_sql(
    *, settings: E2EClickHouseConnectionSettings, sql: str
) -> tuple[int, str]:
    """Execute emitted workflow SQL through the real ClickHouse CLI."""

    completed: subprocess.CompletedProcess[str] = subprocess.run(
        (
            "docker",
            "exec",
            settings.container_id,
            "clickhouse-client",
            "--user",
            settings.username,
            "--password",
            settings.password,
            "--multiquery",
            "--query",
            sql,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stderr


def load_virtual_manual_workflow_snapshot(
    *,
    clickhouse_client: Client,
    database: str,
    deployment_id: str,
    target_table_name: str,
    audit_assessment: str,
) -> VirtualManagedManualWorkflowSnapshot:
    """Load normalized unpublished virtual state from one isolated database."""

    deployment_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT 'deployment', 'backfilling', replay_lineage_mode, '<workflow-fingerprint>', "
        "'<boundary-time>', tool_version "
        f"FROM {database}._sqltide_virtual_deployments "
        f"WHERE deployment_id = '{deployment_id}'"
    ).result_rows
    object_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT 'object', logical_object_type, logical_object_name, object_fingerprint, "
        "ifNull(canonical_query, ''), ifNull(physical_relation_name, ''), is_selected_root "
        f"FROM {database}._sqltide_virtual_object_state "
        f"WHERE deployment_id = '{deployment_id}' "
        "ORDER BY logical_object_type, logical_object_name"
    ).result_rows
    physical_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, engine, create_table_query FROM system.tables "
        f"WHERE database = '{database}' AND name NOT LIKE '\\_sqltide%' "
        "ORDER BY name"
    ).result_rows
    replay_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {database}.{target_table_name}__{deployment_id} ORDER BY order_id"
    ).result_rows
    watermark_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT boundary_kind, ifNull(partition_value, ''), cutoff_value "
        f"FROM {database}._sqltide_virtual_replay_boundaries "
        f"WHERE deployment_id = '{deployment_id}' ORDER BY boundary_kind, partition_value"
    ).result_rows
    publish_event_count: int = int(
        clickhouse_client.query(
            f"SELECT count() FROM {database}._sqltide_virtual_publications "
            f"WHERE deployment_id = '{deployment_id}'"
        ).result_rows[0][0]
    )
    stable_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, create_table_query FROM system.tables "
        f"WHERE database = '{database}' AND name = '{target_table_name}' ORDER BY name"
    ).result_rows
    deployment_metadata: tuple[tuple[str, ...], ...] = _normalize_virtual_snapshot_rows(
        rows=(*deployment_rows, *object_rows),
        database=database,
        deployment_id=deployment_id,
    )
    physical_graph: tuple[tuple[str, ...], ...] = _normalize_virtual_snapshot_rows(
        rows=physical_rows,
        database=database,
        deployment_id=deployment_id,
    )
    return VirtualManagedManualWorkflowSnapshot(
        deployment_status=str(deployment_rows[0][1]),
        deployment_metadata=deployment_metadata,
        physical_graph=physical_graph,
        replay_order_ids=tuple(str(row[0]) for row in replay_rows),
        watermark_rows=tuple((str(row[0]), str(row[1])) for row in watermark_rows),
        audit_assessment=audit_assessment,
        publish_event_count=publish_event_count,
        stable_bindings=tuple((str(row[0]), str(row[1])) for row in stable_rows),
    )


def _normalize_virtual_snapshot_rows(
    *, rows: Sequence[Sequence[object]], database: str, deployment_id: str
) -> tuple[tuple[str, ...], ...]:
    normalized_rows: list[tuple[str, ...]] = []
    row: Sequence[object]
    for row in rows:
        normalized_row: tuple[str, ...] = tuple(
            str(value).replace(database, "<database>").replace(deployment_id, "<deployment>")
            for value in row
        )
        normalized_rows.append(normalized_row)
    return tuple(normalized_rows)


def wait_for_live_shadow_row_count(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    raw_table_name: str,
    staged_table_name: str,
    expected_count: int,
    timeout_seconds: float = 45.0,
) -> None:
    try:
        wait_for_row_count(
            clickhouse_client=clickhouse_client,
            clickhouse_database=clickhouse_database,
            table_name=staged_table_name,
            expected_count=expected_count,
            timeout_seconds=timeout_seconds,
        )
    except AssertionError as error:
        raise AssertionError(
            build_live_shadow_debug_message(
                clickhouse_client=clickhouse_client,
                clickhouse_database=clickhouse_database,
                raw_table_name=raw_table_name,
                staged_table_name=staged_table_name,
                error=error,
            )
        ) from error


def build_live_shadow_debug_message(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    raw_table_name: str,
    staged_table_name: str,
    error: AssertionError,
) -> str:
    staged_mv_name: str = staged_table_name.replace("tbl__", "mv__", 1)
    live_table_name: str = staged_table_name.rsplit("__", maxsplit=1)[0]
    raw_count: int = _query_table_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        table_name=raw_table_name,
    )
    staged_count: int = _query_table_count(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        table_name=staged_table_name,
    )
    latest_raw_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT _replay_partition, _replay_offset, _replay_timestamp FROM "
        f"{clickhouse_database}.{raw_table_name} "
        "ORDER BY _replay_partition DESC, _replay_offset DESC LIMIT 5"
    ).result_rows
    latest_staged_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id, _replay_timestamp FROM {clickhouse_database}.{staged_table_name} "
        "ORDER BY _replay_timestamp DESC, order_id DESC LIMIT 5"
    ).result_rows
    live_count: int | None = clickhouse_client.query(
        "SELECT anyOrNull(total_rows) FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name = '{live_table_name}'"
    ).result_rows[0][0]
    staged_mv_metadata: Sequence[object] = clickhouse_client.query(
        "SELECT count() > 0, anyOrNull(create_table_query) FROM system.tables "
        f"WHERE database = '{clickhouse_database}' AND name = '{staged_mv_name}'"
    ).result_rows[0]
    staged_mv_exists: bool = bool(staged_mv_metadata[0])
    staged_mv_ddl: str | None = staged_mv_metadata[1]
    return (
        f"{error}. raw_count={raw_count}, staged_count={staged_count}, "
        f"live_count={live_count}, staged_mv_exists={staged_mv_exists}, "
        f"latest_raw_rows={tuple(latest_raw_rows)}, "
        f"latest_staged_rows={tuple(latest_staged_rows)}, "
        f"staged_mv_ddl={staged_mv_ddl!r}"
    )


def _query_table_count(
    *, clickhouse_client: Client, clickhouse_database: str, table_name: str
) -> int:
    return int(
        clickhouse_client.query(
            f"SELECT count() FROM {clickhouse_database}.{table_name}"
        ).result_rows[0][0]
    )


def wait_for_row_count(
    *,
    clickhouse_client: Client,
    table_name: str,
    clickhouse_database: str,
    expected_count: int,
    timeout_seconds: float = 25.0,
    poll_interval_seconds: float = 0.5,
) -> None:
    deadline: float = time.time() + timeout_seconds
    while time.time() < deadline:
        result: Any = clickhouse_client.query(
            f"SELECT count() FROM {clickhouse_database}.{table_name}"
        )
        actual_count: int = int(result.result_rows[0][0])
        if actual_count >= expected_count:
            return
        time.sleep(poll_interval_seconds)
    raise AssertionError(
        f"Timed out waiting for {clickhouse_database}.{table_name} to reach {expected_count} rows"
    )


def wait_for_query_view_activity(
    *, client: Client, database: str, relation_name: str, expected_rows: int
) -> tuple[str, int]:
    deadline: float = time.monotonic() + 25
    last_observation: tuple[str, int] | None = None
    while time.monotonic() < deadline:
        client.command("SYSTEM FLUSH LOGS")
        rows: Sequence[Sequence[Any]] = client.query(
            build_query_views_activity_query(database=database, window_seconds=120)
        ).result_rows
        for row in rows:
            target: str = str(row[1]).replace("`", "").rsplit(".", 1)[-1]
            if target != relation_name:
                continue
            last_observation = (str(row[3]), int(row[2]))
        if last_observation is not None:
            status, written_rows = last_observation
            if status == "QueryFinish" and written_rows >= expected_rows:
                return last_observation
        time.sleep(0.25)
    raise AssertionError(
        f"query_views_log activity did not reach {expected_rows} rows for "
        f"{database}.{relation_name}; last={last_observation!r}"
    )


def wait_for_state_model_activity(
    *, base_url: str, model_name: str, expected_state: str
) -> dict[str, object]:
    deadline: float = time.monotonic() + 25
    last_activity: dict[str, object] | None = None
    while time.monotonic() < deadline:
        with urlopen(f"{base_url}/api/state", timeout=10) as response:  # noqa: S310
            payload: object = json.loads(response.read().decode("utf-8"))
        assert isinstance(payload, dict)
        payload_dict: dict[str, object] = cast(dict[str, object], payload)
        models: object = payload_dict.get("models")
        assert isinstance(models, dict)
        model: object = cast(dict[str, object], models).get(model_name)
        assert isinstance(model, dict)
        activity: object = cast(dict[str, object], model).get("activity")
        assert isinstance(activity, dict)
        last_activity = cast(dict[str, object], activity)
        if last_activity.get("state") == expected_state:
            return payload_dict
        time.sleep(0.1)
    raise AssertionError(
        f"state API activity for {model_name!r} did not become {expected_state!r}; "
        f"last={last_activity!r}"
    )


def wait_for_table_exists(
    *,
    clickhouse_client: Client,
    table_name: str,
    clickhouse_database: str,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.5,
) -> None:
    deadline: float = time.time() + timeout_seconds
    while time.time() < deadline:
        result: Any = clickhouse_client.query(
            "SELECT count() FROM system.tables "
            f"WHERE database = '{clickhouse_database}' AND name = '{table_name}'"
        )
        if int(result.result_rows[0][0]) > 0:
            return
        time.sleep(poll_interval_seconds)
    raise AssertionError(f"Timed out waiting for {clickhouse_database}.{table_name} to exist")


def wait_for_table_missing(
    *,
    clickhouse_client: Client,
    table_name: str,
    clickhouse_database: str,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.5,
) -> None:
    deadline: float = time.time() + timeout_seconds
    while time.time() < deadline:
        result: Any = clickhouse_client.query(
            "SELECT count() FROM system.tables "
            f"WHERE database = '{clickhouse_database}' AND name = '{table_name}'"
        )
        if int(result.result_rows[0][0]) == 0:
            return
        time.sleep(poll_interval_seconds)
    raise AssertionError(f"Timed out waiting for {clickhouse_database}.{table_name} to disappear")
