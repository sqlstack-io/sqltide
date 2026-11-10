import pytest

from sqltide._native import run_deployment_list, run_deployment_show
from sqltide.adapter.models import AdapterDeploymentInventory, AdapterDeploymentRecord
from tests.unit.src.sqltide.cli.deployment.main._test_types import (
    DeploymentReadCommandTestCase,
)
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentReadCommandTestCase(
            description="lists authoritative inventory without warehouse mutations",
            expected_exit_code=0,
            expected_output_fragment='"deployments": []',
        )
    ],
    ids=lambda case: case.description,
)
def test_given_inventory_when_listing_deployments_then_command_is_read_only(
    test_case: DeploymentReadCommandTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client: RecordingAdapterConnection = RecordingAdapterConnection()

    exit_code: int = run_deployment_list(
        database="analytics",
        metadata_database="metadata",
        json_output=True,
        client=client,
    )

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_output_fragment in capsys.readouterr().out
    assert client.workflow_mutation_statements == []


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentReadCommandTestCase(
            description="shows authoritative deployment without warehouse mutations",
            expected_exit_code=0,
            expected_output_fragment='"deployment_id": "20260806T000100Z_staged"',
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_when_showing_deployment_then_command_is_read_only(
    test_case: DeploymentReadCommandTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=AdapterDeploymentInventory(
            deployments=(
                AdapterDeploymentRecord(
                    deployment_id="20260806T000100Z_staged",
                    created_at="2026-08-06 00:01:00.000",
                    status="staged",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=(),
                ),
            ),
            publish_events=(),
        )
    )

    exit_code: int = run_deployment_show(
        deployment_id="20260806T000100Z_staged",
        database="analytics",
        metadata_database="metadata",
        json_output=True,
        client=client,
    )

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_output_fragment in capsys.readouterr().out
    assert client.workflow_mutation_statements == []
