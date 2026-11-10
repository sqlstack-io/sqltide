import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client
from kafka import KafkaProducer

from sqltide._native import execute_publish
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.types import ReplayLineageMode, ReplayOnChangeMode
from sqltide.compiler.planner.types import RebuildExecutionMode
from sqltide.executor.backfill.models import BackfillExecutionResult
from sqltide.executor.promotion.models import PublishRequest
from sqltide.executor.readiness.types import AuditAssessment
from sqltide.executor.workflow.models import PublishedBuildWorkflow
from tests.e2e.src.sqltide.conftest import (
    E2EClickHouseConnectionSettings,
    E2EKafkaConnectionSettings,
)
from tests.e2e.src.sqltide.executor._test_types import (
    DirectManagedManualWorkflowE2ETestCase,
    GreenfieldKafkaWorkflowE2ETestCase,
    KafkaLiveShadowScenarioResult,
    KafkaLiveShadowWorkflowE2ETestCase,
    KafkaOffsetAuditWorkflowE2ETestCase,
    KafkaRecoveryWorkflowE2ETestCase,
    KafkaSchemaChangeWorkflowE2ETestCase,
    ManagedSourceBootstrapE2ETestCase,
    VirtualManagedManualWorkflowE2ETestCase,
    VirtualManagedManualWorkflowSnapshot,
    VirtualRollbackE2ETestCase,
)
from tests.e2e.src.sqltide.executor.helpers import (
    E2E_KAFKA_LANDED_AT_PROJECT_DIR,
    E2E_KAFKA_OFFSET_PROJECT_DIR,
    E2E_KAFKA_TIMESTAMP_PROJECT_DIR,
    build_authored_greenfield_workflow_compiled_pipeline,
    build_bounded_replay_times,
    build_future_replay_times,
    build_greenfield_workflow_request,
    build_kafka_producer,
    build_schema_change_workflow_compiled_pipeline,
    execute_e2e_clickhouse_client_sql,
    load_virtual_manual_workflow_snapshot,
    prepare_authored_e2e_project,
    produce_kafka_messages,
    require_managed_source,
    require_model_resources,
    run_kafka_live_shadow_scenario,
    run_sqltide_deployment_audit_cli,
    run_sqltide_deployment_promote_cli,
    run_sqltide_deployment_rollback_previous_cli,
    run_sqltide_doctor_cli,
    run_sqltide_repair_active_view_cli,
    run_sqltide_virtual_build_cli,
    wait_for_row_count,
    wait_for_table_exists,
    wait_for_table_missing,
    with_replay_on_change_policy,
)
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.cli.helpers import (
    direct_build_order_ids,
    direct_fingerprinted_relation_names,
    direct_replay_artifact_ranges,
    publish_direct_workflow,
    publish_virtual_workflow,
    run_direct_build,
    write_direct_build_project,
)
from tests.integration.src.sqltide.executor.backfill.helpers import execute_backfill

GREENFIELD_CREATED_AT: str
GREENFIELD_BOUNDARY_TIME: str
GREENFIELD_CREATED_AT, GREENFIELD_BOUNDARY_TIME = build_future_replay_times(seconds_from_now=0)

RECOVERY_CREATED_AT: str
RECOVERY_BOUNDARY_TIME: str
RECOVERY_CREATED_AT, RECOVERY_BOUNDARY_TIME = build_future_replay_times(seconds_from_now=10)

