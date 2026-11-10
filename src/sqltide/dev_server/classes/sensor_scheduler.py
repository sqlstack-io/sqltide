"""Small single-process sensor dispatch host owned by the dev-server lifecycle."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from sqltide._native import DevServerState, sensors_enabled
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.classes.warehouse_runtime import WarehouseRuntime
from sqltide.dev_server.types import SensorSchedulerState
from sqltide.sensors.classes.sensor_dispatcher import SensorDispatcher
from sqltide.sensors.classes.sensor_state_repository import SensorStateRepository
from sqltide.sensors.models import SensorDispatchSummary

_POLL_SECONDS: float = 10.0
_MAX_BACKOFF_SECONDS: float = 300.0
_RETENTION_INTERVAL_SECONDS: float = 3600.0


class SensorScheduler:
    """Poll current compile state and dispatch running sensors one pass at a time."""

    def __init__(
        self,
        *,
        state: DevServerState,
        warehouse: WarehouseRuntime,
        database: str | None,
    ) -> None:
        self._state: DevServerState = state
        self._warehouse: WarehouseRuntime = warehouse
        self._database: str | None = database
        self._stop: threading.Event = threading.Event()
        self._tick_lock: threading.Lock = threading.Lock()
        self._health_lock: threading.RLock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._health_state: SensorSchedulerState = (
            SensorSchedulerState.IDLE
            if warehouse.connection is not None and database is not None
            else SensorSchedulerState.DISABLED
        )
        self._consecutive_errors: int = 0
        self._backoff_until: float = 0.0
        self._latest_error: str | None = None
        self._last_successful_tick: str | None = None
        self._last_summary: SensorDispatchSummary | None = None
        self._retention_applied_at: float = 0.0
        self._dispatcher_id: str = uuid4().hex

    @property
    def repository(self) -> SensorStateRepository | None:
        """The shared sensor state repository, if the warehouse is configured."""

        connection: AdapterConnection | None = self._warehouse.connection
        if connection is None or self._database is None:
            return None
        return SensorStateRepository(connection=connection, database=self._database)

    def build_dispatcher(self, *, analysis: CompileAnalysis) -> SensorDispatcher | None:
        """Build one dispatcher over the shared repository for the current compile."""

        repository: SensorStateRepository | None = self.repository
        if repository is None or self._database is None:
            return None
        return SensorDispatcher(
            repository=repository,
            event_target=analysis.compiled_project.target_name or self._database,
            providers=(() if analysis.sensors is None else analysis.sensors.providers),
            dispatcher_id=self._dispatcher_id,
            maximum_event_age_seconds=_maximum_event_age_seconds(analysis=analysis),
        )

    def start(self) -> None:
        """Start the daemon polling loop once."""

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sqltide-sensors", daemon=True)
        self._thread.start()

    def initialize_event_checkpoints(self) -> None:
        """Initialize new event streams before producers start."""

        if self.repository is None or self._database is None:
            return
        with self._state.query_lock:
            analysis: CompileAnalysis = self._state.current_analysis()
            if not sensors_enabled(analysis):
                return
            if analysis.sensors is None or not analysis.sensors.registry.sensors:
                return
            dispatcher: SensorDispatcher | None = self.build_dispatcher(analysis=analysis)
            if dispatcher is None:
                return
            dispatcher.initialize_event_checkpoints(
                registry=analysis.sensors.registry,
                target=self._database,
            )

    def close(self) -> None:
        """Stop polling and wait briefly for the current tick."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def tick(self) -> SensorDispatchSummary | None:
        """Run at most one dispatch pass, returning its summary."""

        if self._seconds_until_attempt() > 0:
            return None
        try:
            return self._tick_once()
        except Exception as error:
            self._record_error(error)
            raise

    def health(self) -> dict[str, object]:
        """Return the process-local dispatcher lifecycle and backoff state."""

        with self._health_lock:
            summary: SensorDispatchSummary | None = self._last_summary
            return {
                "state": self._health_state,
                "consecutiveErrors": self._consecutive_errors,
                "latestError": self._latest_error,
                "backoffSeconds": self._seconds_until_attempt(),
                "nextTickSeconds": max(self._seconds_until_attempt(), _POLL_SECONDS),
                "lastSuccessfulTick": self._last_successful_tick,
                "lastEvaluatedCount": None if summary is None else summary.evaluated,
                "leaseHeld": None if summary is None else summary.lease_acquired,
            }

    def _tick_once(self) -> SensorDispatchSummary | None:
        if self.repository is None or self._database is None:
            self._record_success(state=SensorSchedulerState.DISABLED)
            return None
        if not self._tick_lock.acquire(blocking=False):
            return None
        try:
            with self._state.query_lock:
                analysis: CompileAnalysis = self._state.current_analysis()
                if not sensors_enabled(analysis):
                    self._record_success(state=SensorSchedulerState.DISABLED)
                    return None
                if analysis.sensors is None or not analysis.sensors.registry.sensors:
                    self._record_success(state=SensorSchedulerState.DISABLED)
                    return None
                dispatcher: SensorDispatcher | None = self.build_dispatcher(analysis=analysis)
                if dispatcher is None:
                    self._record_success(state=SensorSchedulerState.DISABLED)
                    return None
                summary: SensorDispatchSummary = dispatcher.dispatch_once(
                    registry=analysis.sensors.registry,
                    target=self._database,
                )
                self._maybe_apply_retention(analysis=analysis)
                self._record_success(
                    state=(
                        SensorSchedulerState.IDLE
                        if summary.lease_acquired
                        else SensorSchedulerState.STANDBY
                    ),
                    summary=summary,
                )
                return summary
        finally:
            self._tick_lock.release()

    def _maybe_apply_retention(self, *, analysis: CompileAnalysis) -> None:
        retention_days: int = _tick_retention_days(analysis=analysis)
        repository: SensorStateRepository | None = self.repository
        if retention_days <= 0 or repository is None:
            return
        if monotonic() - self._retention_applied_at < _RETENTION_INTERVAL_SECONDS:
            return
        repository.apply_tick_retention(retention_days=retention_days)
        self._retention_applied_at = monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _ = self.tick()
            except Exception:
                pass
            self._stop.wait(max(_POLL_SECONDS, self._seconds_until_attempt()))

    def _seconds_until_attempt(self) -> float:
        with self._health_lock:
            return max(0.0, self._backoff_until - monotonic())

    def _record_success(
        self,
        *,
        state: SensorSchedulerState,
        summary: SensorDispatchSummary | None = None,
    ) -> None:
        with self._health_lock:
            self._health_state = state
            self._consecutive_errors = 0
            self._backoff_until = 0.0
            self._latest_error = None
            self._last_successful_tick = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self._last_summary = summary if summary is not None else self._last_summary

    def _record_error(self, error: Exception) -> None:
        with self._health_lock:
            self._consecutive_errors += 1
            backoff_seconds: float = min(
                _MAX_BACKOFF_SECONDS, _POLL_SECONDS * (2 ** (self._consecutive_errors - 1))
            )
            self._backoff_until = monotonic() + backoff_seconds
            self._latest_error = f"{type(error).__name__}: {error}"
            self._health_state = SensorSchedulerState.BACKING_OFF


def _tick_retention_days(*, analysis: CompileAnalysis) -> int:
    loaded_project: LoadedProject | None = analysis.discovered_inputs.loaded_project
    if loaded_project is None or loaded_project.effective_configuration is None:
        return 0
    return loaded_project.effective_configuration.sensors.tick_retention_days


def _maximum_event_age_seconds(*, analysis: CompileAnalysis) -> int | None:
    loaded_project: LoadedProject | None = analysis.discovered_inputs.loaded_project
    if loaded_project is None or loaded_project.effective_configuration is None:
        return None
    return loaded_project.effective_configuration.sensors.maximum_event_age_seconds
