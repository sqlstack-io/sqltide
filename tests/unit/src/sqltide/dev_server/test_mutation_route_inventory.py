"""CI inventory: every mutation route must declare its authorization policy."""

from pathlib import Path

import pytest

from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeTestClient
from tests.unit.src.sqltide.dev_server._test_types import DevRefactorTestCase
from tests.unit.src.sqltide.dev_server.helpers import (
    build_test_client,
    registered_mutation_routes,
    write_dev_server_project,
)

# Every mutating API route must appear here with its explicit authorization
# policy. Adding a mutation route without declaring its policy fails this test.
_DECLARED_MUTATION_POLICIES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/login"): "public: issues the session that all other calls require",
    ("POST", "/api/auth/logout"): "session-owner: revokes only the caller's session",
    ("POST", "/api/admin/users"): "system-only: account.manage via require_admin",
    ("PATCH", "/api/admin/users/{user_id}"): "system-only: account.manage via require_admin",
    ("POST", "/api/admin/users/{user_id}/password"): (
        "system-only: account.manage via require_admin"
    ),
    ("POST", "/api/admin/users/{user_id}/roles"): "system-only: role.assign via require_admin",
    ("DELETE", "/api/admin/users/{user_id}/roles/{role_name}"): (
        "system-only: role.assign via require_admin"
    ),
    ("POST", "/api/admin/users/{user_id}/project-roles"): (
        "system-only: role.assign via require_admin"
    ),
    ("DELETE", "/api/admin/project-roles/{assignment_id}"): (
        "system-only: role.assign via require_admin"
    ),
    ("POST", "/api/reload"): "project.reload via reload_guarded operation authorization",
    ("POST", "/api/warehouse/refresh"): (
        "authenticated read recovery: reconnects without warehouse mutation"
    ),
    ("POST", "/api/checks/run"): (
        "quality.test.run or quality.audit.run via require_check_authorization"
    ),
    ("POST", "/api/audits/run"): (
        "quality.audit.run for every requested audit via require_check_authorization"
    ),
    ("POST", "/api/build"): (
        "build.direct.run and/or deployment.create via require_prepared_build_authorization"
    ),
    ("POST", "/api/build/cancel"): "build.cancel via require_run_cancel_authorization",
    ("POST", "/api/build/kill"): "build.kill via require_kill_authorization",
    ("POST", "/api/deployments/{deployment_id}/promote"): (
        "deployment.promote via require_promotion_authorization"
    ),
    ("POST", "/api/deployments/{deployment_id}/rollback"): (
        "deployment.promote over the target deployment via require_promotion_authorization"
    ),
    ("POST", "/api/deployments/cleanup"): ("deployment.cleanup via require_cleanup_authorization"),
    ("POST", "/api/destruction/plans"): (
        "pipeline.destroy or target.reset via require_destruction_authorization"
    ),
    ("POST", "/api/destruction/plans/{plan_id}/review"): (
        "creator-bound pipeline.destroy or target.reset review gate"
    ),
    ("POST", "/api/destruction/plans/{plan_id}/execute"): (
        "creator-bound exact challenge plus current destruction authorization"
    ),
    ("POST", "/api/runs/{invocation_id}/recovery-plan"): (
        "failed durable destruction intent plus current destruction authorization"
    ),
    ("POST", "/api/sources/{name}/messages"): (
        "source.messages.read via require_message_read_authorization"
    ),
    ("POST", "/api/sources/{name}/messages/record"): (
        "source.messages.read via require_message_read_authorization"
    ),
    ("POST", "/api/sources/{name}/messages/facets"): (
        "source.messages.read via require_message_read_authorization"
    ),
    ("POST", "/api/sensors/{sensor_name}/status"): (
        "automation.manage via require_automation_authorization"
    ),
    ("POST", "/api/sensors/dead-letters/retry"): (
        "automation.manage via require_automation_authorization"
    ),
    ("POST", "/api/sensors/dead-letters/skip"): (
        "automation.manage via require_automation_authorization"
    ),
}


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="every mutation route declares an authorization policy",
            expected_value=(),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_registered_routes_when_inventorying_mutations_then_every_policy_is_declared(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    registered: frozenset[tuple[str, str]] = registered_mutation_routes(client=client)

    undeclared: tuple[tuple[str, str], ...] = tuple(
        sorted(registered - set(_DECLARED_MUTATION_POLICIES))
    )
    removed: tuple[tuple[str, str], ...] = tuple(
        sorted(set(_DECLARED_MUTATION_POLICIES) - registered)
    )
    assert undeclared == test_case.expected_value
    assert removed == test_case.expected_value


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
