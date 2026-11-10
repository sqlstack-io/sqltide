import json
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

from sqltide._native import WarehouseRuntime
from sqltide.adapter.models import (
    AdapterQueryCancellation,
    AdapterQueryResult,
    AdapterRunEventRecord,
)
from sqltide.dev_server.types import KafkaLagOffsets
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


class CancellingProcess:
    def __init__(self) -> None:
        self.signal_received: int | None = None
        self.terminated: bool = False
        self.killed: bool = False
        self.terminate_requested: threading.Event = threading.Event()
        self.exit_requested: threading.Event = threading.Event()
        self._wait_actions: Iterator[Callable[[float | None], int]] = iter(
            (self._timeout_wait, self._timeout_wait, self._exit_wait)
        )

    def poll(self) -> int | None:
        return None

    def send_signal(self, requested_signal: int) -> None:
        self.signal_received = requested_signal

    def wait(self, timeout: float | None = None) -> int:
        return next(self._wait_actions)(timeout)

    def _timeout_wait(self, timeout: float | None) -> int:
        del timeout
        raise subprocess.TimeoutExpired(cmd="sqt build", timeout=1.0)

    def _exit_wait(self, timeout: float | None) -> int:
        del timeout
        self.exit_requested.wait(timeout=5.0)
        return -9

    def terminate(self) -> None:
        self.terminated = True
        self.terminate_requested.set()

    def kill(self) -> None:
        self.killed = True
        self.exit_requested.set()


def expected_interrupt_signal() -> int:
    return signal.SIGINT


class PendingBuildOutput:
    def __init__(self) -> None:
        self.release: threading.Event = threading.Event()

    def __iter__(self) -> Iterator[str]:
        self.release.wait(timeout=5)
        return iter(())


class PendingBuildProcess:
    def __init__(self) -> None:
        self.stdout: PendingBuildOutput = PendingBuildOutput()
        self.poll_result: int | None = None
        self.signal_received: int | None = None
        self.terminated: bool = False
        self.killed: bool = False

    def poll(self) -> int | None:
        return self.poll_result

    def wait(self, timeout: float | None = None) -> int:
        _ = self.stdout.release.wait(timeout=timeout)
        return 0

    def send_signal(self, requested_signal: int) -> None:
        self.signal_received = requested_signal

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def finish(self) -> None:
        self.poll_result = 0
        self.stdout.release.set()


class ActiveQueryBuildOutput:
    def __init__(self, *, invocation_id: str, query_id: str) -> None:
        self._line: str = json.dumps(
            {
                "event": "statement_started",
                "invocationId": invocation_id,
                "sequence": 2,
                "statementSequence": 1,
                "stepId": "replay_orders",
                "phase": "replay",
                "queryId": query_id,
            }
        )
        self.started: threading.Event = threading.Event()
        self.release: threading.Event = threading.Event()

    def __iter__(self) -> Iterator[str]:
        yield self._line
        self.started.set()
        self.release.wait(timeout=5.0)


class ActiveQueryBuildProcess:
    def __init__(self, *, invocation_id: str, query_id: str) -> None:
        self.stdout: ActiveQueryBuildOutput = ActiveQueryBuildOutput(
            invocation_id=invocation_id, query_id=query_id
        )
        self.poll_result: int | None = None
        self.signal_received: int | None = None

    def poll(self) -> int | None:
        return self.poll_result

    def wait(self, timeout: float | None = None) -> int:
        _ = self.stdout.release.wait(timeout=timeout)
        return 0

    def send_signal(self, requested_signal: int) -> None:
        self.signal_received = requested_signal

    def terminate(self) -> None:
        self.stdout.release.set()

    def kill(self) -> None:
        self.stdout.release.set()

    def finish(self) -> None:
        self.poll_result = 0
        self.stdout.release.set()


class CancellationRecordingConnection(RecordingAdapterConnection):
    def __init__(self, *, next_sequence: int) -> None:
        super().__init__()
        self.next_sequence = next_sequence
        self.cancelled_query_ids: list[str] = []
        self.run_events: list[AdapterRunEventRecord] = []

    def cancel_workflow_query(self, *, query_id: str) -> AdapterQueryCancellation:
        self.cancelled_query_ids.append(query_id)
        return AdapterQueryCancellation(
            query_id=query_id,
            supported=True,
            query_found=True,
            termination_confirmed=True,
        )

    def query(self, statement: str) -> AdapterQueryResult:
        self.statements.append(statement)
        return AdapterQueryResult(column_names=("sequence",), rows=((self.next_sequence,),))

    def render_run_events(
        self,
        *,
        database: str,
        events: tuple[AdapterRunEventRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        del database, include_migration
        self.run_events.extend(events)
        return ()


class CancellationWarehouseRuntime:
    def __init__(self, connection: CancellationRecordingConnection) -> None:
        self.connection = connection

    @contextmanager
    def read_connection(self) -> Iterator[CancellationRecordingConnection]:
        yield self.connection

    def as_runtime(self) -> WarehouseRuntime:
        return cast(WarehouseRuntime, self)


class ScriptedLagCollector:
    """Answers every broker offset read from one scripted response and records the reads."""

    def __init__(self, *, respond: Callable[[], KafkaLagOffsets]) -> None:
        self._respond = respond
        self.calls: tuple[tuple[str, str], ...] = ()

    def __call__(
        self,
        *,
        key: tuple[str, tuple[tuple[str, str], ...]],
        config: dict[str, object],
        topic: str,
        consumer_group: str,
    ) -> KafkaLagOffsets:
        del key, config
        self.calls = (*self.calls, (consumer_group, topic))
        return self._respond()


def broker_unavailable() -> KafkaLagOffsets:
    raise ConnectionError("broker unavailable")
