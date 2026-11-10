"""HTTP-boundary coverage for high-impact operational permissions."""

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeResponse
from tests.unit.src.sqltide.dev_server._test_types import (
    OperationAuthorizationRouteTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_assigned_proxy_operations_client,
    build_assigned_proxy_operations_client_without_warehouse,
    build_direct_plan_preparation,
    proxy_proof_headers,
)


@pytest.mark.parametrize(
    "test_case",
    [
        OperationAuthorizationRouteTestCase(
            description="direct build requires complete pipeline permission",
            path="/api/build",
            body={"selectors": ["orders_clean"]},
            expected_allowed_status=200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_build_role_when_starting_build_then_http_boundary_enforces_permission(
    test_case: OperationAuthorizationRouteTestCase, tmp_path: Path
) -> None:
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with (
        patch(
            "sqltide._native.prepare_build_workflow",
            return_value=build_direct_plan_preparation(),
        ),
        patch(
            "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
            return_value=[],
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.start",
            return_value={"invocationId": "run-1", "status": "starting"},
        ),
    ):
        denied: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="bob"),
        )
        allowed: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="alice"),
        )

    assert denied.status_code == 403
    assert denied.json()["detail"]["permission"] == "build.direct.run"
    assert allowed.status_code == test_case.expected_allowed_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        OperationAuthorizationRouteTestCase(
            description="build cancellation requires pipeline permission",
            path="/api/build/cancel",
            body={"invocationId": "run-1"},
            expected_allowed_status=200,
        ),
        OperationAuthorizationRouteTestCase(
            description="force kill requires target permission",
            path="/api/build/kill",
            body={"invocationId": "run-1"},
            expected_allowed_status=200,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_run_control_role_when_signalling_build_then_http_boundary_enforces_permission(
    test_case: OperationAuthorizationRouteTestCase, tmp_path: Path
) -> None:
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    active_runs: list[dict[str, object]] = [
        {
            "invocationId": "run-1",
            "status": "running",
            "executedLogicalIds": ["model:orders_clean"],
        }
    ]
    with (
        patch(
            "sqltide.dev_server._helpers.queries.runs_query.read_active_runs",
            return_value=active_runs,
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.cancel",
            return_value={"invocationId": "run-1", "status": "cancelling"},
        ),
        patch(
            "sqltide.dev_server.classes.build_process.BuildProcessManager.kill",
            return_value={"invocationId": "run-1", "status": "killed"},
        ),
    ):
        denied: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="bob"),
        )
        allowed: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="alice"),
        )

    assert denied.status_code == 403
    assert allowed.status_code == test_case.expected_allowed_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        OperationAuthorizationRouteTestCase(
            description="deployment promotion requires pipeline permission",
            path="/api/deployments/deployment-1/promote",
            body={},
            expected_allowed_status=200,
        ),
        OperationAuthorizationRouteTestCase(
            description="deployment cleanup requires target permission",
            path="/api/deployments/cleanup",
            body={"retentionDays": 7},
            expected_allowed_status=200,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_role_when_mutating_deployments_then_http_boundary_enforces_permission(
    test_case: OperationAuthorizationRouteTestCase, tmp_path: Path
) -> None:
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with (
        patch(
            "sqltide.dev_server._helpers.server.deployment_operations.promotion_executed_logical_ids",
            return_value=("model:orders_clean",),
        ),
        patch(
            "sqltide.dev_server._helpers.server.deployment_operations.run_deployment_promotion",
            return_value={"deploymentId": "deployment-1", "status": "published"},
        ),
        patch(
            "sqltide.dev_server._helpers.server.deployment_operations.run_deployment_cleanup",
            return_value={"removed": []},
        ),
    ):
        denied: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="bob"),
        )
        allowed: NativeResponse = client.post(
            test_case.path,
            json=test_case.body,
            headers=proxy_proof_headers(username="alice"),
        )

    assert denied.status_code == 403
    assert allowed.status_code == test_case.expected_allowed_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        OperationAuthorizationRouteTestCase(
            description="sensor management requires target permission",
            path="/api/sensors/quality_alerts/status",
            body={"status": "running"},
            expected_allowed_status=503,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_automation_role_when_changing_sensor_then_http_boundary_enforces_permission(
    test_case: OperationAuthorizationRouteTestCase, tmp_path: Path
) -> None:
    client, store = build_assigned_proxy_operations_client_without_warehouse(project_dir=tmp_path)

    denied: NativeResponse = client.post(
        test_case.path,
        json=test_case.body,
        headers=proxy_proof_headers(username="bob"),
    )
    allowed: NativeResponse = client.post(
        test_case.path,
        json=test_case.body,
        headers=proxy_proof_headers(username="alice"),
    )

    assert denied.status_code == 403
    assert allowed.status_code == test_case.expected_allowed_status
    assert allowed.json()["detail"] == "no warehouse connection"
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
