"""Behavior tests for the sensor observability and management routes."""

from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import create_dev_app
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.dev_server.classes.dev_server_state import DevServerState
from sqltide.dev_server.models import DevExecutionContext
from sqltide.sensors.classes.sensor_state_repository import SensorStateRepository
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    ReadConnectionRouteTestCase,
    SensorRouteErrorTestCase,
    SensorRoutesTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_compile_callable,
    build_test_client,
    write_dev_server_project,
    write_sensor_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ReadConnectionRouteTestCase(
            description="sensor inventory avoids the primary execution connection",
            path="/api/sensors",
            expected_status=200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_isolated_read_factory_when_reading_sensors_then_primary_connection_is_avoided(
    test_case: ReadConnectionRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    write_sensor_file(project_dir=tmp_path)
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

    with patch(
        "sqltide._native.build_sensors_payload",
        return_value={"sensors": [], "deadLetterCount": 0, "health": {}},
    ):
        response: NativeResponse = client.get(test_case.path)

    assert response.status_code == test_case.expected_status
    assert create_read.call_count == 1
    read_mock.close.assert_called_once()
    primary_mock.query.assert_not_called()


@pytest.mark.parametrize(
    "test_case",
    [
        ReadConnectionRouteTestCase(
            description="sensor status writes avoid the primary execution connection",
            path="/api/sensors/quality_alerts/status",
            expected_status=200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_isolated_connection_when_setting_sensor_status_then_primary_connection_is_avoided(
    test_case: ReadConnectionRouteTestCase, tmp_path: Path
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    write_sensor_file(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))
    primary_mock: MagicMock = MagicMock()
    isolated_mock: MagicMock = MagicMock()
    create_isolated: MagicMock = MagicMock(return_value=cast(AdapterConnection, isolated_mock))
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=cast(AdapterConnection, primary_mock),
            database="analytics",
            project_dir=tmp_path,
            execution_context=DevExecutionContext(
                database="analytics",
                observation_connection_factory=cast(
                    Callable[[], AdapterConnection], create_isolated
                ),
            ),
        )
    )

    with (
        patch.object(SensorStateRepository, "ensure_ready"),
        patch.object(SensorStateRepository, "record_override") as record_override,
    ):
        response: NativeResponse = client.post(test_case.path, json={"status": "running"})

    assert response.status_code == test_case.expected_status
    assert response.json() == {"sensorName": "quality_alerts", "status": "running"}
    assert create_isolated.call_count == 1
    isolated_mock.close.assert_called_once()
    primary_mock.query.assert_not_called()
    record_override.assert_called_once()


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRoutesTestCase(
            description="compiled sensors list with declared defaults and no warehouse",
            expected_sensor_names=("quality_alerts",),
            expected_effective_statuses=("stopped",),
            expected_event_types=("AuditCompleted",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_authored_sensor_when_listing_then_definitions_are_served(
    test_case: SensorRoutesTestCase, tmp_path: Path
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    write_sensor_file(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/sensors")

    assert response.status_code == 200
    payload: dict[str, object] = response.json()
    sensors: list[dict[str, object]] = payload["sensors"]
    assert tuple(str(sensor["name"]) for sensor in sensors) == (test_case.expected_sensor_names)
    assert tuple(str(sensor["effectiveStatus"]) for sensor in sensors) == (
        test_case.expected_effective_statuses
    )
    assert tuple(sensor.get("eventType") for sensor in sensors) == (test_case.expected_event_types)
    assert payload["deadLetterCount"] == 0


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRouteErrorTestCase(
            description="tick history for unknown sensors is not found",
            method="GET",
            path="/api/sensors/missing_sensor/ticks",
            body=None,
            expected_status_code=404,
            expected_detail_fragment="Unknown sensor 'missing_sensor'",
        ),
        SensorRouteErrorTestCase(
            description="tick windows require ISO timestamps",
            method="GET",
            path="/api/sensors/quality_alerts/ticks?after=not-a-time",
            body=None,
            expected_status_code=400,
            expected_detail_fragment="after must be an ISO timestamp",
        ),
        SensorRouteErrorTestCase(
            description="unknown override statuses are rejected",
            method="POST",
            path="/api/sensors/quality_alerts/status",
            body={"status": "paused"},
            expected_status_code=400,
            expected_detail_fragment="Unknown sensor status 'paused'",
        ),
        SensorRouteErrorTestCase(
            description="valid overrides without a warehouse are unavailable",
            method="POST",
            path="/api/sensors/quality_alerts/status",
            body={"status": "running"},
            expected_status_code=503,
            expected_detail_fragment="no warehouse connection",
        ),
        SensorRouteErrorTestCase(
            description="dead-letter retries for unknown sensors are not found",
            method="POST",
            path="/api/sensors/dead-letters/retry",
            body={"sensorName": "missing_sensor", "eventId": "event-1"},
            expected_status_code=404,
            expected_detail_fragment="Unknown sensor 'missing_sensor'",
        ),
        SensorRouteErrorTestCase(
            description="dead-letter skips require a reason",
            method="POST",
            path="/api/sensors/dead-letters/skip",
            body={"sensorName": "quality_alerts", "eventId": "event-1", "reason": "  "},
            expected_status_code=400,
            expected_detail_fragment="requires a reason",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sensor_requests_when_calling_then_errors_are_structured(
    test_case: SensorRouteErrorTestCase, tmp_path: Path
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    write_sensor_file(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.request(test_case.method, test_case.path, json=test_case.body)

    assert response.status_code == test_case.expected_status_code
    assert test_case.expected_detail_fragment in str(response.json()["detail"])


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
