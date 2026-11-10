import threading
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import AuditScheduler, BuildProcessManager, DevServerState
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.compiler.pipeline.models import CompileAnalysis
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    AuditSchedulerActiveRunTestCase,
    AuditSchedulerBackoffTestCase,
    AuditSchedulerLocalRaceTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        AuditSchedulerActiveRunTestCase(
            description="presumed failed build remains blocked without a later successful build",
            active_runs=(
                {
                    "invocationId": "blocked-build",
                    "command": "build",
                    "mode": "direct",
                    "status": "presumed_failed",
                    "startedAt": "2026-08-08 12:00:00.000",
                    "projectIdentity": "/opt/sqltide-v1/project",
                },
            ),
            latest_applied_at=None,
            expected_payload_reads=0,
            expected_latest_error="build blocked-build is presumed_failed",
        ),
        AuditSchedulerActiveRunTestCase(
            description="later successful build recovers an older presumed failed build",
            active_runs=(
                {
                    "invocationId": "recovered-build",
                    "command": "build",
                    "mode": "direct",
                    "status": "presumed_failed",
                    "startedAt": "2026-08-08 11:00:00.000",
                    "projectIdentity": "/opt/sqltide-v1/project",
                },
            ),
            latest_applied_at="2026-08-08 12:00:00.000",
            expected_payload_reads=1,
            expected_latest_error=None,
        ),
        AuditSchedulerActiveRunTestCase(
            description="active build for another logical project does not block audits",
            active_runs=(
                {
                    "invocationId": "unrelated-build",
                    "command": "build",
                    "mode": "direct",
                    "status": "running",
                    "startedAt": "2026-08-08 11:00:00.000",
                    "projectIdentity": "another-project",
                },
            ),
            latest_applied_at="2026-08-08 12:00:00.000",
            expected_payload_reads=1,
            expected_latest_error=None,
        ),
        AuditSchedulerActiveRunTestCase(
            description="expired scheduler heartbeat does not block recovery forever",
            active_runs=(
                {
                    "command": "audit",
                    "mode": "scheduled",
                    "status": "presumed_failed",
                    "startedAt": "2026-08-08 11:00:00.000",
                },
            ),
            latest_applied_at=None,
            expected_payload_reads=1,
            expected_latest_error=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_active_run_history_when_ticking_then_scheduler_uses_recoverable_safety_guard(
    test_case: AuditSchedulerActiveRunTestCase,
) -> None:
    state_mock: MagicMock = MagicMock()
    state_mock.query_lock = threading.Lock()
    state_mock.current_analysis.return_value = cast(CompileAnalysis, MagicMock())
    builds_mock: MagicMock = MagicMock()
    builds_mock.feed.return_value = {"running": False}
    scheduler: AuditScheduler = AuditScheduler(
        state=cast(DevServerState, state_mock),
        connection=cast(AdapterConnection, MagicMock()),
        database="analytics",
        project_dir=Path("/project"),
        builds=cast(BuildProcessManager, builds_mock),
    )
    with (
        patch(
            "sqltide._native.scheduler_enabled",
            return_value=True,
        ),
        patch(
            "sqltide._native.read_active_runs",
            return_value=list(test_case.active_runs),
        ),
        patch(
            "sqltide._native.read_latest_applied_direct_build_at",
            return_value=test_case.latest_applied_at,
        ),
        patch(
            "sqltide._native.build_audit_scheduler_payload",
            return_value={
                "enabled": True,
                "state": "idle",
                "warehouseNow": "2026-08-08 12:00:00.000",
                "audits": [],
            },
        ) as build_payload,
    ):
        result_count: int = scheduler.tick()

    assert result_count == 0
    assert build_payload.call_count == test_case.expected_payload_reads
    assert scheduler.health()["latestError"] == test_case.expected_latest_error


@pytest.mark.parametrize(
    "test_case",
    [
        AuditSchedulerLocalRaceTestCase(
            description="build appears before scheduler acquires shared lock",
            expected_feed_reads=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_build_starts_during_lock_wait_when_ticking_then_guard_is_rechecked(
    test_case: AuditSchedulerLocalRaceTestCase,
) -> None:
    state_mock: MagicMock = MagicMock()
    state_mock.query_lock = threading.Lock()
    state_mock.current_analysis.return_value = cast(CompileAnalysis, MagicMock())
    builds_mock: MagicMock = MagicMock()
    builds_mock.feed.side_effect = ({"running": False}, {"running": True})
    scheduler: AuditScheduler = AuditScheduler(
        state=cast(DevServerState, state_mock),
        connection=cast(AdapterConnection, MagicMock()),
        database="analytics",
        project_dir=Path("/project"),
        builds=cast(BuildProcessManager, builds_mock),
    )

    with patch("sqltide._native.build_audit_scheduler_payload") as build_payload:
        result_count: int = scheduler.tick()

    assert result_count == 0
    assert builds_mock.feed.call_count == test_case.expected_feed_reads
    build_payload.assert_not_called()


@pytest.mark.parametrize(
    "test_case",
    [
        AuditSchedulerBackoffTestCase(
            description="warehouse outage backs off and successful read clears health",
            error_message="warehouse unavailable",
            expected_initial_backoff_seconds=10.0,
            expected_result_count_after_recovery=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_scheduler_read_error_when_warehouse_recovers_then_backoff_is_cleared(
    test_case: AuditSchedulerBackoffTestCase,
) -> None:
    state_mock: MagicMock = MagicMock()
    state_mock.query_lock = threading.Lock()
    state_mock.current_analysis.return_value = cast(CompileAnalysis, MagicMock())
    state: DevServerState = cast(DevServerState, state_mock)
    connection: AdapterConnection = cast(AdapterConnection, MagicMock())
    builds_mock: MagicMock = MagicMock()
    builds_mock.feed.return_value = {"running": False}
    builds: BuildProcessManager = cast(BuildProcessManager, builds_mock)
    scheduler: AuditScheduler = AuditScheduler(
        state=state,
        connection=connection,
        database="analytics",
        project_dir=Path("/project"),
        builds=builds,
    )
    clock: MagicMock = MagicMock(return_value=0.0)
    recovered_payload: dict[str, object] = {
        "enabled": True,
        "state": "due",
        "warehouseNow": "2026-08-08 12:00:00.000",
        "audits": [
            {
                "name": "orders are valid",
                "state": "due",
                "scheduledFor": "2026-08-08 12:00:00.000",
            }
        ],
    }
    with (
        patch("time.monotonic", clock),
        patch(
            "sqltide._native.scheduler_enabled",
            return_value=True,
        ),
        patch(
            "sqltide._native.read_active_runs",
            return_value=[],
        ),
        patch(
            "sqltide._native.read_latest_applied_direct_build_at",
            return_value=None,
        ),
        patch(
            "sqltide._native.build_audit_scheduler_payload",
            side_effect=[AdapterWarehouseError(test_case.error_message), recovered_payload],
        ) as build_payload,
        patch(
            "sqltide._native.execute_due_audits",
            return_value=test_case.expected_result_count_after_recovery,
        ),
    ):
        with pytest.raises(AdapterWarehouseError, match=test_case.error_message):
            scheduler.tick()
        failed_health: dict[str, object] = scheduler.health()
        skipped_result: int = scheduler.tick()
        clock.return_value = test_case.expected_initial_backoff_seconds + 1
        recovered_result: int = scheduler.tick()
        recovered_health: dict[str, object] = scheduler.health()

    assert failed_health["state"] == "backing_off"
    assert failed_health["consecutiveErrors"] == 1
    assert failed_health["latestError"] == test_case.error_message
    assert failed_health["backoffSeconds"] == test_case.expected_initial_backoff_seconds
    assert skipped_result == 0
    assert build_payload.call_count == 2
    assert recovered_result == test_case.expected_result_count_after_recovery
    assert recovered_health["consecutiveErrors"] == 0
    assert recovered_health["latestError"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
