from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from time import monotonic
from unittest.mock import PropertyMock, patch

import pytest

from sqltide.auth.classes.control_store import ControlStore
from sqltide.authorization.models import AuthorizationDecision
from sqltide.authorization.types import AuthorizationReason
from sqltide.compiler.access.types import Permission
from sqltide.dev_server.classes.warehouse_runtime import WarehouseRuntime
from sqltide.executor.destruction.exceptions import (
    DestructionDependencyError,
    DestructionResourceError,
)
from sqltide.executor.destruction.models import (
    DestructionPlan,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    DestructionActorBindingRouteTestCase,
    DestructionAsyncExecutionRouteTestCase,
    DestructionAuthorizationRouteTestCase,
    DestructionClosureAuthorizationRouteTestCase,
    DestructionRecoveryDependencyRouteTestCase,
    DestructionRecoveryRejectionRouteTestCase,
    DestructionRecoveryRouteTestCase,
    DestructionResetRouteTestCase,
    DestructionResourceConflictRouteTestCase,
    DestructionRestartRouteTestCase,
    DestructionReviewGateRouteTestCase,
    InactivePipelineRouteTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_assigned_proxy_operations_client,
    build_pipeline_destruction_route_plan,
    build_proxy_test_client,
    build_target_reset_route_plan,
    proxy_proof_headers,
)


