"""Small single-process audit scheduler owned by the dev-server lifecycle."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from sqltide._native import (
    DevServerState,
    read_active_runs,
    read_latest_applied_direct_build_at,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.entry.types import CliCommand
from sqltide.compiler.discovery.constants import DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.classes.build_process import BuildProcessManager
from sqltide.dev_server.classes.warehouse_runtime import WarehouseRuntime
from sqltide.dev_server.main._build_audit_scheduler_payload import (
    build_audit_scheduler_payload,
)
from sqltide.dev_server.main._execute_due_audits import execute_due_audits
from sqltide.dev_server.main._scheduler_enabled import scheduler_enabled
from sqltide.dev_server.types import AuditScheduleState, RunPresentationStatus
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)
from sqltide.executor.observability.types import QualityResultTrigger

_POLL_SECONDS: float = 10.0
_MAX_BACKOFF_SECONDS: float = 300.0


class AuditScheduler:
    """Poll current compile and metadata state, then run one due batch at a time."""

    def __init__(
        self,
        *,
        state: DevServerState,
        warehouse: WarehouseRuntime | None = None,
        connection: AdapterConnection | None = None,
        observation_connection: AdapterConnection | None = None,
        database: str | None,
        project_dir: Path,
        builds: BuildProcessManager,
        presumed_failed_after_seconds: int = DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS,
    ) -> None:
        self._state = state
        self._warehouse = warehouse or WarehouseRuntime(
            connection=connection,
            observation_connection=observation_connection,
            connection_factory=None,
            observation_connection_factory=None,
            database=database,
        )
        self._database = database
        self._project_dir: Path = project_dir
        self._builds = builds
        self._presumed_failed_after_seconds = presumed_failed_after_seconds
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._health_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._health_state: AuditScheduleState = (
            AuditScheduleState.IDLE
            if self._warehouse.connection is not None and database is not None
            else AuditScheduleState.DISABLED
        )
        self._consecutive_errors = 0
        self._backoff_until = 0.0
        self._latest_error: str | None = None
        self._last_successful_tick: str | None = None
        self._running_audit_count = 0

    def start(self) -> None:
        """Start the daemon polling loop once."""

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sqltide-audits", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop polling and wait briefly for the current tick."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def tick(self) -> int:
        """Run at most one due batch, returning its terminal result count."""

        if self._seconds_until_attempt() > 0:
            return 0
        try:
            return self._tick_once()
        except Exception as error:
            self._record_error(error)
            raise

    def health(self) -> dict[str, object]:
        """Return the process-local scheduler lifecycle and backoff state."""

        with self._health_lock:
            return {
                "state": self._health_state,
                "consecutiveErrors": self._consecutive_errors,
                "latestError": self._latest_error,
                "backoffSeconds": self._seconds_until_attempt(),
                "nextTickSeconds": max(self._seconds_until_attempt(), _POLL_SECONDS),
                "lastSuccessfulTick": self._last_successful_tick,
                "runningAuditCount": self._running_audit_count,
            }

    def _tick_once(self) -> int:
        connection: AdapterConnection | None = self._warehouse.connection
        if connection is None or self._database is None:
            self._record_success(state=AuditScheduleState.DISABLED)
            return 0
        local_feed: dict[str, object] = self._builds.feed(after=0)
        if bool(local_feed["running"]):
            self._record_blocked(
                warning=f"local build {local_feed.get('invocationId') or 'unknown'} is running"
            )
            return 0
        if not self._tick_lock.acquire(blocking=False):
            return 0
        try:
            with self._state.query_lock:
                local_feed = self._builds.feed(after=0)
                if bool(local_feed["running"]):
                    self._record_blocked(
                        warning=(
                            f"local build {local_feed.get('invocationId') or 'unknown'} is running"
                        )
                    )
                    return 0
                analysis: CompileAnalysis = self._state.current_analysis()
                if not scheduler_enabled(analysis):
                    self._record_success(state=AuditScheduleState.DISABLED)
                    return 0
                blocking_build: dict[str, object] | None = self._blocking_mutating_build()
                if blocking_build is not None:
                    self._record_blocked(
                        warning=(
                            f"build {blocking_build.get('invocationId') or 'unknown'} is "
                            f"{blocking_build['status']}"
                        )
                    )
                    return 0
                if self._has_active_scheduled_audit():
                    self._record_blocked(
                        warning="another scheduler process has an active scheduled audit run"
                    )
                    return 0
                payload: dict[str, object] = build_audit_scheduler_payload(
                    analysis=analysis,
                    connection=connection,
                    database=self._database,
                    project_dir=self._project_dir,
                )
                if not payload["enabled"]:
                    self._record_success(state=AuditScheduleState.DISABLED)
                    return 0
                raw_audits: object = payload["audits"]
                due_items: list[dict[str, object]] = []
                if isinstance(raw_audits, list):
                    for item in raw_audits:
                        if not isinstance(item, dict):
                            continue
                        typed_item: dict[str, object] = {
                            str(key): value for key, value in item.items()
                        }
                        if typed_item.get("state") == AuditScheduleState.DUE:
                            due_items.append(typed_item)
                due: tuple[dict[str, object], ...] = tuple(due_items)
                payload_state: AuditScheduleState = AuditScheduleState(str(payload["state"]))
                if not due:
                    self._record_success(
                        state=payload_state,
                        completed_at=str(payload["warehouseNow"]),
                    )
                    return 0
                self._set_running_audit_count(len(due))
                result_count: int = execute_due_audits(
                    analysis=analysis,
                    connection=connection,
                    observation_connection=self._warehouse.observation_connection,
                    database=self._database,
                    project_dir=self._project_dir,
                    due=due,
                )
                self._record_success(state=AuditScheduleState.IDLE)
                return result_count
        finally:
            self._set_running_audit_count(0)
            self._tick_lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(max(_POLL_SECONDS, self._seconds_until_attempt()))

    def _blocking_mutating_build(self) -> dict[str, object] | None:
        connection: AdapterConnection | None = self._warehouse.connection
        if connection is None or self._database is None:
            return None
        project_identity: str = logical_project_identity(project_dir=self._project_dir)
        latest_applied_at: str | None = read_latest_applied_direct_build_at(
            connection=connection,
            database=self._database,
            project_identity=project_identity,
        )
        for run in read_active_runs(
            connection=connection,
            database=self._database,
            presumed_failed_after_seconds=self._presumed_failed_after_seconds,
        ):
            if run["command"] != CliCommand.BUILD:
                continue
            run_project_identity: object = run.get("projectIdentity")
            if run_project_identity is not None and (
                str(run_project_identity).rsplit("/", maxsplit=1)[-1] != project_identity
            ):
                continue
            if run["status"] in {
                RunPresentationStatus.RUNNING,
                RunPresentationStatus.UNRESPONSIVE,
            }:
                return run
            if run["status"] == RunPresentationStatus.PRESUMED_FAILED and (
                latest_applied_at is None or str(run["startedAt"]) >= latest_applied_at
            ):
                return run
        return None

    def _has_active_scheduled_audit(self) -> bool:
        connection: AdapterConnection | None = self._warehouse.connection
        if connection is None or self._database is None:
            return False
        return any(
            run["command"] == CliCommand.AUDIT
            and run["mode"] == QualityResultTrigger.SCHEDULED
            and run["status"] in {RunPresentationStatus.RUNNING, RunPresentationStatus.UNRESPONSIVE}
            for run in read_active_runs(
                connection=connection,
                database=self._database,
                presumed_failed_after_seconds=self._presumed_failed_after_seconds,
            )
        )

    def _record_error(self, error: Exception) -> None:
        with self._health_lock:
            self._consecutive_errors += 1
            delay: float = min(
                _POLL_SECONDS * (2 ** (self._consecutive_errors - 1)),
                _MAX_BACKOFF_SECONDS,
            )
            self._backoff_until = monotonic() + delay
            self._latest_error = str(error)
            self._health_state = AuditScheduleState.BACKING_OFF

    def _record_success(
        self,
        *,
        state: AuditScheduleState,
        completed_at: str | None = None,
    ) -> None:
        with self._health_lock:
            self._consecutive_errors = 0
            self._backoff_until = 0.0
            self._latest_error = None
            self._health_state = state
            self._last_successful_tick = (
                completed_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            )

    def _seconds_until_attempt(self) -> float:
        with self._health_lock:
            return max(self._backoff_until - monotonic(), 0.0)

    def _record_blocked(self, *, warning: str) -> None:
        self._record_success(state=AuditScheduleState.BLOCKED)
        with self._health_lock:
            self._latest_error = warning

    def _set_running_audit_count(self, count: int) -> None:
        with self._health_lock:
            self._running_audit_count = count
            if count:
                self._health_state = AuditScheduleState.RUNNING
