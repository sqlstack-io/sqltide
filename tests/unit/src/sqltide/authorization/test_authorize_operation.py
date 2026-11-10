"""Behavior tests for centralized project authorization."""

from pathlib import Path

import pytest

from sqltide._native import authorize_operation
from sqltide.authorization.models import AuthorizationDecision
from sqltide.authorization.types import AuthorizationReason
from tests.unit.src.sqltide.authorization._test_types import (
    AuthorizationScenario,
    AuthorizationTestCase,
)
from tests.unit.src.sqltide.authorization.helpers import (
    admin_without_policy,
    assigned_project_role,
    collectively_covered_pipelines,
    partially_covered_pipelines,
    project_scoped_quality_grant,
    stale_assignment,
    target_mismatched_assignment,
    viewer_without_policy,
)


@pytest.mark.parametrize(
    "test_case",
    [
        AuthorizationTestCase(
            description="system administrator bypasses absent policy",
            build_scenario=admin_without_policy,
            expected_allowed=True,
            expected_reason=AuthorizationReason.SYSTEM_ADMIN,
            expected_roles=("admin",),
        ),
        AuthorizationTestCase(
            description="viewer fails closed without policy",
            build_scenario=viewer_without_policy,
            expected_allowed=False,
            expected_reason=AuthorizationReason.POLICY_ABSENT,
        ),
        AuthorizationTestCase(
            description="assigned role grants project operation",
            build_scenario=assigned_project_role,
            expected_allowed=True,
            expected_reason=AuthorizationReason.GRANTED,
            expected_roles=("operator",),
        ),
        AuthorizationTestCase(
            description="target mismatch denies assignment",
            build_scenario=target_mismatched_assignment,
            expected_allowed=False,
            expected_reason=AuthorizationReason.NO_MATCHING_ASSIGNMENT,
        ),
        AuthorizationTestCase(
            description="removed policy role makes assignment stale",
            build_scenario=stale_assignment,
            expected_allowed=False,
            expected_reason=AuthorizationReason.STALE_ASSIGNMENT,
        ),
        AuthorizationTestCase(
            description="different roles collectively cover pipelines",
            build_scenario=collectively_covered_pipelines,
            expected_allowed=True,
            expected_reason=AuthorizationReason.GRANTED,
            expected_roles=("ingestion_operator", "reporting_operator"),
        ),
        AuthorizationTestCase(
            description="partial pipeline coverage fails whole operation",
            build_scenario=partially_covered_pipelines,
            expected_allowed=False,
            expected_reason=AuthorizationReason.MISSING_PIPELINES,
            expected_roles=("ingestion_operator",),
            expected_missing_pipelines=("reporting",),
        ),
        AuthorizationTestCase(
            description="project-scoped grant covers every affected pipeline",
            build_scenario=project_scoped_quality_grant,
            expected_allowed=True,
            expected_reason=AuthorizationReason.GRANTED,
            expected_roles=("quality_project",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_operation_and_current_membership_when_authorizing_then_decision_is_explainable(
    test_case: AuthorizationTestCase,
    tmp_path: Path,
) -> None:
    scenario: AuthorizationScenario = test_case.build_scenario(tmp_path=tmp_path)
    store, request = scenario

    decision: AuthorizationDecision = authorize_operation(store=store, request=request)

    assert decision.allowed is test_case.expected_allowed
    assert decision.reason == test_case.expected_reason
    assert decision.matched_roles == test_case.expected_roles
    assert decision.missing_pipelines == test_case.expected_missing_pipelines
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