@pytest.mark.parametrize(
    "test_case",
    [
        InactivePipelineRouteTestCase(
            description="retained manifest pipeline is exposed to an authorized administrator",
            expected_pipeline_name="retired_orders",
            expected_model_count=2,
            expected_resource_count=5,
            expected_last_published_at="2026-08-23 10:00:00.000000",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_inactive_pipeline_ownership_when_listing_then_api_returns_historical_summary(
    tmp_path: Path,
    test_case: InactivePipelineRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with patch(
        "sqltide._native.list_inactive_pipelines",
        return_value=(
            InactivePipeline(
                name=test_case.expected_pipeline_name,
                model_count=test_case.expected_model_count,
                resource_count=test_case.expected_resource_count,
                last_published_at=test_case.expected_last_published_at,
            ),
        ),
    ):
        response: NativeResponse = client.get(
            "/api/destruction/pipelines/inactive",
            headers=proxy_proof_headers(username="alice"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "pipelines": [
            {
                "name": test_case.expected_pipeline_name,
                "modelCount": test_case.expected_model_count,
                "resourceCount": test_case.expected_resource_count,
                "lastPublishedAt": test_case.expected_last_published_at,
            }
        ]
    }
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionAuthorizationRouteTestCase(
            description="pipeline destroy scope permits only its assigned actor",
            expected_denied_status=403,
            expected_reason="system_admin_required",
            expected_allowed_status=200,
            expected_plan_id="plan-1",
            expected_planner_call_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_destroy_permission_when_planning_then_http_boundary_enforces_scope(
    tmp_path: Path,
    test_case: DestructionAuthorizationRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with patch(
        "sqltide._native.plan_destruction",
        return_value=build_pipeline_destruction_route_plan(),
    ) as planner:
        denied: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=proxy_proof_headers(username="bob"),
        )
        allowed: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=proxy_proof_headers(username="alice"),
        )

    assert denied.status_code == test_case.expected_denied_status
    assert denied.json()["detail"]["reason"] == test_case.expected_reason
    assert allowed.status_code == test_case.expected_allowed_status
    assert allowed.json()["planId"] == test_case.expected_plan_id
    assert allowed.json()["reviewedAt"] is None
    assert planner.call_count == test_case.expected_planner_call_count
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionResourceConflictRouteTestCase(
            description="recorded resource conflict returns a structured response",
            conflict_message="recorded virtual resource is in another database",
            expected_status=409,
            expected_reason="resource_conflict",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_resource_conflict_when_planning_then_api_returns_actionable_conflict(
    tmp_path: Path,
    test_case: DestructionResourceConflictRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with patch(
        "sqltide._native.plan_destruction",
        side_effect=DestructionResourceError(test_case.conflict_message),
    ):
        response: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=proxy_proof_headers(username="alice"),
        )

    assert response.status_code == test_case.expected_status
    assert response.json()["detail"] == {
        "message": test_case.conflict_message,
        "reason": test_case.expected_reason,
    }
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionClosureAuthorizationRouteTestCase(
            description="suggested dependent closure is reauthorized before disclosure",
            dependent_pipeline="reporting",
            expected_status=403,
            expected_permission="pipeline.destroy",
            expected_authorization_call_count=2,
            expected_planner_call_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unauthorized_dependant_when_planning_then_suggested_closure_is_hidden(
    tmp_path: Path,
    test_case: DestructionClosureAuthorizationRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    blocked_plan: DestructionPlan = replace(
        build_pipeline_destruction_route_plan(),
        included_dependent_pipeline_names=(test_case.dependent_pipeline,),
        affected_pipeline_names=("order_events", test_case.dependent_pipeline),
    )
    denied: AuthorizationDecision = AuthorizationDecision(
        allowed=False,
        reason=AuthorizationReason.NO_MATCHING_ASSIGNMENT,
        permission=Permission(test_case.expected_permission),
        project_name="test_project",
        target_name="dev",
        affected_pipelines=("order_events", test_case.dependent_pipeline),
    )
    with (
        patch(
            "sqltide._native.plan_destruction",
            side_effect=(
                DestructionDependencyError((test_case.dependent_pipeline,)),
                blocked_plan,
            ),
        ) as planner,
        patch(
            "sqltide.authorization.main.authorize_operation.authorize_operation",
            side_effect=(
                AuthorizationDecision(
                    allowed=True,
                    reason=AuthorizationReason.SYSTEM_ADMIN,
                    permission=Permission(test_case.expected_permission),
                    project_name="test_project",
                    target_name="dev",
                    affected_pipelines=("order_events",),
                ),
                denied,
            ),
        ) as authorization,
    ):
        response: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=proxy_proof_headers(username="alice"),
        )

    assert response.status_code == test_case.expected_status
    assert response.json()["detail"]["permission"] == test_case.expected_permission
    assert authorization.call_count == test_case.expected_authorization_call_count
    assert planner.call_count == test_case.expected_planner_call_count
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionResetRouteTestCase(
            description="target reset scope permits planning",
            expected_status=200,
            expected_managed_sources_included=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_target_reset_permission_when_planning_then_reset_scope_is_allowed(
    tmp_path: Path,
    test_case: DestructionResetRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with patch(
        "sqltide._native.plan_destruction",
        return_value=build_target_reset_route_plan(),
    ):
        response: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "reset_target", "pipelineNames": []},
            headers=proxy_proof_headers(username="alice"),
        )

    assert response.status_code == test_case.expected_status
    assert response.json()["managedSourcesIncluded"] is test_case.expected_managed_sources_included
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionReviewGateRouteTestCase(
            description="execution cannot bypass server review",
            expected_status=409,
            expected_detail_fragment="has not been reviewed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unreviewed_plan_when_executing_then_server_review_gate_blocks(
    tmp_path: Path,
    test_case: DestructionReviewGateRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    headers: dict[str, str] = proxy_proof_headers(username="alice")
    with patch(
        "sqltide._native.plan_destruction",
        return_value=build_pipeline_destruction_route_plan(),
    ):
        created: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=headers,
        )
        response: NativeResponse = client.post(
            f"/api/destruction/plans/{created.json()['planId']}/execute",
            json={"responses": ["order_events"]},
            headers=headers,
        )

    assert response.status_code == test_case.expected_status
    assert test_case.expected_detail_fragment in response.json()["detail"]
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRecoveryRouteTestCase(
            description="failed pipeline destruction retains explicit dependent intent",
            invocation_id="failed-destruction-1",
            command="destroy pipelines",
            operation_kind="destroy_pipelines",
            expected_plan_id="plan-1",
            expected_pipeline_names=("order_events",),
            expected_included_pipeline_names=("included_consumer",),
        ),
        DestructionRecoveryRouteTestCase(
            description="failed target reset creates a fresh unreviewed reset plan",
            invocation_id="failed-reset-1",
            command="reset target",
            operation_kind="reset_target",
            expected_plan_id="plan-1",
            expected_pipeline_names=(),
            expected_included_pipeline_names=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failed_destruction_run_when_recovering_then_server_replans_recorded_intent(
    tmp_path: Path,
    test_case: DestructionRecoveryRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    headers: dict[str, str] = proxy_proof_headers(username="alice")
    run: dict[str, object] = {
        "projectIdentity": logical_project_identity(project_dir=tmp_path),
        "command": test_case.command,
        "mode": "destructive",
        "outcome": "failed",
        "summary": {
            "operationKind": test_case.operation_kind,
            "target": "dev",
            "database": "analytics",
            "originalSelection": list(test_case.expected_pipeline_names),
            "includedDependentPipelines": list(test_case.expected_included_pipeline_names),
            "affectedPipelines": ["order_events", "historical_derived_scope"],
            "planId": "consumed-plan",
        },
    }

    @contextmanager
    def read_connection(_: WarehouseRuntime) -> Iterator[object]:
        yield object()

    with (
        patch.object(WarehouseRuntime, "read_connection", read_connection),
        patch(
            "sqltide._native.read_destruction_recovery_run",
            return_value=run,
        ),
        patch(
            "sqltide._native.plan_destruction",
            return_value=replace(
                {
                    "destroy_pipelines": build_pipeline_destruction_route_plan(),
                    "reset_target": build_target_reset_route_plan(),
                }[test_case.operation_kind],
                requested_pipeline_names=test_case.expected_pipeline_names,
                included_dependent_pipeline_names=(test_case.expected_included_pipeline_names),
            ),
        ) as planner,
    ):
        response: NativeResponse = client.post(
            f"/api/runs/{test_case.invocation_id}/recovery-plan",
            headers=headers,
            json={
                "operation": "reset_target",
                "pipelineNames": ["client_supplied_scope"],
                "sql": "DROP TABLE everything",
            },
        )
        stored: NativeResponse = client.get(
            f"/api/destruction/plans/{test_case.expected_plan_id}",
            headers=headers,
        )

    planned_request: DestructionRequest = planner.call_args.kwargs["request"]
    assert response.status_code == 200
    assert response.json()["planId"] == test_case.expected_plan_id
    assert response.json()["reviewedAt"] is None
    assert planned_request.pipeline_names == test_case.expected_pipeline_names
    assert (
        planned_request.included_dependent_pipeline_names
        == test_case.expected_included_pipeline_names
    )
    assert stored.status_code == 200
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRecoveryRejectionRouteTestCase(
            description="a successful run is not recovery evidence",
            mode="destructive",
            outcome="succeeded",
            command="destroy pipelines",
            operation_kind="destroy_pipelines",
            project_identity_kind="current",
            target="dev",
            included_dependant_pipelines=[],
            expected_status=409,
            expected_error_fragment="terminal failed destruction run",
        ),
        DestructionRecoveryRejectionRouteTestCase(
            description="a command and operation mismatch fails closed",
            mode="destructive",
            outcome="failed",
            command="build",
            operation_kind="destroy_pipelines",
            project_identity_kind="current",
            target="dev",
            included_dependant_pipelines=[],
            expected_status=409,
            expected_error_fragment="command and evidence disagree",
        ),
        DestructionRecoveryRejectionRouteTestCase(
            description="another project cannot supply recovery intent",
            mode="destructive",
            outcome="failed",
            command="destroy pipelines",
            operation_kind="destroy_pipelines",
            project_identity_kind="other",
            target="dev",
            included_dependant_pipelines=[],
            expected_status=404,
            expected_error_fragment="was not found",
        ),
        DestructionRecoveryRejectionRouteTestCase(
            description="another target cannot supply recovery intent",
            mode="destructive",
            outcome="failed",
            command="destroy pipelines",
            operation_kind="destroy_pipelines",
            project_identity_kind="current",
            target="prod",
            included_dependant_pipelines=[],
            expected_status=409,
            expected_error_fragment="differs from the active server",
        ),
        DestructionRecoveryRejectionRouteTestCase(
            description="incomplete recorded scope fails closed",
            mode="destructive",
            outcome="failed",
            command="destroy pipelines",
            operation_kind="destroy_pipelines",
            project_identity_kind="current",
            target="dev",
            included_dependant_pipelines=None,
            expected_status=409,
            expected_error_fragment="includedDependentPipelines",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_durable_run_when_recovering_then_no_plan_is_created(
    tmp_path: Path,
    test_case: DestructionRecoveryRejectionRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    headers: dict[str, str] = proxy_proof_headers(username="alice")
    project_identities: dict[str, str] = {
        "current": logical_project_identity(project_dir=tmp_path),
        "other": "another-project",
    }
    run: dict[str, object] = {
        "projectIdentity": project_identities[test_case.project_identity_kind],
        "command": test_case.command,
        "mode": test_case.mode,
        "outcome": test_case.outcome,
        "summary": {
            "operationKind": test_case.operation_kind,
            "target": test_case.target,
            "database": "analytics",
            "originalSelection": ["order_events"],
            "includedDependentPipelines": test_case.included_dependant_pipelines,
        },
    }

    @contextmanager
    def read_connection(_: WarehouseRuntime) -> Iterator[object]:
        yield object()

    with (
        patch.object(WarehouseRuntime, "read_connection", read_connection),
        patch(
            "sqltide._native.read_destruction_recovery_run",
            return_value=run,
        ),
        patch("sqltide._native.plan_destruction") as planner,
    ):
        response: NativeResponse = client.post(
            "/api/runs/invalid-destruction/recovery-plan",
            headers=headers,
        )

    assert response.status_code == test_case.expected_status
    assert test_case.expected_error_fragment in response.text
    planner.assert_not_called()
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRecoveryDependencyRouteTestCase(
            description="new dependency closure requires an interactive selection",
            newly_required_pipelines=("new_consumer",),
            expected_status=409,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_new_dependency_when_recovering_then_it_is_not_silently_included(
    tmp_path: Path,
    test_case: DestructionRecoveryDependencyRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    run: dict[str, object] = {
        "projectIdentity": logical_project_identity(project_dir=tmp_path),
        "command": "destroy pipelines",
        "mode": "destructive",
        "outcome": "failed",
        "summary": {
            "operationKind": "destroy_pipelines",
            "target": "dev",
            "database": "analytics",
            "originalSelection": ["order_events"],
            "includedDependentPipelines": [],
        },
    }

    @contextmanager
    def read_connection(_: WarehouseRuntime) -> Iterator[object]:
        yield object()

    with (
        patch.object(WarehouseRuntime, "read_connection", read_connection),
        patch(
            "sqltide._native.read_destruction_recovery_run",
            return_value=run,
        ),
        patch(
            "sqltide._native.plan_destruction",
            side_effect=DestructionDependencyError(test_case.newly_required_pipelines),
        ),
    ):
        response: NativeResponse = client.post(
            "/api/runs/dependency-drift/recovery-plan",
            headers=proxy_proof_headers(username="alice"),
        )

    assert response.status_code == test_case.expected_status
    assert response.json()["detail"]["missingPipelines"] == list(test_case.newly_required_pipelines)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionAsyncExecutionRouteTestCase(
            description="accepted execution returns before the warehouse worker completes",
            expected_status=202,
            expected_execution_status="starting",
            maximum_response_seconds=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_plan_when_executing_then_api_returns_run_identity_before_completion(
    tmp_path: Path,
    test_case: DestructionAsyncExecutionRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    headers: dict[str, str] = proxy_proof_headers(username="alice")
    started: Event = Event()
    release: Event = Event()
    finished: Event = Event()
    captured_invocation_ids: list[str] = []

    def execute_worker(**kwargs: object) -> None:
        captured_invocation_ids.append(str(kwargs["invocation_id"]))
        started.set()
        _ = release.wait(timeout=5)
        finished.set()

    with (
        patch(
            "sqltide._native.plan_destruction",
            return_value=build_pipeline_destruction_route_plan(),
        ),
        patch(
            "sqltide._native.execute_destruction",
            side_effect=execute_worker,
        ),
        patch.object(
            WarehouseRuntime,
            "observation_connection",
            new_callable=PropertyMock,
            return_value=object(),
        ),
    ):
        created: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=headers,
        )
        _ = client.post(
            f"/api/destruction/plans/{created.json()['planId']}/review",
            headers=headers,
        )
        timeout_release: Timer = Timer(2, release.set)
        timeout_release.start()
        request_started: float = monotonic()
        response: NativeResponse = client.post(
            f"/api/destruction/plans/{created.json()['planId']}/execute",
            json={"responses": ["order_events"]},
            headers=headers,
        )
        elapsed: float = monotonic() - request_started
        timeout_release.cancel()

        assert response.status_code == test_case.expected_status, response.text
        assert started.wait(timeout=1)
        release.set()
        assert finished.wait(timeout=1)

    assert response.json()["status"] == test_case.expected_execution_status
    assert elapsed < test_case.maximum_response_seconds
    assert captured_invocation_ids == [response.json()["invocationId"]]
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionActorBindingRouteTestCase(
            description="another actor cannot discover a frozen plan",
            expected_status=404,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_another_actor_plan_when_reviewing_then_creator_binding_hides_plan(
    tmp_path: Path,
    test_case: DestructionActorBindingRouteTestCase,
) -> None:
    client: NativeTestClient
    store: ControlStore
    client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    with patch(
        "sqltide._native.plan_destruction",
        return_value=build_pipeline_destruction_route_plan(),
    ):
        created: NativeResponse = client.post(
            "/api/destruction/plans",
            json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
            headers=proxy_proof_headers(username="alice"),
        )
        response: NativeResponse = client.post(
            f"/api/destruction/plans/{created.json()['planId']}/review",
            headers=proxy_proof_headers(username="bob"),
        )

    assert response.status_code == test_case.expected_status
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRestartRouteTestCase(
            description="reviewed plan survives dev server reconstruction",
            expected_plan_id="plan-1",
            expected_reviewed_status=200,
            expected_reloaded_status=200,
            expected_mismatched_review_status=409,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_plan_when_dev_app_is_reconstructed_then_http_plan_survives(
    tmp_path: Path,
    test_case: DestructionRestartRouteTestCase,
) -> None:
    first_client: NativeTestClient
    store: ControlStore
    first_client, store = build_assigned_proxy_operations_client(project_dir=tmp_path)
    headers: dict[str, str] = proxy_proof_headers(username="alice")
    with first_client:
        with patch(
            "sqltide._native.plan_destruction",
            return_value=build_pipeline_destruction_route_plan(),
        ):
            created: NativeResponse = first_client.post(
                "/api/destruction/plans",
                json={"operation": "destroy_pipelines", "pipelineNames": ["order_events"]},
                headers=headers,
            )
            reviewed: NativeResponse = first_client.post(
                f"/api/destruction/plans/{created.json()['planId']}/review",
                headers=headers,
            )

    second_client: NativeTestClient = build_proxy_test_client(project_dir=tmp_path, store=store)
    with second_client:
        reloaded: NativeResponse = second_client.get(
            f"/api/destruction/plans/{test_case.expected_plan_id}",
            headers=headers,
        )
        mismatched_review: NativeResponse = second_client.post(
            f"/api/destruction/plans/{test_case.expected_plan_id}/review",
            headers=headers,
        )

    assert reviewed.status_code == test_case.expected_reviewed_status
    assert reloaded.status_code == test_case.expected_reloaded_status
    assert reloaded.json()["planId"] == test_case.expected_plan_id
    assert reloaded.json()["reviewedAt"] == reviewed.json()["reviewedAt"]
    assert mismatched_review.status_code == test_case.expected_mismatched_review_status
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
