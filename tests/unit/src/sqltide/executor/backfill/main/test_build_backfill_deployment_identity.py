import pytest

from sqltide._native import build_backfill_deployment_identity
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.backfill.models import BackfillDeploymentIdentity
from tests.unit.src.sqltide.executor.backfill.main._test_types import (
    DeploymentIdentityTestCase,
    ExplicitDeploymentIdentityTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentIdentityTestCase(
            description="rejects an identity that cannot be recognized after publication",
            deployment_id="release_2026_07",
            expected_error_fragment=(
                "Deployment ID must match YYYYMMDDTHHMMSSZ_<alphanumeric-suffix>"
            ),
        ),
        DeploymentIdentityTestCase(
            description="rejects an impossible timestamp in a recognized identity",
            deployment_id="20261340T250000Z_release",
            expected_error_fragment="timestamp must be a valid UTC date and time",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_deployment_id_when_building_identity_then_it_is_rejected(
    test_case: DeploymentIdentityTestCase,
) -> None:
    with pytest.raises(BackfillExecutionError, match=test_case.expected_error_fragment):
        build_backfill_deployment_identity(deployment_id=test_case.deployment_id)


@pytest.mark.parametrize(
    "test_case",
    [
        ExplicitDeploymentIdentityTestCase(
            description="derives a stable creation timestamp from an explicit identity",
            deployment_id="20260802T120000Z_reviewed",
            expected_created_at="2026-08-02 12:00:00.000",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_deployment_id_when_building_identity_then_created_at_is_deterministic(
    test_case: ExplicitDeploymentIdentityTestCase,
) -> None:
    identity: BackfillDeploymentIdentity = build_backfill_deployment_identity(
        deployment_id=test_case.deployment_id
    )

    assert identity.deployment_id == test_case.deployment_id
    assert identity.created_at == test_case.expected_created_at
