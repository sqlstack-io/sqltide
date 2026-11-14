from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import DevServerState, create_dev_app
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from tests.integration.src.sqltide.adapters.clickhouse.helpers import connect_clickhouse
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.dev_server._test_types import (
    DeploymentRollbackRouteTestCase,
)
from tests.integration.src.sqltide.dev_server.helpers import write_rollback_history
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server.helpers import build_compile_callable

_FIRST: str = "20991231T100000Z_first"
_SECOND: str = "20991231T110000Z_second"
_STAGED: str = "20991231T120000Z_staged"
_UNCHANGED_STATES: dict[str, str] = {_FIRST: "superseded", _SECOND: "active", _STAGED: "staged"}


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentRollbackRouteTestCase(
            description="superseded deployment becomes active and the rollback is a recorded run",
            target_deployment_id=_FIRST,
            expected_status=200,
            expected_states={_FIRST: "active", _SECOND: "superseded", _STAGED: "staged"},
            expected_previous_deployment_id=_SECOND,
            expected_error_message=None,
            expected_rollback_run_count=1,
        ),
        DeploymentRollbackRouteTestCase(
            description="active deployment is refused and nothing changes",
            target_deployment_id=_SECOND,
            expected_status=400,
            expected_states=_UNCHANGED_STATES,
            expected_previous_deployment_id=None,
            expected_error_message=(
                f"Deployment '{_SECOND}' is active, not superseded; "
                "only a superseded deployment can be rolled back to"
            ),
            expected_rollback_run_count=0,
        ),
        DeploymentRollbackRouteTestCase(
            description="staged deployment is refused and nothing changes",
            target_deployment_id=_STAGED,
            expected_status=400,
            expected_states=_UNCHANGED_STATES,
            expected_previous_deployment_id=None,
            expected_error_message=(
                f"Deployment '{_STAGED}' is staged, not superseded; "
                "only a superseded deployment can be rolled back to"
            ),
            expected_rollback_run_count=0,
        ),
        DeploymentRollbackRouteTestCase(
            description="unknown deployment is refused and nothing changes",
            target_deployment_id="20991231T130000Z_unknown",
            expected_status=400,
            expected_states=_UNCHANGED_STATES,
            expected_previous_deployment_id=None,
            expected_error_message="Deployment '20991231T130000Z_unknown' was not found",
            expected_rollback_run_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_history_when_rolling_back_then_only_superseded_targets_go_live(
    test_case: DeploymentRollbackRouteTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    project: Path = tmp_path / "project"
    write_rollback_history(
        project=project,
        settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        database=clickhouse_database,
        first_deployment_id=_FIRST,
        second_deployment_id=_SECOND,
        staged_deployment_id=_STAGED,
    )
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings,
        database=clickhouse_database,
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=build_compile_callable(project_dir=project)),
            connection=connection,
            database=clickhouse_database,
            project_dir=project,
        )
    )

    try:
        response: NativeResponse = client.post(
            f"/api/deployments/{test_case.target_deployment_id}/rollback"
        )
        deployments: list[dict[str, object]] = client.get("/api/deployments").json()["deployments"]
        runs: list[dict[str, object]] = client.get("/api/runs").json()
    finally:
        connection.close()

    body: dict[str, object] = response.json()
    states: dict[object, object] = {
        deployment["deploymentId"]: deployment["state"] for deployment in deployments
    }
    recorded: list[tuple[object, ...]] = [
        (run["command"], run["invocationId"], run["status"], run["displayCommand"]) for run in runs
    ]
    expected_run: tuple[object, ...] = (
        "deployment rollback",
        body.get("invocationId"),
        "succeeded",
        f"sqt deployment rollback {test_case.target_deployment_id}",
    )
    assert response.status_code == test_case.expected_status
    assert body.get("previousDeploymentId") == test_case.expected_previous_deployment_id
    assert body.get("detail") == test_case.expected_error_message
    assert {key: states[key] for key in test_case.expected_states} == test_case.expected_states
    assert [run[0] for run in recorded].count("deployment rollback") == (
        test_case.expected_rollback_run_count
    )
    assert recorded.count(expected_run) == test_case.expected_rollback_run_count


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
