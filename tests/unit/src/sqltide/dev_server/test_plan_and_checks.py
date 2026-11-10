import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import create_dev_app, validate_expected_build_scope
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
)
from sqltide.cli.build.constants import (
    EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR,
    EXPECTED_BUILD_READ_SCOPE_ENV_VAR,
    EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR,
)
from sqltide.cli.build.models import WorkflowPreparationOptions
from sqltide.cli.entry.constants import DEV_CLI_VARIABLES_ENV_VAR
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.classes.build_process import _build_environment, build_invocation
from sqltide.dev_server.classes.dev_server_state import DevServerState
from sqltide.dev_server.exceptions import DevConfigurationError
from sqltide.dev_server.models import DevExecutionContext
from sqltide.dev_server.types import ActivityTone, DevServerReporter
from sqltide.executor.observability.constants import RUN_DISPLAY_COMMAND_ENV_VAR
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    AuditBatchRouteTestCase,
    BuildConflictScopeTestCase,
    ChecksRunTestCase,
    ChecksStatusTestCase,
    DevRefactorTestCase,
    ModeAwarePlanTestCase,
    PlanEndpointTestCase,
    PlanSourceRecreationTestCase,
    RunEventsFeedTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    FakeAdapterConnection,
    FakeEmptyResultConnection,
    build_compile_callable,
    build_direct_plan_preparation,
    build_direct_source_drift_plan_preparation,
    build_direct_source_recreation_plan_preparation,
    build_fake_state_connection,
    build_mixed_plan_preparation,
    build_state_test_client,
    build_virtual_plan_preparation,
    write_dev_server_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlanEndpointTestCase(
            description="plans the selected model closure against the fake warehouse",
            selectors=("orders_clean",),
            expected_status=200,
            expected_entry_names=("orders_clean",),
            expected_command="sqt build --select orders_clean",
            expected_replay_root_rows=(None,),
            expected_sql_changes=("baseline_unavailable",),
        ),
        PlanEndpointTestCase(
            description="expands a pipeline selector to its models",
            selectors=("pipeline:order_events",),
            expected_status=200,
            expected_entry_names=("orders_clean",),
            expected_command="sqt build --select pipeline:order_events",
            expected_replay_root_rows=(None,),
            expected_sql_changes=("baseline_unavailable",),
        ),
        PlanEndpointTestCase(
            description="rejects an unknown selector with a clear message",
            selectors=("nonsense+",),
            expected_status=400,
            expected_entry_names=(),
            expected_command="",
            expected_replay_root_rows=(),
            expected_sql_changes=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_selectors_when_planning_then_returns_expected_plan(
    test_case: PlanEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)
    params: list[tuple[str, str]] = [("select", selector) for selector in test_case.selectors]

    response: object = client.get("/api/plan", params=params)

    assert response.status_code == test_case.expected_status
    payload_entries: tuple = tuple(
        entry["modelName"] for entry in response.json().get("entries", ())
    )
    assert payload_entries == test_case.expected_entry_names
    assert response.json().get("command", "") == test_case.expected_command
    replay_root_rows: tuple = tuple(
        root["rowsToReplay"] for root in response.json().get("replayRoots", ())
    )
    assert replay_root_rows == test_case.expected_replay_root_rows
    sql_changes: tuple = tuple(
        entry["sqlChange"]["status"] for entry in response.json().get("entries", ())
    )
    assert sql_changes == test_case.expected_sql_changes


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="direct plan exposes one immediate execution phase",
            expected_value=["direct"],
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_pipeline_when_planning_then_returns_direct_execution_phase(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/plan", params={"select": "orders_clean"})

    assert response.status_code == 200
    assert response.json()["mode"] == "direct"
    assert response.json()["executionOrder"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        ModeAwarePlanTestCase(
            description="virtual plan exposes one staged phase",
            mode="virtual",
            expected_execution_order=("virtual",),
            preparation_builder=build_virtual_plan_preparation,
        ),
        ModeAwarePlanTestCase(
            description="mixed plan exposes virtual then direct phases",
            mode="mixed",
            expected_execution_order=("virtual", "direct"),
            preparation_builder=build_mixed_plan_preparation,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_mode_aware_preparation_when_planning_then_returns_exact_execution_phases(
    test_case: ModeAwarePlanTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)
    deployment_id: str = "20260811T120000Z_plan"
    preparation: object = test_case.preparation_builder(deployment_id)

    with patch(
        "sqltide._native.prepare_build_workflow",
        return_value=preparation,
    ) as prepare_build:
        response: NativeResponse = client.get(
            "/api/plan",
            params={
                "select": "orders_clean",
                "start": "2026-08-01T12:00:00Z",
                "deployment": deployment_id,
            },
        )

    payload: dict = response.json()
    options: object = prepare_build.call_args.kwargs["options"]
    assert isinstance(options, WorkflowPreparationOptions)
    assert response.status_code == 200
    assert payload["mode"] == test_case.mode
    assert tuple(payload["executionOrder"]) == test_case.expected_execution_order
    assert tuple(phase["mode"] for phase in payload["phases"]) == (
        test_case.expected_execution_order
    )
    assert payload["deploymentId"] == deployment_id
    assert payload["command"].endswith(f"--deployment-id {deployment_id}")
    assert payload["upperBoundary"] == {
        "mode": "captured_at_execution",
        "continuesLive": True,
    }
    assert payload["phases"][0]["contextModelNames"] == ["orders"]
    assert payload["phases"][0]["actions"][0]["logicalName"] == "orders_summary"
    assert payload["warnings"][0]["relatedModel"] == "orders_summary"
    assert payload["sourceRecreations"] == []
    assert options.deployment_id == deployment_id


@pytest.mark.parametrize(
    "test_case",
    [
        PlanSourceRecreationTestCase(
            description="drifted managed source recreation is shown on the plan",
            preparation_builder=build_direct_source_recreation_plan_preparation,
            expected_warning_code="managed_source_recreated",
            expected_message_fragments=(
                "kafka__orders and mv__orders are recreated with the declared settings "
                "(kafka_topic_list)",
                "raw__orders and its rows are kept",
                "committed offsets of consumer group 'sqltide_orders_analytics'",
            ),
            expected_source_recreation={
                "sourceName": "orders",
                "relationName": "kafka__orders",
                "landingViewName": "mv__orders",
                "landingRelationName": "raw__orders",
                "stagedRelationName": "_sqltide_next__kafka__orders",
                "consumerGroup": "sqltide_orders_analytics",
                "driftedSettings": ["kafka_topic_list"],
                "selectedPipeline": None,
                "otherReaderPipelines": [],
            },
            expected_actions=(
                ("preparation", "stage", "orders", "_sqltide_next__kafka__orders"),
                ("preparation", "swap", "orders", "kafka__orders"),
                ("preparation", "retire", "orders", "_sqltide_next__kafka__orders"),
                ("creation", "create", "orders_clean", "tbl__orders_clean"),
            ),
            expected_entry_reasons=("selected",),
        ),
        PlanSourceRecreationTestCase(
            description="a changed selection names the pipeline it rebuilds for the source",
            preparation_builder=build_direct_source_drift_plan_preparation,
            expected_warning_code="managed_source_recreated",
            expected_message_fragments=("raw__orders and its rows are kept",),
            expected_source_recreation={
                "sourceName": "orders",
                "selectedPipeline": "order_events",
                "otherReaderPipelines": ["order_audit"],
            },
            expected_actions=(
                ("preparation", "stage", "orders", "_sqltide_next__kafka__orders"),
                ("preparation", "swap", "orders", "kafka__orders"),
                ("preparation", "retire", "orders", "_sqltide_next__kafka__orders"),
                ("creation", "create", "orders_clean", "tbl__orders_clean"),
            ),
            expected_entry_reasons=("source_drift",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_source_recreation_when_planning_then_plan_describes_it(
    test_case: PlanSourceRecreationTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    with patch(
        "sqltide._native.prepare_build_workflow",
        return_value=test_case.preparation_builder(),
    ):
        response: NativeResponse = client.get("/api/plan", params={"select": "orders_clean"})

    payload: dict = response.json()
    warnings: list[dict] = payload["warnings"]
    message: str = warnings[0]["message"]
    recreation: dict = payload["sourceRecreations"][0]
    actions: list[dict] = payload["phases"][0]["actions"]
    assert response.status_code == 200
    assert [warning["code"] for warning in warnings] == [test_case.expected_warning_code]
    assert all(fragment in message for fragment in test_case.expected_message_fragments)
    assert len(payload["sourceRecreations"]) == 1
    assert {key: recreation[key] for key in test_case.expected_source_recreation} == (
        test_case.expected_source_recreation
    )
    assert recreation["message"] == message
    assert tuple(
        (action["phase"], action["action"], action["logicalName"], action["physicalName"])
        for action in actions
    ) == (test_case.expected_actions)
    assert tuple(entry["reason"] for entry in payload["entries"]) == (
        test_case.expected_entry_reasons
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="plan exposes the protected pipeline operator gate",
            expected_value=[
                {
                    "pipelineName": "order_events",
                    "warning": "Interrupts protected order events.",
                    "confirmation": "DEPLOY_ORDER_EVENTS",
                }
            ],
        )
    ],
    ids=lambda case: case.description,
)
def test_given_protected_pipeline_when_planning_then_returns_confirmation_requirement(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    (tmp_path / "pipelines" / "order_events" / "pipeline.toml").write_text(
        """
mode = "direct"

[protection]
warning = "Interrupts protected order events."
confirmation = "DEPLOY_ORDER_EVENTS"
""".strip(),
        encoding="utf-8",
    )
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    response: object = client.get("/api/plan", params={"select": "orders_clean"})

    assert response.status_code == 200
    assert response.json()["protections"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="preview hides the dev context retained by the build process",
            expected_value=("local", "analytics"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_resolved_dev_context_when_planning_then_preview_and_build_command_share_it(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    context: DevExecutionContext = DevExecutionContext(
        database="analytics",
        selected_target="local",
        cli_variables=(
            ("region", "eu"),
            ("batch_size", 50),
            ("warehouse_password", "variable-secret"),
        ),
        environment={"PATH": "/bin"},
        connection_host="clickhouse.internal",
        connection_port=8124,
        connection_username="builder",
        connection_password="secret-value",
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=build_fake_state_connection(),
            database="analytics",
            project_dir=tmp_path,
            execution_context=context,
        )
    )

    response: object = client.get("/api/plan", params={"select": "orders_clean"})
    expected_argv, expected_command = build_invocation(
        selectors=("orders_clean",), start_time=None, execution_context=context
    )

    assert response.status_code == 200
    assert response.json()["command"] == expected_command
    assert (context.selected_target, context.database) == test_case.expected_value
    assert shlex.split(expected_command) == ["sqt", "build", "--select", "orders_clean"]
    assert expected_argv[1:7] == [
        "build",
        "--target",
        "local",
        "--database",
        "analytics",
        "--select",
    ]
    assert "--target" not in expected_command
    assert "--database" not in expected_command
    assert "secret-value" not in expected_command
    assert "variable-secret" not in expected_command
    assert all("variable-secret" not in argument for argument in expected_argv)
    child_environment: dict[str, str] = _build_environment(
        execution_context=context,
        display_command=expected_command,
    )
    assert json.loads(child_environment[DEV_CLI_VARIABLES_ENV_VAR]) == {
        "batch_size": 50,
        "region": "eu",
        "warehouse_password": "variable-secret",
    }
    assert child_environment["SQLTIDE_CLICKHOUSE_HOST"] == "clickhouse.internal"
    assert child_environment["SQLTIDE_CLICKHOUSE_PORT"] == "8124"
    assert child_environment["SQLTIDE_CLICKHOUSE_USERNAME"] == "builder"
    assert child_environment["SQLTIDE_CLICKHOUSE_PASSWORD"] == "secret-value"
    assert child_environment[RUN_DISPLAY_COMMAND_ENV_VAR] == expected_command


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="changed plan flags survive the HTTP boundary",
            expected_value="sqt build --changed --include-missing-upstream",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_changed_plan_flags_when_planning_then_route_and_command_preserve_them(
    tmp_path: Path, test_case: DevRefactorTestCase
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    fingerprints: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
        status="available",
        baselines=(
            AdapterDirectFingerprintRecord(
                fingerprint_id="orders-old",
                logical_model_identity="analytics.orders_clean",
                definition_sql="SELECT 0",
                definition_hash="outdated",
                identity_metadata="{}",
                workflow_id="previous",
                tool_version="0.32.0",
            ),
        ),
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(fingerprints=fingerprints),
            database="analytics",
            project_dir=tmp_path,
        )
    )

    response: NativeResponse = client.get(
        "/api/plan",
        params={"changed": "true", "include_missing_upstream": "true"},
    )

    assert response.status_code == 200
    assert response.json()["entries"][0]["reason"] == "changed"
    assert response.json()["command"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="changed build flags survive the HTTP boundary",
            expected_value=(
                True,
                True,
                frozenset({"model:orders_clean"}),
                frozenset({"source:orders"}),
                frozenset({"model:orders_clean=order_events"}),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_changed_build_flags_when_starting_then_process_receives_them(
    tmp_path: Path, test_case: DevRefactorTestCase
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    fingerprints: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
        status="available",
        baselines=(
            AdapterDirectFingerprintRecord(
                fingerprint_id="orders-old",
                logical_model_identity="analytics.orders_clean",
                definition_sql="SELECT 0",
                definition_hash="outdated",
                identity_metadata="{}",
                workflow_id="previous",
                tool_version="0.32.0",
            ),
        ),
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(fingerprints=fingerprints),
            database="analytics",
            project_dir=tmp_path,
        )
    )
    with (
        patch(
            "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
            return_value=[],
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.start",
            return_value={"invocationId": "new-run", "status": "starting"},
        ) as start_build,
    ):
        response: NativeResponse = client.post(
            "/api/build",
            json={"changed": True, "includeMissingUpstream": True},
        )

    assert response.status_code == 200
    assert (
        start_build.call_args.kwargs["changed"],
        start_build.call_args.kwargs["include_missing_upstream"],
        start_build.call_args.kwargs["expected_write_scope"],
        start_build.call_args.kwargs["expected_read_scope"],
        start_build.call_args.kwargs["expected_pipeline_scope"],
    ) == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="child replanning cannot exceed the server-authorized scope",
            expected_value="Build scope changed after server authorization",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_authorized_scope_when_child_plan_changes_then_validation_is_rejected(
    test_case: DevRefactorTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()
    monkeypatch.setenv(
        EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR,
        json.dumps(["model:orders_clean"]),
    )
    monkeypatch.setenv(EXPECTED_BUILD_READ_SCOPE_ENV_VAR, json.dumps([]))
    monkeypatch.setenv(
        EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR,
        json.dumps(["model:orders_clean=other_pipeline"]),
    )

    with pytest.raises(CliUserError) as rejection:
        validate_expected_build_scope(
            preparation=build_direct_plan_preparation(),
            analysis=analysis,
        )

    assert str(test_case.expected_value) in str(rejection.value)


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="authorized scopes are serialized into the child environment",
            expected_value=(
                ["model:orders_clean"],
                ["source:orders"],
                ["model:orders_clean=order_events"],
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_authorized_scopes_when_building_environment_then_child_receives_them(
    test_case: DevRefactorTestCase,
) -> None:
    environment: dict[str, str] = _build_environment(
        execution_context=None,
        expected_write_scope=frozenset({"model:orders_clean"}),
        expected_read_scope=frozenset({"source:orders"}),
        expected_pipeline_scope=frozenset({"model:orders_clean=order_events"}),
    )

    assert (
        json.loads(environment[EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR]),
        json.loads(environment[EXPECTED_BUILD_READ_SCOPE_ENV_VAR]),
        json.loads(environment[EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR]),
    ) == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="build invocation passes every protection confirmation",
            expected_value=("DEPLOY_ORDERS", "DEPLOY_PRICES"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_protection_confirmations_when_building_invocation_then_passes_each_value(
    test_case: DevRefactorTestCase,
) -> None:
    confirmations: tuple[str, ...] = cast(tuple[str, ...], test_case.expected_value)
    argv, command = build_invocation(
        selectors=("orders_clean",),
        start_time=None,
        execution_context=None,
        confirmations=confirmations,
    )

    assert argv[-6:] == [
        "--confirm",
        "DEPLOY_ORDERS",
        "--confirm",
        "DEPLOY_PRICES",
        "--auto-approve",
        "--events",
    ]
    assert "--confirm DEPLOY_ORDERS" in command
    assert "--confirm DEPLOY_PRICES" in command


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="planned deployment is passed to process launch",
            expected_value="20260811T120000Z_plan",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_planned_deployment_when_starting_build_then_launches_same_deployment(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)
    deployment_id: str = str(test_case.expected_value)
    with (
        patch(
            "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
            return_value=[],
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.start",
            return_value={"invocationId": "new-run", "status": "starting"},
        ) as start_build,
    ):
        response: NativeResponse = client.post(
            "/api/build",
            json={
                "selectors": ["orders_clean"],
                "deploymentId": deployment_id,
            },
        )

    assert response.status_code == 200
    assert start_build.call_args.kwargs["deployment_id"] == deployment_id


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="start time without selection matches CLI validation",
            expected_value="--start-time requires --changed or at least one --select",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_start_time_without_selection_when_planning_then_matches_cli_validation(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    response: object = client.get("/api/plan", params={"start": "2026-08-01"})

    assert response.status_code == 400
    assert response.json()["detail"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="non-UTC replay count keeps normalized UTC start",
            expected_value="2026-08-01 12:00:00.000",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_non_utc_warehouse_when_planning_then_replay_count_keeps_utc_start(
    test_case: DevRefactorTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    connection: FakeAdapterConnection = build_fake_state_connection()
    connection._catalog = replace(connection._catalog, warehouse_timezone="America/New_York")
    captured: dict[str, object] = {}

    def fake_count_replay_rows(**kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        return {"orders_clean": 1}

    monkeypatch.setattr(
        "sqltide.dev_server._helpers.payloads.plan_payload.count_replay_rows",
        fake_count_replay_rows,
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=connection,
            database="analytics",
            project_dir=tmp_path,
        )
    )

    structural: object = client.get(
        "/api/plan",
        params={"select": "orders_clean", "start": "2026-08-01T12:00:00Z"},
    )
    assert structural.status_code == 200
    assert structural.json()["replayRoots"][0]["rowsToReplay"] is None
    assert captured == {}

    response: object = client.get(
        "/api/plan",
        params={
            "select": "orders_clean",
            "start": "2026-08-01T12:00:00Z",
            "counts": "true",
        },
    )

    assert response.status_code == 200
    assert response.json()["replayWindow"]["startTime"] == test_case.expected_value
    assert captured["start_time"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="mismatched retained database is rejected",
            expected_value="does not match",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mismatched_database_context_when_creating_app_then_it_is_rejected(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))

    with pytest.raises(DevConfigurationError) as captured_error:
        create_dev_app(
            state=state,
            database="analytics",
            execution_context=DevExecutionContext(database="other"),
        )
    assert str(test_case.expected_value) in str(captured_error.value)


@pytest.mark.parametrize(
    "test_case",
    [
        ChecksRunTestCase(
            description="runs one audit and reports a pass on zero failing rows",
            kind="audit",
            name="orders_clean.order_id.not_null.1",
            expected_status=200,
            expected_passed=True,
        ),
        ChecksRunTestCase(
            description="rejects an unknown audit name",
            kind="audit",
            name="missing_audit",
            expected_status=400,
            expected_passed=False,
        ),
        ChecksRunTestCase(
            description="rejects an unsupported check kind",
            kind="lint",
            name="anything",
            expected_status=400,
            expected_passed=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_check_request_when_running_then_returns_expected_result(
    test_case: ChecksRunTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    connection: FakeEmptyResultConnection = FakeEmptyResultConnection(
        catalog=build_fake_state_connection()._catalog,
        results_by_query={},
        warehouse_timestamp="2026-08-03 12:00:00.000",
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state, connection=connection, database="analytics", project_dir=tmp_path
        )
    )

    response: object = client.post(
        "/api/checks/run", json={"kind": test_case.kind, "name": test_case.name}
    )

    assert response.status_code == test_case.expected_status
    assert response.json().get("passed", False) is test_case.expected_passed


@pytest.mark.parametrize(
    "test_case",
    [
        AuditBatchRouteTestCase(
            description="runs all requested audits through the batch route",
            names=("orders_clean.order_id.not_null.1",),
            expected_name="orders_clean.order_id.not_null.1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_audit_names_when_running_batch_then_returns_all_results(
    test_case: AuditBatchRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    connection: FakeEmptyResultConnection = FakeEmptyResultConnection(
        catalog=build_fake_state_connection()._catalog,
        results_by_query={},
        warehouse_timestamp="2026-08-03 12:00:00.000",
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state, connection=connection, database="analytics", project_dir=tmp_path
        )
    )

    response: object = client.post("/api/audits/run", json={"names": list(test_case.names)})

    assert response.status_code == 200
    assert response.json()[0]["name"] == test_case.expected_name
    assert response.json()[0]["passed"] is True


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="warming audit is narrated as deferred rather than failed",
            expected_value="deferred",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_warming_audit_when_run_from_ui_then_activity_is_deferred(
    test_case: DevRefactorTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    runner: MagicMock = MagicMock(
        return_value={
            "name": "orders_clean.order_id.not_null.1",
            "kind": "audit",
            "passed": False,
            "deferredUntil": "2026-08-03 12:15:00.000",
        }
    )
    reporter_mock: MagicMock = MagicMock()
    monkeypatch.setattr("sqltide.dev_server._helpers.server.checks_execution.run_one_audit", runner)
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=tmp_path)),
            connection=build_fake_state_connection(),
            database="analytics",
            project_dir=tmp_path,
            reporter=cast(DevServerReporter, reporter_mock),
        )
    )

    response: object = client.post(
        "/api/checks/run",
        json={"kind": "audit", "name": "orders_clean.order_id.not_null.1"},
    )

    assert response.status_code == 200
    assert reporter_mock.report_activity.call_args.kwargs["status"] == test_case.expected_value
    assert reporter_mock.report_activity.call_args.kwargs["tone"] == ActivityTone.CAUTION


@pytest.mark.parametrize(
    "test_case",
    [
        ChecksStatusTestCase(
            description="maps recorded node history back to audit names",
            expected_name="orders_clean.order_id.not_null.1",
            expected_status="passed",
            expected_failure_count=0,
            expected_completed_at="2026-08-03 09:00:00.000",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_recorded_history_when_reading_checks_status_then_maps_names(
    test_case: ChecksStatusTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    payload: list = client.get("/api/checks/status").json()

    status: dict = payload[0]
    assert status["name"] == test_case.expected_name
    assert status["status"] == test_case.expected_status
    assert status["failureCount"] == test_case.expected_failure_count
    assert status["completedAt"] == test_case.expected_completed_at
    assert status["payload"]["sample_column_names"] == ["order_id"]


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="unmaterialized audit is explicitly deferred in Quality",
            expected_value={
                "status": "deferred",
                "missingRelations": ["orders_clean"],
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unmaterialized_relation_when_reading_quality_then_missing_relation_is_reported(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    with (
        patch(
            "sqltide.dev_server._helpers.server.checks_execution.load_model_anchors",
            return_value={},
        ),
        patch(
            "sqltide.dev_server._helpers.server.checks_execution.load_materialized_model_names",
            return_value=frozenset(),
        ),
    ):
        payload: list[dict[str, object]] = client.get("/api/checks/status").json()

    expected: dict[str, object] = cast(dict[str, object], test_case.expected_value)
    assert {key: payload[0][key] for key in expected} == expected


@pytest.mark.parametrize(
    "test_case",
    [
        RunEventsFeedTestCase(
            description="serves one recorded run timeline with parsed payloads",
            invocation_id="inv-42",
            expected_event_kinds=("run_started", "statement_completed"),
            expected_written_rows=42,
            expected_executed_logical_ids=("model:orders",),
            expected_context_logical_ids=("source:order_events",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_recorded_run_when_reading_events_then_returns_ordered_timeline(
    test_case: RunEventsFeedTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    payload: dict[str, object] = client.get(f"/api/runs/{test_case.invocation_id}/events").json()
    events: list[dict[str, object]] = payload["events"]

    assert payload["found"] is True
    assert tuple(event["event"] for event in events) == test_case.expected_event_kinds
    assert events[1]["writtenRows"] == test_case.expected_written_rows
    assert events[1]["stepId"] == "replay_orders"
    assert events[0]["executedLogicalIds"] == list(test_case.expected_executed_logical_ids)
    assert events[0]["contextLogicalIds"] == list(test_case.expected_context_logical_ids)


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="unresponsive build conflict explains the safety window",
            expected_value=(
                "Run stale-run is unresponsive: no signal for 550s. No new run was started. "
                "To prevent overlapping warehouse writes, SQLTide will wait 50s more "
                "before treating it as presumed failed (configured safety window: 600s via "
                "defaults.run_presumed_failed_after). Retry after that."
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unresponsive_run_when_starting_build_then_conflict_explains_retry(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)
    with patch(
        "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
        return_value=[
            {
                "invocationId": "stale-run",
                "status": "unresponsive",
                "lastSignalAgeSeconds": 550,
            }
        ],
    ):
        response: NativeResponse = client.post("/api/build", json={"selectors": ["orders_clean"]})

    assert response.status_code == 409
    assert response.json()["detail"] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        BuildConflictScopeTestCase(
            description="disjoint active build launches",
            executed_logical_ids=("model:inventory_clean",),
            context_logical_ids=("source:inventory",),
            expected_status=200,
            expected_started=True,
            expected_detail_fragment="new-run",
        ),
        BuildConflictScopeTestCase(
            description="overlapping active model blocks",
            executed_logical_ids=("model:orders_clean",),
            context_logical_ids=(),
            expected_status=409,
            expected_started=False,
            expected_detail_fragment="Run active-run is still running",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_active_run_scope_when_starting_build_then_only_overlap_conflicts(
    test_case: BuildConflictScopeTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)
    launch_payload: dict[str, object] = {
        "invocationId": "new-run",
        "command": "sqt build --select orders_clean",
        "status": "starting",
    }
    with (
        patch(
            "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
            return_value=[
                {
                    "invocationId": "active-run",
                    "status": "running",
                    "lastSignalAgeSeconds": 10,
                    "executedLogicalIds": list(test_case.executed_logical_ids),
                    "contextLogicalIds": list(test_case.context_logical_ids),
                }
            ],
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.start",
            return_value=launch_payload,
        ) as start_build,
    ):
        response: NativeResponse = client.post("/api/build", json={"selectors": ["orders_clean"]})

    assert response.status_code == test_case.expected_status
    assert start_build.called is test_case.expected_started
    assert test_case.expected_detail_fragment in response.text


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="build above pipeline limit rejects before process launch",
            expected_value="Build affects 2 pipelines, exceeding max_pipelines=1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_build_exceeding_pipeline_limit_when_starting_then_rejects_before_launch(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    (tmp_path / "pipelines" / "second_pipeline").mkdir()
    (tmp_path / "pipelines" / "second_pipeline" / "second_model.sql").write_text(
        'MODEL (order_by ["order_id"]);\n'
        'SELECT order_id::String AS order_id FROM __source("orders")\n',
        encoding="utf-8",
    )
    project_path: Path = tmp_path / "sqltide_project.toml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8") + "\n[build]\nmax_pipelines = 1\n",
        encoding="utf-8",
    )
    client: NativeTestClient = build_state_test_client(project_dir=tmp_path)

    with patch("sqltide.dev_server.classes.build_process.BuildProcessManager.start") as start_build:
        response: NativeResponse = client.post("/api/build", json={"selectors": []})

    assert response.status_code == 400
    assert str(test_case.expected_value) in response.text
    start_build.assert_not_called()
