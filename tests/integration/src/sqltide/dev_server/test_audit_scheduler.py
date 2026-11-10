from collections.abc import Sequence
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import (
    AuditScheduler,
    DevServerState,
    SilentDevServerReporter,
    analyze_project,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.classes.build_process import BuildProcessManager
from sqltide.dev_server.models import DevExecutionContext
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    execute_rendered_statements,
)
from tests.integration.src.sqltide.cli.helpers import (
    KEYED_ORDER_ITEMS_COLUMNS,
    KEYED_ORDER_ITEMS_ORDER_BY,
    build_managed_clickhouse_client,
    build_order_items_ddl,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.dev_server._test_types import (
    ScheduledAuditOutcomeTestCase,
    ScheduledAuditWarehouseTestCase,
)
from tests.integration.src.sqltide.dev_server.helpers import write_scheduled_audit_project


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ScheduledAuditWarehouseTestCase(
            description="due audit executes once and persists cadence plus live run evidence",
            expected_first_tick_count=1,
            expected_later_tick_count=0,
            expected_status="warning",
            expected_outcome="succeeded",
            expected_event_kinds=(
                "run_started",
                "audit_started",
                "run_heartbeat",
                "audit_completed",
                "run_completed",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_real_clickhouse_when_scheduler_ticks_then_slot_executes_once_with_run_evidence(
    test_case: ScheduledAuditWarehouseTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_project: LoadedProject = write_scheduled_audit_project(
        project_dir=tmp_path,
        database=clickhouse_database,
    )
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    clickhouse_client.command(
        build_order_items_ddl(
            database=clickhouse_database,
            columns=KEYED_ORDER_ITEMS_COLUMNS,
            order_by=KEYED_ORDER_ITEMS_ORDER_BY,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.tbl__order_items",
        data=[["ord_001", -5.0]],
        column_names=["order_id", "line_total"],
    )
    execution_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    observation_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    execute_rendered_statements(
        client=clickhouse_client,
        statements=execution_connection.render_migrate_metadata_state(clickhouse_database),
    )
    monkeypatch.setattr(
        "sqltide.executor.observability.classes.run_event_sink.HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    state: DevServerState = DevServerState(run_compile=lambda: analysis)
    builds: BuildProcessManager = BuildProcessManager(
        reporter=SilentDevServerReporter(),
        execution_context=DevExecutionContext(database=clickhouse_database),
    )
    scheduler: AuditScheduler = AuditScheduler(
        state=state,
        connection=execution_connection,
        observation_connection=observation_connection,
        database=clickhouse_database,
        project_dir=tmp_path,
        builds=builds,
    )

    try:
        first_count: int = scheduler.tick()
        second_count: int = scheduler.tick()
        restarted_scheduler: AuditScheduler = AuditScheduler(
            state=state,
            connection=execution_connection,
            observation_connection=observation_connection,
            database=clickhouse_database,
            project_dir=tmp_path,
            builds=builds,
        )
        restarted_count: int = restarted_scheduler.tick()
    finally:
        scheduler.close()
        builds.close()
        observation_connection.close()
        execution_connection.close()

    result_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT trigger, status, isNotNull(scheduled_for) "
        f"FROM {clickhouse_database}._sqltide_node_results"
    ).result_rows
    invocation_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT mode, outcome FROM {clickhouse_database}._sqltide_invocations"
    ).result_rows
    event_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT event_kind FROM {clickhouse_database}._sqltide_run_events ORDER BY sequence"
    ).result_rows

    assert first_count == test_case.expected_first_tick_count
    assert second_count == test_case.expected_later_tick_count
    assert restarted_count == test_case.expected_later_tick_count
    assert tuple(result_rows[0]) == ("scheduled", test_case.expected_status, 1)
    assert tuple(invocation_rows[0]) == ("scheduled", test_case.expected_outcome)
    actual_event_kinds: tuple[str, ...] = tuple(str(row[0]) for row in event_rows)
    event_positions: tuple[int, ...] = tuple(
        actual_event_kinds.index(event_kind) for event_kind in test_case.expected_event_kinds
    )
    assert event_positions == tuple(sorted(event_positions))


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ScheduledAuditOutcomeTestCase(
            description="passing audit succeeds scheduled invocation",
            severity="warning",
            audit_query=('SELECT order_id FROM __ref("order_items") WHERE line_total >= 0'),
            expected_status="passed",
            expected_outcome="succeeded",
        ),
        ScheduledAuditOutcomeTestCase(
            description="warning violation succeeds scheduled invocation",
            severity="warning",
            audit_query='SELECT order_id FROM __ref("order_items") WHERE line_total < 0',
            expected_status="warning",
            expected_outcome="succeeded",
        ),
        ScheduledAuditOutcomeTestCase(
            description="error violation fails scheduled invocation",
            severity="error",
            audit_query='SELECT order_id FROM __ref("order_items") WHERE line_total < 0',
            expected_status="failed",
            expected_outcome="failed",
        ),
        ScheduledAuditOutcomeTestCase(
            description="audit SQL error fails scheduled invocation",
            severity="error",
            audit_query='SELECT missing_column FROM __ref("order_items")',
            expected_status="error",
            expected_outcome="failed",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_outcome_when_scheduler_ticks_then_cadence_attempt_is_persisted(
    test_case: ScheduledAuditOutcomeTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    loaded_project: LoadedProject = write_scheduled_audit_project(
        project_dir=tmp_path,
        database=clickhouse_database,
        severity=test_case.severity,
        audit_query=test_case.audit_query,
    )
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    clickhouse_client.command(
        build_order_items_ddl(
            database=clickhouse_database,
            columns=KEYED_ORDER_ITEMS_COLUMNS,
            order_by=KEYED_ORDER_ITEMS_ORDER_BY,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.tbl__order_items",
        data=[["ord_001", -5.0]],
        column_names=["order_id", "line_total"],
    )
    execution_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    execute_rendered_statements(
        client=clickhouse_client,
        statements=execution_connection.render_migrate_metadata_state(clickhouse_database),
    )
    state: DevServerState = DevServerState(run_compile=lambda: analysis)
    builds: BuildProcessManager = BuildProcessManager(
        reporter=SilentDevServerReporter(),
        execution_context=DevExecutionContext(database=clickhouse_database),
    )
    scheduler: AuditScheduler = AuditScheduler(
        state=state,
        connection=execution_connection,
        database=clickhouse_database,
        project_dir=tmp_path,
        builds=builds,
    )

    try:
        result_count: int = scheduler.tick()
    finally:
        scheduler.close()
        builds.close()
        execution_connection.close()

    result_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT status, isNotNull(scheduled_for) FROM {clickhouse_database}._sqltide_node_results"
    ).result_rows
    invocation_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT outcome FROM {clickhouse_database}._sqltide_invocations"
    ).result_rows

    assert result_count == 1
    assert tuple(result_rows[0]) == (test_case.expected_status, 1)
    assert tuple(invocation_rows[0]) == (test_case.expected_outcome,)