OFFSET_AUDIT_CREATED_AT: str
OFFSET_AUDIT_BOUNDARY_TIME: str
OFFSET_AUDIT_CREATED_AT, OFFSET_AUDIT_BOUNDARY_TIME = build_future_replay_times(seconds_from_now=40)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        DirectManagedManualWorkflowE2ETestCase(
            description="managed direct command numbered and combined workflows match",
            messages=(("order-1", "{}"), ("order-2", "{}")),
            expected_order_ids=("order-1", "order-2"),
            expected_exit_code=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_retained_kafka_messages_when_executing_direct_artifacts_then_forms_match(
    test_case: DirectManagedManualWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    numbered_database: str = f"{e2e_clickhouse_database}_steps"
    combined_database: str = f"{e2e_clickhouse_database}_combined"
    databases: tuple[str, ...] = (
        e2e_clickhouse_database,
        numbered_database,
        combined_database,
    )
    topic: str = f"source.direct_manual.{e2e_clickhouse_database}"
    write_direct_build_project(
        project_root=tmp_path,
        topic=topic,
        broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
    )
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        e2e_clickhouse_client.command(f"CREATE DATABASE {numbered_database}")
        e2e_clickhouse_client.command(f"CREATE DATABASE {combined_database}")
        produce_kafka_messages(producer=producer, topic=topic, messages=test_case.messages)
        command_exit_code: int = run_direct_build(
            project_root=tmp_path,
            database=e2e_clickhouse_database,
            connection=connection,
        )
        numbered: PublishedBuildWorkflow = publish_direct_workflow(
            project_root=tmp_path,
            database=numbered_database,
            connection=connection,
            stabilization_seconds=10,
        )
        numbered_results: tuple[tuple[int, str], ...] = tuple(
            execute_e2e_clickhouse_client_sql(
                settings=e2e_clickhouse_connection_settings,
                sql=path.read_text(encoding="utf-8"),
            )
            for path in sorted((numbered.artifact_root / "steps").iterdir())
        )
        combined: PublishedBuildWorkflow = publish_direct_workflow(
            project_root=tmp_path,
            database=combined_database,
            connection=connection,
            stabilization_seconds=10,
        )
        combined_result: tuple[int, str] = execute_e2e_clickhouse_client_sql(
            settings=e2e_clickhouse_connection_settings,
            sql=(combined.artifact_root / "workflow.sql").read_text(encoding="utf-8"),
        )
        wait_for_row_count(
            clickhouse_client=e2e_clickhouse_client,
            clickhouse_database=numbered_database,
            table_name="tbl__orders_enriched",
            expected_count=len(test_case.expected_order_ids),
        )
        wait_for_row_count(
            clickhouse_client=e2e_clickhouse_client,
            clickhouse_database=combined_database,
            table_name="tbl__orders_enriched",
            expected_count=len(test_case.expected_order_ids),
        )
        order_ids: tuple[tuple[str, ...], ...] = tuple(
            direct_build_order_ids(clickhouse_client=e2e_clickhouse_client, database=database)
            for database in databases
        )
        fingerprinted_names: tuple[tuple[str, ...], ...] = (
            direct_fingerprinted_relation_names(
                clickhouse_client=e2e_clickhouse_client, database=e2e_clickhouse_database
            ),
            (),
            (),
        )
    finally:
        producer.close()
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {numbered_database} SYNC")
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {combined_database} SYNC")
        connection.close()

    assert command_exit_code == test_case.expected_exit_code
    assert tuple(result[0] for result in numbered_results) == tuple(
        test_case.expected_exit_code for _result in numbered_results
    )
    assert combined_result[0] == test_case.expected_exit_code
    assert order_ids == tuple(test_case.expected_order_ids for _database in databases)
    assert fingerprinted_names == (("orders_enriched",), (), ())


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        DirectManagedManualWorkflowE2ETestCase(
            description="bounded direct command numbered and combined workflows match",
            messages=(
                ("order-1", "{}"),
                ("order-2", "{}"),
                ("order-3", "{}"),
                ("order-4", "{}"),
            ),
            expected_order_ids=("order-2", "order-3", "order-4"),
            expected_exit_code=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_bounded_direct_workflow_when_executing_artifacts_then_forms_match(
    test_case: DirectManagedManualWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    numbered_database: str = f"{e2e_clickhouse_database}_bounded_steps"
    combined_database: str = f"{e2e_clickhouse_database}_bounded_combined"
    databases: tuple[str, ...] = (
        e2e_clickhouse_database,
        numbered_database,
        combined_database,
    )
    topic: str = f"source.direct_bounded_manual.{e2e_clickhouse_database}"
    write_direct_build_project(
        project_root=tmp_path,
        topic=topic,
        broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
    )
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        e2e_clickhouse_client.command(f"CREATE DATABASE {numbered_database}")
        e2e_clickhouse_client.command(f"CREATE DATABASE {combined_database}")
        bootstrap_exit_codes: tuple[int, ...] = tuple(
            run_direct_build(
                project_root=tmp_path,
                database=database,
                connection=connection,
            )
            for database in databases
        )
        produce_kafka_messages(
            producer=producer,
            topic=topic,
            messages=test_case.messages[:2],
        )
        database: str
        for database in databases:
            wait_for_row_count(
                clickhouse_client=e2e_clickhouse_client,
                clickhouse_database=database,
                table_name="tbl__orders_enriched",
                expected_count=2,
            )
        requested_start_time: str = connection.capture_warehouse_timestamp().replace(" ", "T") + "Z"
        effective_start_time: str = requested_start_time.replace("T", " ").removesuffix("Z")
        produce_kafka_messages(
            producer=producer,
            topic=topic,
            messages=test_case.messages[2:],
        )
        for database in databases:
            wait_for_row_count(
                clickhouse_client=e2e_clickhouse_client,
                clickhouse_database=database,
                table_name="tbl__orders_enriched",
                expected_count=4,
            )
        command_exit_code: int = run_direct_build(
            project_root=tmp_path,
            database=e2e_clickhouse_database,
            connection=connection,
            selectors=("orders_enriched",),
            start_time=requested_start_time,
        )
        numbered: PublishedBuildWorkflow = publish_direct_workflow(
            project_root=tmp_path,
            database=numbered_database,
            connection=connection,
            selectors=("orders_enriched",),
            effective_start_time=effective_start_time,
        )
        numbered_results: tuple[tuple[int, str], ...] = tuple(
            execute_e2e_clickhouse_client_sql(
                settings=e2e_clickhouse_connection_settings,
                sql=path.read_text(encoding="utf-8"),
            )
            for path in sorted((numbered.artifact_root / "steps").iterdir())
        )
        combined: PublishedBuildWorkflow = publish_direct_workflow(
            project_root=tmp_path,
            database=combined_database,
            connection=connection,
            selectors=("orders_enriched",),
            effective_start_time=effective_start_time,
        )
        combined_result: tuple[int, str] = execute_e2e_clickhouse_client_sql(
            settings=e2e_clickhouse_connection_settings,
            sql=(combined.artifact_root / "workflow.sql").read_text(encoding="utf-8"),
        )
        for database in databases:
            wait_for_row_count(
                clickhouse_client=e2e_clickhouse_client,
                clickhouse_database=database,
                table_name="tbl__orders_enriched",
                expected_count=len(test_case.expected_order_ids),
            )
        order_ids: tuple[tuple[str, ...], ...] = tuple(
            direct_build_order_ids(clickhouse_client=e2e_clickhouse_client, database=database)
            for database in databases
        )
        coverage_ranges: tuple[tuple[tuple[str, str, str], ...], ...] = tuple(
            direct_replay_artifact_ranges(project_root=tmp_path) for _database in databases
        )
        workflow_sql: str = (combined.artifact_root / "workflow.sql").read_text(encoding="utf-8")
    finally:
        producer.close()
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {numbered_database} SYNC")
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {combined_database} SYNC")
        connection.close()

    assert bootstrap_exit_codes == tuple(test_case.expected_exit_code for _database in databases)
    assert command_exit_code == test_case.expected_exit_code
    assert tuple(result[0] for result in numbered_results) == tuple(
        test_case.expected_exit_code for _result in numbered_results
    )
    assert combined_result[0] == test_case.expected_exit_code
    assert order_ids == tuple(test_case.expected_order_ids for _database in databases)
    assert coverage_ranges == tuple((("_replay_partition=0", "1", "3"),) for _database in databases)
    assert effective_start_time in workflow_sql


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualManagedManualWorkflowE2ETestCase(
            description=(
                "managed virtual command numbered and combined workflows match unpublished state"
            ),
            deployment_ids=(
                "20260801T120000Z_virtualcommand",
                "20260801T120100Z_virtualsteps",
                "20260801T120200Z_virtualcombined",
            ),
            messages=(("order-1", '{"order_id":"order-1"}'), ("order-2", '{"order_id":"order-2"}')),
            expected_order_ids=("order-1", "order-2"),
            expected_watermark_rows=(("offsets", "0"),),
            expected_audit_assessment=AuditAssessment.READY,
            expected_deployment_status="backfilling",
            expected_publish_event_count=0,
            expected_stable_bindings=(),
            expected_exit_code=0,
            expected_min_physical_relation_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_retained_kafka_messages_when_executing_virtual_artifacts_then_forms_match(
    test_case: VirtualManagedManualWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    numbered_database: str = f"{e2e_clickhouse_database}_virtual_steps"
    combined_database: str = f"{e2e_clickhouse_database}_virtual_combined"
    databases: tuple[str, ...] = (
        e2e_clickhouse_database,
        numbered_database,
        combined_database,
    )
    project_parents: tuple[Path, ...] = (
        tmp_path / "command",
        tmp_path / "numbered",
        tmp_path / "combined",
    )
    project_parent: Path
    for project_parent in project_parents:
        project_parent.mkdir()
    project_dirs: tuple[Path, ...] = tuple(
        prepare_authored_e2e_project(
            fixture_project_dir=E2E_KAFKA_OFFSET_PROJECT_DIR,
            tmp_path=project_parent,
            kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
            topic_suffix=database,
        )
        for project_parent, database in zip(project_parents, databases, strict=True)
    )
    compiled_pipelines: tuple[CompiledPipeline, ...] = tuple(
        build_authored_greenfield_workflow_compiled_pipeline(project_dir=project_dir)
        for project_dir in project_dirs
    )
    target_table_names: tuple[str, ...] = tuple(
        require_model_resources(compiled_pipeline).target_table_name
        for compiled_pipeline in compiled_pipelines
    )
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    e2e_clickhouse_client.command(f"CREATE DATABASE {numbered_database}")
    e2e_clickhouse_client.command(f"CREATE DATABASE {combined_database}")
    compiled_pipeline: CompiledPipeline
    database: str
    for compiled_pipeline, database in zip(compiled_pipelines, databases, strict=True):
        e2e_clickhouse_client.command(
            render_create_kafka_table_ddl(
                table=require_managed_source(compiled_pipeline).kafka_table,
                database=database,
            )
        )
        e2e_clickhouse_client.command(
            render_create_table_ddl(
                table=require_managed_source(compiled_pipeline).raw_table,
                database=database,
            )
        )
        e2e_clickhouse_client.command(
            render_create_materialized_view_ddl(
                materialized_view=require_managed_source(compiled_pipeline).materialized_view,
                database=database,
            )
        )
    try:
        for compiled_pipeline in compiled_pipelines:
            produce_kafka_messages(
                producer=producer,
                topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
                messages=test_case.messages,
            )
    finally:
        producer.close()
    for compiled_pipeline, database in zip(compiled_pipelines, databases, strict=True):
        wait_for_row_count(
            clickhouse_client=e2e_clickhouse_client,
            clickhouse_database=database,
            table_name=require_managed_source(compiled_pipeline).raw_table.name,
            expected_count=len(test_case.expected_order_ids),
        )
    try:
        run_sqltide_virtual_build_cli(
            project_dir=project_dirs[0],
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=databases[0],
            deployment_id=test_case.deployment_ids[0],
        )
        numbered: PublishedBuildWorkflow = publish_virtual_workflow(
            project_root=project_dirs[1],
            database=databases[1],
            deployment_id=test_case.deployment_ids[1],
            connection=connection,
        )
        numbered_results: tuple[tuple[int, str], ...] = tuple(
            execute_e2e_clickhouse_client_sql(
                settings=e2e_clickhouse_connection_settings,
                sql=path.read_text(encoding="utf-8"),
            )
            for path in sorted((numbered.artifact_root / "steps").iterdir())
        )
        combined: PublishedBuildWorkflow = publish_virtual_workflow(
            project_root=project_dirs[2],
            database=databases[2],
            deployment_id=test_case.deployment_ids[2],
            connection=connection,
        )
        combined_result: tuple[int, str] = execute_e2e_clickhouse_client_sql(
            settings=e2e_clickhouse_connection_settings,
            sql=(combined.artifact_root / "workflow.sql").read_text(encoding="utf-8"),
        )
        audit_results: tuple[dict[str, object], ...] = tuple(
            run_sqltide_deployment_audit_cli(
                project_dir=project_dir,
                host=e2e_clickhouse_connection_settings.host,
                port=e2e_clickhouse_connection_settings.port,
                username=e2e_clickhouse_connection_settings.username,
                password=e2e_clickhouse_connection_settings.password,
                database=database,
                deployment_id=deployment_id,
            )
            for project_dir, database, deployment_id in zip(
                project_dirs, databases, test_case.deployment_ids, strict=True
            )
        )
        snapshots: tuple[VirtualManagedManualWorkflowSnapshot, ...] = tuple(
            load_virtual_manual_workflow_snapshot(
                clickhouse_client=e2e_clickhouse_client,
                database=database,
                deployment_id=deployment_id,
                target_table_name=target_table_name,
                audit_assessment=str(audit_result["assessment"]),
            )
            for database, deployment_id, target_table_name, audit_result in zip(
                databases,
                test_case.deployment_ids,
                target_table_names,
                audit_results,
                strict=True,
            )
        )
    finally:
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {numbered_database} SYNC")
        e2e_clickhouse_client.command(f"DROP DATABASE IF EXISTS {combined_database} SYNC")
        connection.close()

    assert tuple(result[0] for result in numbered_results) == tuple(
        test_case.expected_exit_code for _result in numbered_results
    )
    assert combined_result[0] == test_case.expected_exit_code
    assert snapshots == tuple(snapshots[0] for _database in databases)
    assert tuple(snapshot.deployment_status for snapshot in snapshots) == tuple(
        test_case.expected_deployment_status for _database in databases
    )
    assert tuple(snapshot.replay_order_ids for snapshot in snapshots) == tuple(
        test_case.expected_order_ids for _database in databases
    )
    assert tuple(snapshot.watermark_rows for snapshot in snapshots) == tuple(
        test_case.expected_watermark_rows for _database in databases
    )
    assert tuple(snapshot.audit_assessment for snapshot in snapshots) == tuple(
        test_case.expected_audit_assessment for _database in databases
    )
    assert tuple(snapshot.publish_event_count for snapshot in snapshots) == tuple(
        test_case.expected_publish_event_count for _database in databases
    )
    assert tuple(snapshot.stable_bindings for snapshot in snapshots) == tuple(
        test_case.expected_stable_bindings for _database in databases
    )
    assert len(snapshots[0].physical_graph) >= test_case.expected_min_physical_relation_count


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        GreenfieldKafkaWorkflowE2ETestCase(
            description="runs the greenfield Kafka workflow from landing ingest through publish",
            fixture_project_dir=E2E_KAFKA_TIMESTAMP_PROJECT_DIR,
            deployment_id="20260410T000000Z_ab12cd",
            created_at=GREENFIELD_CREATED_AT,
            boundary_time=GREENFIELD_BOUNDARY_TIME,
            expected_order_ids=("order-1", "order-2"),
            expected_audit_assessment=AuditAssessment.READY,
            expected_replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
            produced_headers=(("trace-id", b"greenfield-trace"), ("trace-id", b"duplicate-key")),
            expected_header_keys=("trace-id", "trace-id"),
            expected_header_values=("greenfield-trace", "duplicate-key"),
        ),
        GreenfieldKafkaWorkflowE2ETestCase(
            description="runs managed Kafka landed-at replay from landing ingest through publish",
            fixture_project_dir=E2E_KAFKA_LANDED_AT_PROJECT_DIR,
            deployment_id="20260410T000500Z_bc23de",
            created_at=GREENFIELD_CREATED_AT,
            boundary_time=GREENFIELD_BOUNDARY_TIME,
            expected_order_ids=("order-1", "order-2"),
            expected_audit_assessment=AuditAssessment.READY,
            expected_replay_lineage_mode=ReplayLineageMode.LANDED_AT,
            produced_headers=(),
            expected_header_keys=(),
            expected_header_values=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_kafka_backed_greenfield_pipeline_when_running_then_it_publishes_expected_rows(
    test_case: GreenfieldKafkaWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=test_case.fixture_project_dir,
        tmp_path=tmp_path,
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=e2e_clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    e2e_clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=e2e_clickhouse_database,
        )
    )

    produced_timestamp_ms: int = int(time.time()) * 1000 + 123
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=tuple(
                (order_id, json.dumps({"order_id": order_id}))
                for order_id in test_case.expected_order_ids
            ),
            timestamp_ms=produced_timestamp_ms,
            headers=test_case.produced_headers,
        )
    finally:
        producer.close()

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=len(test_case.expected_order_ids),
    )
    landed_header_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        "SELECT kafka_header_keys, kafka_header_values, "
        "toUnixTimestamp64Milli(kafka_timestamp), toUnixTimestamp64Milli(_replay_timestamp) FROM "
        f"{e2e_clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name} "
        "ORDER BY _replay_offset"
    ).result_rows
    assert tuple(tuple(row[0]) for row in landed_header_rows) == tuple(
        test_case.expected_header_keys for _order_id in test_case.expected_order_ids
    )
    assert tuple(tuple(row[1]) for row in landed_header_rows) == tuple(
        test_case.expected_header_values for _order_id in test_case.expected_order_ids
    )
    assert tuple((row[2], row[3]) for row in landed_header_rows) == tuple(
        (produced_timestamp_ms, produced_timestamp_ms) for _order_id in test_case.expected_order_ids
    )

    run_sqltide_virtual_build_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    audit_result: dict[str, object] = run_sqltide_deployment_audit_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    run_sqltide_deployment_promote_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=f"{target_table_name}__{test_case.deployment_id}",
        expected_count=len(test_case.expected_order_ids),
    )
    published_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"SELECT order_id FROM {e2e_clickhouse_database}.{target_table_name} ORDER BY order_id"
    ).result_rows

    assert audit_result["assessment"] == test_case.expected_audit_assessment
    assert compiled_pipeline.effective_replay_lineage_mode == test_case.expected_replay_lineage_mode
    assert tuple(row[0] for row in published_rows) == test_case.expected_order_ids


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualRollbackE2ETestCase(
            description="rebinds active Kafka-backed views to the previous live publication",
            fixture_project_dir=E2E_KAFKA_LANDED_AT_PROJECT_DIR,
            initial_deployment_id="20260808T130000Z_initial",
            current_deployment_id="20260808T131000Z_current",
            expected_rolled_back_order_ids=("rollback-order-1", "rollback-order-2"),
            expected_current_order_ids=("rollback-order-1", "rollback-order-2"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_published_kafka_deployments_when_rolling_back_then_previous_rows_are_active(
    test_case: VirtualRollbackE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=test_case.fixture_project_dir,
        tmp_path=tmp_path,
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=e2e_clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    topic: str = require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        produce_kafka_messages(
            producer=producer,
            topic=topic,
            messages=(("rollback-order-1", '{"order_id":"rollback-order-1"}'),),
        )
        run_sqltide_virtual_build_cli(
            project_dir=project_dir,
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
            deployment_id=test_case.initial_deployment_id,
        )
        run_sqltide_deployment_promote_cli(
            project_dir=project_dir,
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
            deployment_id=test_case.initial_deployment_id,
        )
        produce_kafka_messages(
            producer=producer,
            topic=topic,
            messages=(("rollback-order-2", '{"order_id":"rollback-order-2"}'),),
        )
        wait_for_row_count(
            clickhouse_client=e2e_clickhouse_client,
            clickhouse_database=e2e_clickhouse_database,
            table_name=require_managed_source(compiled_pipeline).raw_table.name,
            expected_count=len(test_case.expected_current_order_ids),
        )
        initial_physical_name: str = f"{target_table_name}__{test_case.initial_deployment_id}"
        current_physical_name: str = f"{target_table_name}__{test_case.current_deployment_id}"
        raw_table_name: str = require_managed_source(compiled_pipeline).raw_table.name
        e2e_clickhouse_client.command(
            f"CREATE TABLE {e2e_clickhouse_database}.{current_physical_name} AS "
            f"{e2e_clickhouse_database}.{initial_physical_name}"
        )
        e2e_clickhouse_client.command(
            f"INSERT INTO {e2e_clickhouse_database}.{current_physical_name} "
            "SELECT JSONExtractString(kafka_value, 'order_id') AS order_id, "
            f"_replay_landed_at FROM {e2e_clickhouse_database}.{raw_table_name}"
        )
        e2e_clickhouse_client.command(
            f"INSERT INTO {e2e_clickhouse_database}._sqltide_virtual_deployments "
            "(deployment_id, workflow_fingerprint, replay_lineage_mode, boundary_time, "
            "created_at, tool_version) VALUES ("
            f"'{test_case.current_deployment_id}', 'rollback-e2e', 'landed_at', now64(3), "
            "now64(3), 'e2e')"
        )
        e2e_clickhouse_client.command(
            f"INSERT INTO {e2e_clickhouse_database}._sqltide_virtual_object_state "
            "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
            "logical_object_type, logical_object_name, physical_database_name, "
            "physical_relation_name, logical_model_database, logical_model_name, "
            "is_selected_root, object_fingerprint, canonical_query, observed_at) VALUES ("
            f"'{test_case.current_deployment_id}', 'rollback-e2e-current', 'deployment', "
            f"'{test_case.current_deployment_id}', '{e2e_clickhouse_database}', 'table', "
            f"'{target_table_name}', '{e2e_clickhouse_database}', '{current_physical_name}', "
            f"NULL, '{compiled_pipeline.models[0].key.name}', true, 'rollback-e2e', NULL, now64(3))"
        )
        run_sqltide_deployment_promote_cli(
            project_dir=project_dir,
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
            deployment_id=test_case.current_deployment_id,
        )
    finally:
        producer.close()
    current_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"SELECT order_id FROM {e2e_clickhouse_database}.{target_table_name} ORDER BY order_id"
    ).result_rows

    rollback_result: dict[str, object] = run_sqltide_deployment_rollback_previous_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
    )
    rolled_back_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"SELECT order_id FROM {e2e_clickhouse_database}.{target_table_name} ORDER BY order_id"
    ).result_rows
    rollback_history: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        "SELECT operation, previous_deployment_id, deployment_id FROM "
        f"{e2e_clickhouse_database}._sqltide_virtual_publications "
        "ORDER BY published_at DESC LIMIT 1"
    ).result_rows

    assert tuple(row[0] for row in current_rows) == test_case.expected_current_order_ids
    assert rollback_result["operation"] == "rollback"
    assert rollback_result["previous_deployment_id"] == test_case.current_deployment_id
    assert rollback_result["deployment_id"] == test_case.initial_deployment_id
    assert tuple(row[0] for row in rolled_back_rows) == test_case.expected_rolled_back_order_ids
    assert rollback_history == [
        ("rollback", test_case.current_deployment_id, test_case.initial_deployment_id)
    ]


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        ManagedSourceBootstrapE2ETestCase(
            description=("builds every message produced before SQLTide creates managed resources"),
            fixture_project_dir=E2E_KAFKA_LANDED_AT_PROJECT_DIR,
            deployment_id="20260731T120000Z_bootstrap",
            expected_order_ids=("pre-source-1", "pre-source-2", "pre-source-3"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_messages_before_managed_source_creation_when_building_then_output_is_complete(
    test_case: ManagedSourceBootstrapE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=test_case.fixture_project_dir,
        tmp_path=tmp_path,
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=e2e_clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=tuple(
                (order_id, json.dumps({"order_id": order_id}))
                for order_id in test_case.expected_order_ids
            ),
        )
    finally:
        producer.close()

    run_sqltide_virtual_build_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    staged_table_name: str = f"{target_table_name}__{test_case.deployment_id}"
    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=staged_table_name,
        expected_count=len(test_case.expected_order_ids),
    )
    output_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"SELECT order_id FROM {e2e_clickhouse_database}.{staged_table_name} ORDER BY order_id"
    ).result_rows

    assert tuple(row[0] for row in output_rows) == test_case.expected_order_ids


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaRecoveryWorkflowE2ETestCase(
            description="repairs a deleted stable view after publishing a Kafka-backed deployment",
            deployment_id="20260410T001000Z_ab12cd",
            created_at=RECOVERY_CREATED_AT,
            boundary_time=RECOVERY_BOUNDARY_TIME,
            expected_order_ids=("order-1", "order-2"),
            expected_doctor_state_kind="logical_view_missing",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_kafka_backed_published_deployment_when_view_is_deleted_then_repair_restores_it(
    test_case: KafkaRecoveryWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=E2E_KAFKA_TIMESTAMP_PROJECT_DIR,
        tmp_path=tmp_path,
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=e2e_clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    target_table_name: str = require_model_resources(compiled_pipeline).target_table_name
    e2e_clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=e2e_clickhouse_database,
        )
    )

    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=tuple(
                (order_id, json.dumps({"order_id": order_id}))
                for order_id in test_case.expected_order_ids
            ),
        )
    finally:
        producer.close()

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=len(test_case.expected_order_ids),
    )

    run_sqltide_virtual_build_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    run_sqltide_deployment_promote_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    e2e_clickhouse_client.command(f"DROP VIEW {e2e_clickhouse_database}.{target_table_name}")
    wait_for_table_missing(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=target_table_name,
    )
    doctor_result: dict[str, object] = run_sqltide_doctor_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
    )
    repair_result: dict[str, object] = run_sqltide_repair_active_view_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        table_name=target_table_name,
        deployment_id=test_case.deployment_id,
    )

    wait_for_table_exists(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=target_table_name,
    )
    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=f"{target_table_name}__{test_case.deployment_id}",
        expected_count=len(test_case.expected_order_ids),
    )
    repaired_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"SELECT order_id FROM {e2e_clickhouse_database}.{target_table_name} ORDER BY order_id"
    ).result_rows

    active_views: list[dict[str, object]] = cast(
        list[dict[str, object]], doctor_result["active_views"]
    )
    candidate_deployment_ids: tuple[str, ...] = tuple(
        cast(list[str], active_views[0]["candidate_deployment_ids"])
    )
    assert active_views[0]["state_kind"] == test_case.expected_doctor_state_kind
    assert candidate_deployment_ids == (test_case.deployment_id,)
    assert repair_result["target_table_name"] == f"{target_table_name}__{test_case.deployment_id}"
    assert tuple(row[0] for row in repaired_rows) == test_case.expected_order_ids


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaLiveShadowWorkflowE2ETestCase(
            description="keeps the staged shadow path live while a staged deployment is open",
            deployment_id="20260410T002000Z_ab12cd",
            initial_order_ids=("order-1", "order-2"),
            live_order_ids=("order-3", "order-4", "order-5"),
            expected_final_order_ids=("order-1", "order-2", "order-3", "order-4", "order-5"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_open_staged_kafka_deployment_when_new_rows_arrive_then_shadow_path_stays_live(
    test_case: KafkaLiveShadowWorkflowE2ETestCase,
    isolated_e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    isolated_e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    isolated_e2e_clickhouse_client: Client,
    isolated_e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    scenario_result: KafkaLiveShadowScenarioResult = run_kafka_live_shadow_scenario(
        test_case=test_case,
        clickhouse_connection_settings=isolated_e2e_clickhouse_connection_settings,
        kafka_connection_settings=isolated_e2e_kafka_connection_settings,
        clickhouse_client=isolated_e2e_clickhouse_client,
        clickhouse_database=isolated_e2e_clickhouse_database,
        tmp_path=tmp_path,
    )

    assert scenario_result.deployment_id == test_case.deployment_id
    assert scenario_result.staged_order_ids == test_case.expected_final_order_ids
    assert tuple(row[0] for row in scenario_result.final_rows) == test_case.expected_final_order_ids


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaOffsetAuditWorkflowE2ETestCase(
            description="audits offset-mode staged deployment under live Kafka ingestion",
            deployment_id="20260410T004000Z_ef56gh",
            created_at=OFFSET_AUDIT_CREATED_AT,
            boundary_time=OFFSET_AUDIT_BOUNDARY_TIME,
            initial_order_ids=("order-1", "order-2"),
            live_order_ids=("order-3",),
            expected_audit_assessment=AuditAssessment.READY,
            expected_partitions_compared=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_offset_mode_staged_kafka_deployment_when_new_rows_arrive_then_audit_reports_catchup(
    test_case: KafkaOffsetAuditWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_dir: Path = prepare_authored_e2e_project(
        fixture_project_dir=E2E_KAFKA_OFFSET_PROJECT_DIR,
        tmp_path=tmp_path,
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        topic_suffix=e2e_clickhouse_database,
    )
    compiled_pipeline: CompiledPipeline = build_authored_greenfield_workflow_compiled_pipeline(
        project_dir=project_dir
    )
    e2e_clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=e2e_clickhouse_database,
        )
    )

    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
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
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=len(test_case.initial_order_ids),
    )

    run_sqltide_virtual_build_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )

    producer = build_kafka_producer(bootstrap_server=e2e_kafka_connection_settings.bootstrap_server)
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

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(compiled_pipeline).raw_table.name,
        expected_count=len(test_case.initial_order_ids) + len(test_case.live_order_ids),
    )

    audit_result: dict[str, object] = run_sqltide_deployment_audit_cli(
        project_dir=project_dir,
        host=e2e_clickhouse_connection_settings.host,
        port=e2e_clickhouse_connection_settings.port,
        username=e2e_clickhouse_connection_settings.username,
        password=e2e_clickhouse_connection_settings.password,
        database=e2e_clickhouse_database,
        deployment_id=test_case.deployment_id,
    )
    root_results: list[dict[str, object]] = cast(
        list[dict[str, object]], audit_result["root_results"]
    )
    root_result: dict[str, object] = root_results[0]
    offset_summary: dict[str, object] | None = cast(
        dict[str, object] | None, root_result["offset_catchup_summary"]
    )

    assert audit_result["assessment"] == test_case.expected_audit_assessment
    assert offset_summary is not None
    assert offset_summary["partitions_compared"] == test_case.expected_partitions_compared


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaSchemaChangeWorkflowE2ETestCase(
            description="bounded non-breaking schema change seeds the prefix and replays the tail",
            initial_pipeline_kind="base",
            changed_pipeline_kind="add_column",
            initial_deployment_id="20260410T005000Z_ab12cd",
            changed_deployment_id="20260410T005500Z_cd34ef",
            lookback_seconds=8,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_view_column_names=(
                "order_id",
                "_replay_timestamp",
                "kafka_topic",
            ),
            expected_selected_columns=("order_id", "kafka_topic"),
            expected_selected_rows=(
                ("frontier-order", "source.orders.created"),
                ("historical-order", ""),
            ),
            topic_row_indexes=(0,),
        ),
        KafkaSchemaChangeWorkflowE2ETestCase(
            description=(
                "bounded breaking remove-column change seeds the prefix and drops the removed "
                "column"
            ),
            initial_pipeline_kind="add_column",
            changed_pipeline_kind="remove_column",
            initial_deployment_id="20260410T006000Z_ab12cd",
            changed_deployment_id="20260410T006500Z_cd34ef",
            lookback_seconds=8,
            expected_execution_mode=RebuildExecutionMode.SEEDED_BOUNDED_REBUILD,
            expected_view_column_names=("order_id",),
            expected_selected_columns=("order_id",),
            expected_selected_rows=(
                ("frontier-order",),
                ("historical-order",),
            ),
        ),
        KafkaSchemaChangeWorkflowE2ETestCase(
            description="bounded breaking type change replays only the tail without seeding",
            initial_pipeline_kind="add_column",
            changed_pipeline_kind="type_change",
            initial_deployment_id="20260410T007000Z_ab12cd",
            changed_deployment_id="20260410T007500Z_cd34ef",
            lookback_seconds=8,
            expected_execution_mode=RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD,
            expected_view_column_names=(
                "order_id",
                "_replay_timestamp",
                "kafka_topic",
            ),
            expected_selected_columns=("order_id",),
            expected_selected_rows=(("frontier-order",),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_published_kafka_pipeline_when_schema_changes_then_bounded_policy_behaves_as_expected(
    test_case: KafkaSchemaChangeWorkflowE2ETestCase,
    e2e_clickhouse_connection_settings: E2EClickHouseConnectionSettings,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
    e2e_clickhouse_client: Client,
    e2e_clickhouse_database: str,
) -> None:
    initial_compiled_pipeline: CompiledPipeline = build_schema_change_workflow_compiled_pipeline(
        kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
        pipeline_kind=test_case.initial_pipeline_kind,
        topic_suffix=e2e_clickhouse_database,
    )
    changed_compiled_pipeline: CompiledPipeline = with_replay_on_change_policy(
        compiled_pipeline=build_schema_change_workflow_compiled_pipeline(
            kafka_broker_list=e2e_kafka_connection_settings.internal_bootstrap_server,
            pipeline_kind=test_case.changed_pipeline_kind,
            topic_suffix=e2e_clickhouse_database,
        ),
        breaking_mode=ReplayOnChangeMode.BOUNDED,
        breaking_lookback_seconds=test_case.lookback_seconds,
        non_breaking_mode=ReplayOnChangeMode.BOUNDED,
        non_breaking_lookback_seconds=test_case.lookback_seconds,
    )
    e2e_clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(initial_compiled_pipeline).kafka_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(initial_compiled_pipeline).raw_table,
            database=e2e_clickhouse_database,
        )
    )
    e2e_clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(initial_compiled_pipeline).materialized_view,
            database=e2e_clickhouse_database,
        )
    )

    producer: KafkaProducer = build_kafka_producer(
        bootstrap_server=e2e_kafka_connection_settings.bootstrap_server
    )
    historical_timestamp_ms: int
    frontier_timestamp_ms: int
    initial_created_at: str
    initial_boundary_time: str
    (
        historical_timestamp_ms,
        frontier_timestamp_ms,
        initial_created_at,
        initial_boundary_time,
    ) = build_bounded_replay_times(lookback_seconds=test_case.lookback_seconds)
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(initial_compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=(("historical-order", json.dumps({"order_id": "historical-order"})),),
            timestamp_ms=historical_timestamp_ms,
        )
    finally:
        producer.close()

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(initial_compiled_pipeline).raw_table.name,
        expected_count=1,
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    try:
        execute_backfill(
            request=build_greenfield_workflow_request(
                clickhouse_database=e2e_clickhouse_database,
                compiled_pipeline=initial_compiled_pipeline,
                deployment_id=test_case.initial_deployment_id,
                created_at=initial_created_at,
                boundary_time=initial_boundary_time,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.initial_deployment_id,
                metadata_database=e2e_clickhouse_database,
                default_database=e2e_clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    producer = build_kafka_producer(bootstrap_server=e2e_kafka_connection_settings.bootstrap_server)
    try:
        produce_kafka_messages(
            producer=producer,
            topic=require_managed_source(initial_compiled_pipeline).kafka_table.spec.kafka.topic,
            messages=(("frontier-order", json.dumps({"order_id": "frontier-order"})),),
            timestamp_ms=frontier_timestamp_ms,
        )
    finally:
        producer.close()

    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=require_managed_source(initial_compiled_pipeline).raw_table.name,
        expected_count=2,
    )
    changed_created_at: str
    changed_boundary_time: str
    changed_created_at = initial_created_at
    changed_boundary_time = initial_boundary_time

    managed_client = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    try:
        second_backfill_result: BackfillExecutionResult = execute_backfill(
            request=build_greenfield_workflow_request(
                clickhouse_database=e2e_clickhouse_database,
                compiled_pipeline=changed_compiled_pipeline,
                deployment_id=test_case.changed_deployment_id,
                created_at=changed_created_at,
                boundary_time=changed_boundary_time,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    expected_shadow_row_count: int = len(test_case.expected_selected_rows)
    wait_for_row_count(
        clickhouse_client=e2e_clickhouse_client,
        clickhouse_database=e2e_clickhouse_database,
        table_name=f"tbl__orders_enriched__{test_case.changed_deployment_id}",
        expected_count=expected_shadow_row_count,
    )

    managed_client = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=e2e_clickhouse_connection_settings.host,
            port=e2e_clickhouse_connection_settings.port,
            username=e2e_clickhouse_connection_settings.username,
            password=e2e_clickhouse_connection_settings.password,
            database=e2e_clickhouse_database,
        )
    )
    try:
        execute_publish(
            request=PublishRequest(
                deployment_id=test_case.changed_deployment_id,
                metadata_database=e2e_clickhouse_database,
                default_database=e2e_clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    selected_rows: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        "SELECT "
        + ", ".join(test_case.expected_selected_columns)
        + f" FROM {e2e_clickhouse_database}.tbl__orders_enriched ORDER BY order_id"
    ).result_rows
    described_columns: Sequence[Sequence[object]] = e2e_clickhouse_client.query(
        f"DESCRIBE TABLE {e2e_clickhouse_database}.tbl__orders_enriched"
    ).result_rows

    expected_selected_rows: list[tuple[object, ...]] = list(test_case.expected_selected_rows)
    actual_topic_name: str = require_managed_source(
        changed_compiled_pipeline
    ).kafka_table.spec.kafka.topic
    topic_row_index: int
    for topic_row_index in test_case.topic_row_indexes:
        expected_row: list[object] = list(expected_selected_rows[topic_row_index])
        expected_row[1] = actual_topic_name
        expected_selected_rows[topic_row_index] = tuple(expected_row)

    assert (
        second_backfill_result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode
        == test_case.expected_execution_mode
    )
    assert tuple(row[0] for row in described_columns) == test_case.expected_view_column_names
    assert selected_rows == expected_selected_rows
