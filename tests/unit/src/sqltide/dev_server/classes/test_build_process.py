import subprocess
import threading
from pathlib import Path
from typing import cast

import pytest

from sqltide._native import SilentDevServerReporter
from sqltide.adapter.models import AdapterQueryCancellation
from sqltide.dev_server.classes.build_process import BuildProcessManager
from sqltide.dev_server.models import BuildCancellationOutcome
from sqltide.executor.observability.constants import RUN_INVOCATION_ID_ENV_VAR
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    BuildCancellationStateTestCase,
    BuildStartTestCase,
    BuildWarehouseCancellationTestCase,
)
from tests.unit.src.sqltide.dev_server.classes.helpers import (
    ActiveQueryBuildProcess,
    CancellingProcess,
    PendingBuildProcess,
    expected_interrupt_signal,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BuildCancellationStateTestCase(
            description="force availability survives cancellation request completion",
            invocation_id="owned-invocation",
            expected_cancel_status="cancelling",
            expected_force_available=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_graceful_cancel_timeout_when_polling_then_force_action_remains_available(
    test_case: BuildCancellationStateTestCase,
) -> None:
    process: CancellingProcess = CancellingProcess()
    outcome_ready: threading.Event = threading.Event()
    manager: BuildProcessManager = BuildProcessManager(
        reporter=SilentDevServerReporter(),
        cancellation_reporter=lambda outcome: outcome_ready.set(),
    )
    manager._process = cast(subprocess.Popen[str], process)
    manager._invocation_id = test_case.invocation_id

    result: dict[str, object] = manager.cancel(invocation_id=test_case.invocation_id)
    assert process.terminate_requested.wait(timeout=2.0)
    feed: dict[str, object] = manager.feed(after=0)
    repeated: dict[str, object] = manager.cancel(invocation_id=test_case.invocation_id)
    manager.kill(invocation_id=test_case.invocation_id)
    assert outcome_ready.wait(timeout=2.0)

    assert result["status"] == test_case.expected_cancel_status
    assert feed["forceAvailable"] is test_case.expected_force_available
    assert repeated["forceAvailable"] is test_case.expected_force_available
    assert process.signal_received == expected_interrupt_signal()
    assert process.terminated is True
    assert process.killed is True


@pytest.mark.parametrize(
    "test_case",
    [
        BuildWarehouseCancellationTestCase(
            description="confirmed warehouse query cancellation",
            invocation_id="owned-invocation",
            query_id="query-123",
            termination_confirmed=True,
            cancellation_detail=None,
            expected_terminal_status="cancelled",
            expected_error=None,
        ),
        BuildWarehouseCancellationTestCase(
            description="unconfirmed warehouse query cancellation",
            invocation_id="owned-invocation",
            query_id="query-456",
            termination_confirmed=False,
            cancellation_detail="ClickHouse query remained active after KILL QUERY SYNC",
            expected_terminal_status="cancellation_failed",
            expected_error="ClickHouse query remained active after KILL QUERY SYNC",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_active_query_when_cancelling_then_manager_reports_confirmed_warehouse_outcome(
    test_case: BuildWarehouseCancellationTestCase,
) -> None:
    process: PendingBuildProcess = PendingBuildProcess()
    outcome_ready: threading.Event = threading.Event()
    outcomes: list[BuildCancellationOutcome] = []

    def cancel_query(query_id: str) -> AdapterQueryCancellation:
        return AdapterQueryCancellation(
            query_id=query_id,
            supported=True,
            query_found=True,
            termination_confirmed=test_case.termination_confirmed,
            detail=test_case.cancellation_detail,
        )

    def record_outcome(outcome: BuildCancellationOutcome) -> None:
        outcomes.append(outcome)
        outcome_ready.set()

    manager: BuildProcessManager = BuildProcessManager(
        reporter=SilentDevServerReporter(),
        query_canceller=cancel_query,
        cancellation_reporter=record_outcome,
    )
    manager._process = cast(subprocess.Popen[str], process)
    manager._invocation_id = test_case.invocation_id
    manager._active_query_id = test_case.query_id

    initial: dict[str, object] = manager.cancel(invocation_id=test_case.invocation_id)
    process.finish()
    assert outcome_ready.wait(timeout=2.0)
    terminal: dict[str, object] = manager.feed(after=0)

    assert initial["status"] == "cancelling"
    assert initial["queryId"] == test_case.query_id
    assert terminal["cancellationStatus"] == test_case.expected_terminal_status
    assert outcomes[0].query_id == test_case.query_id
    assert outcomes[0].warehouse_termination_confirmed is test_case.termination_confirmed
    assert terminal["cancellationError"] == test_case.expected_error


@pytest.mark.parametrize(
    "test_case",
    [
        BuildWarehouseCancellationTestCase(
            description="statement event supplies exact warehouse query identity",
            invocation_id="owned-invocation",
            query_id="query-from-event",
            termination_confirmed=True,
            cancellation_detail=None,
            expected_terminal_status="cancelled",
            expected_error=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_statement_started_event_when_cancelling_then_exact_query_id_is_forwarded(
    test_case: BuildWarehouseCancellationTestCase,
) -> None:
    process: ActiveQueryBuildProcess = ActiveQueryBuildProcess(
        invocation_id=test_case.invocation_id, query_id=test_case.query_id
    )
    cancelled_query_ids: list[str] = []
    cancellation_started: threading.Event = threading.Event()
    outcome_ready: threading.Event = threading.Event()

    def cancel_query(query_id: str) -> AdapterQueryCancellation:
        cancelled_query_ids.append(query_id)
        cancellation_started.set()
        return AdapterQueryCancellation(
            query_id=query_id,
            supported=True,
            query_found=True,
            termination_confirmed=True,
        )

    manager: BuildProcessManager = BuildProcessManager(
        reporter=SilentDevServerReporter(),
        query_canceller=cancel_query,
        cancellation_reporter=lambda outcome: outcome_ready.set(),
    )
    manager._process = cast(subprocess.Popen[str], process)
    manager._invocation_id = test_case.invocation_id
    consumer: threading.Thread = threading.Thread(
        target=manager._consume_stdout,
        kwargs={"process": manager._process, "launch_invocation_id": test_case.invocation_id},
        daemon=True,
    )
    consumer.start()
    assert process.stdout.started.wait(timeout=2.0)

    result: dict[str, object] = manager.cancel(invocation_id=test_case.invocation_id)
    assert cancellation_started.wait(timeout=2.0)
    process.finish()
    assert outcome_ready.wait(timeout=2.0)
    consumer.join(timeout=2.0)

    assert result["queryId"] == test_case.query_id
    assert cancelled_query_ids == [test_case.query_id]
    assert manager.feed(after=0)["cancellationStatus"] == test_case.expected_terminal_status


@pytest.mark.parametrize(
    "test_case",
    [
        BuildStartTestCase(
            description="child launch returns its owned identity before run start",
            selector="orders",
            expected_status="starting",
            expected_running=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_child_has_not_started_when_launching_then_returns_owned_identity_immediately(
    test_case: BuildStartTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process: PendingBuildProcess = PendingBuildProcess()
    child_environment: dict[str, str] = {}

    def start_process(*args: object, **kwargs: object) -> PendingBuildProcess:
        del args
        child_environment.update(cast(dict[str, str], kwargs["env"]))
        return process

    monkeypatch.setattr(subprocess, "Popen", start_process)
    manager: BuildProcessManager = BuildProcessManager(reporter=SilentDevServerReporter())

    result: dict[str, object] = manager.start(
        project_dir=Path("/project"),
        selectors=(test_case.selector,),
        start_time=None,
    )
    feed: dict[str, object] = manager.feed(after=0)

    assert result["status"] == test_case.expected_status
    assert result["invocationId"] == feed["invocationId"]
    assert feed["running"] is test_case.expected_running
    assert feed["currentInvocationId"] is None
    assert child_environment[RUN_INVOCATION_ID_ENV_VAR] == result["invocationId"]
    process.finish()
