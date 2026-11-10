from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest

from sqltide._native import DevApplication, DevServerState, create_dev_app
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.dev_server.models import DevExecutionContext
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    BootstrapAuthorizationTestCase,
    BootstrapEndpointTestCase,
    CapabilitiesTestCase,
    DevAppLifespanTestCase,
    ReadConnectionRouteTestCase,
    ReloadAuthorizationTestCase,
    SnapshotRefreshPerformanceTestCase,
    StateRouteErrorTestCase,
    StatusEndpointTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    break_project_compile,
    build_assigned_proxy_message_client,
    build_assigned_proxy_quality_client,
    build_assigned_proxy_reload_client,
    build_compile_callable,
    build_fake_state_connection,
    build_test_client,
    write_dev_server_project,
    write_reload_access_policy,
)

_PROXY_PROOF_HEADERS: dict[str, str] = {"X-SQLTide-CSRF": "trusted-proxy"}


@pytest.mark.parametrize(
    "test_case",
    [
        BootstrapEndpointTestCase(
            description="returns authentication and held project state in one response",
            expected_auth_mode="disabled",
            expected_compile_state="ok",
            expected_has_definitions=True,
            expected_has_state=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_project_when_reading_bootstrap_then_one_payload_initializes_the_ui(
    test_case: BootstrapEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/bootstrap")
    payload: dict[str, object] = response.json()
    auth: dict[str, object] = cast(dict[str, object], payload["auth"])
    config: dict[str, object] = cast(dict[str, object], auth["config"])
    status: dict[str, object] = cast(dict[str, object], payload["status"])
    compile_status: dict[str, object] = cast(dict[str, object], status["compile"])

    assert response.status_code == 200
    assert config["mode"] == test_case.expected_auth_mode
    assert compile_status["state"] == test_case.expected_compile_state
    assert (payload["definitions"] is not None) is test_case.expected_has_definitions
    assert (payload["state"] is not None) is test_case.expected_has_state
    assert auth["session"] is not None
    assert auth["capabilities"] is not None


@pytest.mark.parametrize(
    "test_case",
    [
        ReadConnectionRouteTestCase(
            description="run history avoids the primary execution connection",
            path="/api/runs",
            expected_status=200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_isolated_read_factory_when_reading_route_then_primary_connection_is_avoided(
    test_case: ReadConnectionRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    primary_mock: MagicMock = MagicMock()
    read_mock: MagicMock = MagicMock()
    create_read: MagicMock = MagicMock(return_value=cast(AdapterConnection, read_mock))
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=cast(AdapterConnection, primary_mock),
            database="analytics",
            project_dir=tmp_path,
            execution_context=DevExecutionContext(
                database="analytics",
                observation_connection_factory=cast(Callable[[], AdapterConnection], create_read),
            ),
        )
    )

    with patch("sqltide._native.read_runs", return_value=[]) as read_runs_mock:
        response: NativeResponse = client.get(test_case.path)

    assert response.status_code == test_case.expected_status
    assert create_read.call_count == 1
    assert read_runs_mock.call_args.kwargs["connection"] is read_mock
    read_mock.close.assert_called_once()
    primary_mock.query.assert_not_called()


@pytest.mark.parametrize(
    "test_case",
    [
        BootstrapAuthorizationTestCase(
            description="rejects bootstrap when the trusted proxy supplies no identity",
            expected_status=401,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_identity_when_reading_bootstrap_then_project_data_is_not_exposed(
    test_case: BootstrapAuthorizationTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_reload_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/bootstrap")

    assert response.status_code == test_case.expected_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        StatusEndpointTestCase(
            description="reports a servable compile and a missing warehouse",
            break_compile=False,
            expected_state="ok",
            expected_warehouse_connected=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clean_project_when_reading_status_then_reports_ok(
    test_case: StatusEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    payload: dict = client.get("/api/status").json()

    assert payload["compile"]["state"] == test_case.expected_state
    assert payload["compile"]["error"] is None
    assert payload["warehouse"]["connected"] is test_case.expected_warehouse_connected


@pytest.mark.parametrize(
    "test_case",
    [
        StatusEndpointTestCase(
            description="warehouse failure leaves definitions available with recovery status",
            break_compile=False,
            expected_state="ok",
            expected_warehouse_connected=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unreachable_warehouse_when_refreshing_then_ui_contract_remains_available(
    test_case: StatusEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    connection_attempts: list[str] = []

    def fail_connection() -> AdapterConnection:
        connection_attempts.append("attempted")
        raise RuntimeError("warehouse is starting")

    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            database="analytics",
            project_dir=tmp_path,
            execution_context=DevExecutionContext(
                database="analytics", connection_factory=fail_connection
            ),
        )
    )

    refresh: NativeResponse = client.post("/api/warehouse/refresh")

    assert refresh.status_code == 200
    assert refresh.json()["compile"]["state"] == test_case.expected_state
    assert refresh.json()["warehouse"]["connected"] is test_case.expected_warehouse_connected
    assert connection_attempts == ["attempted"]
    assert client.get("/api/definitions").status_code == 200
    assert client.get("/api/state").status_code == 503


@pytest.mark.parametrize(
    "test_case",
    [
        StatusEndpointTestCase(
            description="explicit refresh immediately reconnects an available warehouse",
            break_compile=False,
            expected_state="ok",
            expected_warehouse_connected=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_available_warehouse_when_retrying_now_then_response_reports_reconnection(
    test_case: StatusEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    connection: AdapterConnection = build_fake_state_connection()
    connection_attempts: list[str] = []

    def connect() -> AdapterConnection:
        connection_attempts.append("attempted")
        return connection

    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            database="analytics",
            project_dir=tmp_path,
            execution_context=DevExecutionContext(database="analytics", connection_factory=connect),
        )
    )

    refresh: NativeResponse = client.post("/api/warehouse/refresh")

    assert refresh.status_code == 200
    assert refresh.json()["compile"]["state"] == test_case.expected_state
    assert refresh.json()["warehouse"]["connected"] is test_case.expected_warehouse_connected
    assert connection_attempts == ["attempted"]


@pytest.mark.parametrize(
    "test_case",
    [
        SnapshotRefreshPerformanceTestCase(
            description="healthy snapshot refresh skips the contended connection probe",
            expected_status=200,
            expected_probe_calls=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_healthy_warehouse_when_refreshing_snapshot_then_connection_is_not_probed(
    test_case: SnapshotRefreshPerformanceTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    connection: AdapterConnection = build_fake_state_connection()
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=connection,
            database="analytics",
            project_dir=tmp_path,
        )
    )

    with patch.object(connection, "capture_warehouse_timestamp") as probe:
        refresh: NativeResponse = client.post("/api/warehouse/refresh")

    assert refresh.status_code == test_case.expected_status
    assert probe.call_count == test_case.expected_probe_calls


@pytest.mark.parametrize(
    "test_case",
    [
        StatusEndpointTestCase(
            description="reload reports the failure with its source location",
            break_compile=True,
            expected_state="failing",
            expected_warehouse_connected=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_broken_project_when_reloading_then_reports_failure_location(
    test_case: StatusEndpointTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)
    first_version: str = client.get("/api/status").json()["compile"]["versionKey"]
    break_project_compile(project_dir=tmp_path)

    payload: dict = client.post("/api/reload").json()

    assert payload["compile"]["state"] == test_case.expected_state
    assert payload["compile"]["versionKey"] != first_version
    assert "broken.sql" in payload["compile"]["error"]["message"]
    retained_definitions: NativeResponse = client.get("/api/definitions")
    assert retained_definitions.status_code == 200
    assert retained_definitions.json()["versionKey"] == first_version


@pytest.mark.parametrize(
    "test_case",
    [
        ReloadAuthorizationTestCase(
            description="project role grants reload while unassigned viewer is denied",
            expected_denied_status=403,
            expected_allowed_status=200,
            expected_denied_reason="no_matching_assignment",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_reload_role_when_viewers_reload_then_current_assignment_is_enforced(
    test_case: ReloadAuthorizationTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_reload_client(project_dir=tmp_path)
    assert client.get("/api/status", headers={"X-SQLTide-User": "alice"}).status_code == 200

    denied: NativeResponse = client.post(
        "/api/reload",
        headers={"X-SQLTide-User": "bob", **_PROXY_PROOF_HEADERS},
    )
    allowed: NativeResponse = client.post(
        "/api/reload",
        headers={"X-SQLTide-User": "alice", **_PROXY_PROOF_HEADERS},
    )

    assert denied.status_code == test_case.expected_denied_status
    assert denied.json()["detail"]["reason"] == test_case.expected_denied_reason
    assert allowed.status_code == test_case.expected_allowed_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        ReloadAuthorizationTestCase(
            description="pipeline quality role authorizes audit run before execution",
            expected_denied_status=403,
            expected_allowed_status=503,
            expected_denied_reason="no_matching_assignment",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_quality_role_when_running_audit_then_coverage_is_enforced(
    test_case: ReloadAuthorizationTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_quality_client(project_dir=tmp_path)
    body: dict[str, str] = {"kind": "audit", "name": "orders_clean.order_id.not_null.1"}

    denied: NativeResponse = client.post(
        "/api/checks/run",
        json=body,
        headers={"X-SQLTide-User": "bob", **_PROXY_PROOF_HEADERS},
    )
    allowed: NativeResponse = client.post(
        "/api/checks/run",
        json=body,
        headers={"X-SQLTide-User": "alice", **_PROXY_PROOF_HEADERS},
    )

    assert denied.status_code == test_case.expected_denied_status
    assert denied.json()["detail"]["reason"] == test_case.expected_denied_reason
    assert denied.json()["detail"]["missingPipelines"] == ["order_events"]
    assert allowed.status_code == test_case.expected_allowed_status
    assert allowed.json() == {"detail": "no warehouse connection"}
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        CapabilitiesTestCase(
            description="capabilities reflect assigned pipeline quality coverage",
            expected_project="test_project",
            expected_target="dev",
            expected_quality_pipelines=("order_events",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_assigned_quality_role_when_reading_capabilities_then_coverage_is_reported(
    test_case: CapabilitiesTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_quality_client(project_dir=tmp_path)

    alice: dict = client.get("/api/auth/capabilities", headers={"X-SQLTide-User": "alice"}).json()
    bob: dict = client.get("/api/auth/capabilities", headers={"X-SQLTide-User": "bob"}).json()

    assert alice["systemAdmin"] is False
    assert alice["project"] == test_case.expected_project
    assert alice["target"] == test_case.expected_target
    assert alice["pipelinePermissions"]["quality.audit.run"] == list(
        test_case.expected_quality_pipelines
    )
    assert alice["pipelinePermissions"]["quality.test.run"] == list(
        test_case.expected_quality_pipelines
    )
    assert alice["staleRoles"] == []
    assert bob["permissions"] == []
    assert bob["pipelinePermissions"] == {}
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        ReloadAuthorizationTestCase(
            description="raw message boundary requires the message-reader capability",
            expected_denied_status=403,
            expected_allowed_status=503,
            expected_denied_reason="no_matching_assignment",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_message_reader_role_when_browsing_messages_then_boundary_is_enforced(
    test_case: ReloadAuthorizationTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_message_client(project_dir=tmp_path)

    denied: NativeResponse = client.post(
        "/api/sources/orders/messages",
        json={},
        headers={"X-SQLTide-User": "bob", **_PROXY_PROOF_HEADERS},
    )
    allowed: NativeResponse = client.post(
        "/api/sources/orders/messages",
        json={},
        headers={"X-SQLTide-User": "alice", **_PROXY_PROOF_HEADERS},
    )
    topics: NativeResponse = client.get("/api/topics", headers={"X-SQLTide-User": "bob"})

    assert denied.status_code == test_case.expected_denied_status
    assert denied.json()["detail"]["reason"] == test_case.expected_denied_reason
    assert allowed.status_code == test_case.expected_allowed_status
    assert topics.status_code == 200
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        StatusEndpointTestCase(
            description="failed policy reload keeps prior policy usable for a retry",
            break_compile=False,
            expected_state="ok",
            expected_warehouse_connected=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_candidate_policy_when_reloading_then_prior_policy_still_authorizes(
    test_case: StatusEndpointTestCase,
    tmp_path: Path,
) -> None:
    client, store = build_assigned_proxy_reload_client(project_dir=tmp_path)
    alice_headers: dict[str, str] = {"X-SQLTide-User": "alice", **_PROXY_PROOF_HEADERS}
    assert client.get("/api/status", headers=alice_headers).status_code == 200
    write_reload_access_policy(project_dir=tmp_path, permission="project.unknown")

    failed: NativeResponse = client.post("/api/reload", headers=alice_headers)
    write_reload_access_policy(project_dir=tmp_path, permission="project.reload")
    recovered: NativeResponse = client.post("/api/reload", headers=alice_headers)

    assert failed.status_code == 200
    assert failed.json()["compile"]["state"] == "failing"
    assert recovered.status_code == 200
    assert recovered.json()["compile"]["state"] == test_case.expected_state
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        StateRouteErrorTestCase(
            description="state route reports a missing warehouse as unavailable",
            expected_status=503,
            expected_detail="no warehouse connection",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_warehouse_when_reading_state_then_route_returns_explicit_error(
    test_case: StateRouteErrorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/state")

    assert response.status_code == test_case.expected_status
    assert response.json() == {"detail": test_case.expected_detail}


@pytest.mark.parametrize(
    "test_case",
    [
        DevAppLifespanTestCase(
            description="application lifespan starts and closes every owned background resource",
            expected_status=200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dev_app_context_when_lifespan_exits_then_owned_resources_are_closed(
    test_case: DevAppLifespanTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    with (
        patch("sqltide._native.AuditScheduler") as scheduler_class,
        patch("sqltide._native.SensorScheduler") as sensor_scheduler_class,
        patch("sqltide._native.BuildProcessManager") as builds_class,
        patch("sqltide._native.KafkaLagReader") as lag_reader_class,
        patch("sqltide._native.KafkaTopicReader") as topic_reader_class,
    ):
        scheduler: MagicMock = scheduler_class.return_value
        sensor_scheduler: MagicMock = sensor_scheduler_class.return_value
        lifecycle: MagicMock = MagicMock()
        lifecycle.attach_mock(sensor_scheduler, "sensors")
        lifecycle.attach_mock(scheduler, "audits")
        builds: MagicMock = builds_class.return_value
        lag_reader: MagicMock = lag_reader_class.return_value
        topic_reader: MagicMock = topic_reader_class.return_value
        lag_reader.read.return_value = None
        topic_reader.read.return_value = None
        app: DevApplication = create_dev_app(state=state, project_dir=tmp_path)

        with NativeTestClient(app) as client:
            response: NativeResponse = client.get("/api/status")
            assert response.status_code == test_case.expected_status
            sensor_scheduler.initialize_event_checkpoints.assert_called_once_with()
            sensor_scheduler.start.assert_called_once_with()
            scheduler.start.assert_called_once_with()
            lifecycle.assert_has_calls(
                [
                    call.sensors.initialize_event_checkpoints(),
                    call.sensors.start(),
                    call.audits.start(),
                ],
                any_order=False,
            )

        sensor_scheduler.close.assert_called_once_with()
        scheduler.close.assert_called_once_with()
        lag_reader.close.assert_called_once_with()
        topic_reader.close.assert_called_once_with()
        builds.close.assert_called_once_with()
