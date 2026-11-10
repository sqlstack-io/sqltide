from bisect import bisect_right
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from sqltide._native import SensorStateRepository
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterSensorStepRecord, AdapterSensorTickRecord
from sqltide.events.models import InvocationObservation, NodeResultObservation
from sqltide.executor.auditing.types import QualityResultStatus
from sqltide.sensors.models import (
    EventSensorDeclaration,
    LoadedSensor,
    PollingTickState,
    SensorStreamPosition,
    StepMarker,
    TickAttemptState,
)
from sqltide.sensors.types import SensorDeclaration, SensorKind, SensorOverrideStatus

FAKE_WAREHOUSE_NOW: str = "2024-01-01 00:10:00.000"
_FRESH_ATTEMPT_STATE: TickAttemptState = TickAttemptState(
    failed_attempts=0, last_failed_at=None, last_error_message=None, resolved=False
)
_IDLE_POLLING_STATE: PollingTickState = PollingTickState(
    last_started_at=None, last_success_at=None, cursor=None
)


def build_loaded_sensor(*, declaration: SensorDeclaration, name: str | None = None) -> LoadedSensor:
    kinds: dict[bool, SensorKind] = {True: SensorKind.EVENT, False: SensorKind.POLLING}
    return LoadedSensor(
        name=name or declaration.name,
        kind=kinds[isinstance(declaration, EventSensorDeclaration)],
        declaration=declaration,
        file_path=Path("/project/sensors/quality.py"),
        relative_path=Path("sensors/quality.py"),
        source="def handler(ctx): ...",
        definition_line=1,
        timeout_seconds=declaration.timeout_seconds,
    )


class FakeSensorStateRepository(SensorStateRepository):
    """Scripted in-memory repository recording every write."""

    def __new__(cls, **_: object) -> "FakeSensorStateRepository":
        return super().__new__(cls, connection=cast("AdapterConnection", None), database="")

    def __init__(
        self,
        *,
        checkpoints: dict[tuple[str, str], SensorStreamPosition] | None = None,
        node_results: tuple[NodeResultObservation, ...] = (),
        invocations: tuple[InvocationObservation, ...] = (),
        previous_statuses: dict[str, QualityResultStatus] | None = None,
        attempt_states: dict[tuple[str, str], TickAttemptState] | None = None,
        overrides: dict[str, SensorOverrideStatus] | None = None,
        polling_states: dict[str, PollingTickState] | None = None,
        newest_position: SensorStreamPosition | None = None,
        lease_acquired: bool = True,
        lease_results: tuple[bool, ...] = (),
        step_markers: dict[tuple[str, str, str], StepMarker] | None = None,
    ) -> None:
        self._checkpoints: dict[tuple[str, str], SensorStreamPosition] = dict(checkpoints or {})
        self._node_results: tuple[NodeResultObservation, ...] = node_results
        self._invocations: tuple[InvocationObservation, ...] = invocations
        self._previous_statuses: dict[str, QualityResultStatus] = dict(previous_statuses or {})
        self._attempt_states: dict[tuple[str, str], TickAttemptState] = dict(attempt_states or {})
        self._overrides: dict[str, SensorOverrideStatus] = dict(overrides or {})
        self._polling_states: dict[str, PollingTickState] = dict(polling_states or {})
        self._newest_position: SensorStreamPosition | None = newest_position
        self._lease_acquired: bool = lease_acquired
        self._lease_results: Iterator[bool] = iter((*lease_results, lease_acquired))
        self._step_markers: dict[tuple[str, str, str], StepMarker] = dict(step_markers or {})
        self._node_results_by_id: dict[str, NodeResultObservation] = {
            row.result_id: row for row in node_results
        }
        self._invocations_by_id: dict[str, InvocationObservation] = {
            row.invocation_id: row for row in invocations
        }
        self.ensure_ready_calls: int = 0
        self.advanced: list[tuple[str, str, SensorStreamPosition]] = []
        self.recorded_ticks: list[AdapterSensorTickRecord] = []
        self.recorded_steps: list[AdapterSensorStepRecord] = []
        self.recorded_overrides: list[tuple[str, SensorOverrideStatus, str | None]] = []

    def ensure_ready(self) -> None:
        self.ensure_ready_calls += 1

    def warehouse_now(self) -> str:
        return FAKE_WAREHOUSE_NOW

    def acquire_dispatch_lease(self, *, owner_id: str, ttl_seconds: float) -> bool:
        return next(self._lease_results, self._lease_acquired)

    def override_statuses(self) -> dict[str, SensorOverrideStatus]:
        return dict(self._overrides)

    def record_override(
        self, *, sensor_name: str, status: SensorOverrideStatus, actor: str | None
    ) -> None:
        self.recorded_overrides.append((sensor_name, status, actor))
        self._overrides[sensor_name] = status

    def read_checkpoint(self, *, sensor_name: str, source: str) -> SensorStreamPosition | None:
        return self._checkpoints.get((sensor_name, source))

    def advance_checkpoint(
        self, *, sensor_name: str, source: str, position: SensorStreamPosition
    ) -> None:
        self.advanced.append((sensor_name, source, position))
        self._checkpoints[(sensor_name, source)] = position

    def newest_node_result_position(self, *, target: str) -> SensorStreamPosition | None:
        return self._newest_position

    def newest_invocation_position(self, *, target: str) -> SensorStreamPosition | None:
        return self._newest_position

    def fetch_node_results_after(
        self, *, position: SensorStreamPosition, target: str, limit: int
    ) -> tuple[NodeResultObservation, ...]:
        keys: tuple[tuple[str, str], ...] = tuple(
            (row.completed_at, row.result_id) for row in self._node_results
        )
        start: int = bisect_right(keys, (position.completed_at, position.result_id))
        return self._node_results[start : start + limit]

    def fetch_node_result(self, *, result_id: str) -> NodeResultObservation | None:
        return self._node_results_by_id.get(result_id)

    def fetch_invocations_after(
        self, *, position: SensorStreamPosition, target: str, limit: int
    ) -> tuple[InvocationObservation, ...]:
        keys: tuple[tuple[str, str], ...] = tuple(
            (row.completed_at, row.invocation_id) for row in self._invocations
        )
        start: int = bisect_right(keys, (position.completed_at, position.result_id))
        return self._invocations[start : start + limit]

    def fetch_invocation(self, *, invocation_id: str) -> InvocationObservation | None:
        return self._invocations_by_id.get(invocation_id)

    def previous_node_status(
        self, *, binding_key: str, target: str, position: SensorStreamPosition
    ) -> QualityResultStatus | None:
        return self._previous_statuses.get(binding_key)

    def tick_attempt_state(self, *, sensor_name: str, event_id: str) -> TickAttemptState:
        return self._attempt_states.get((sensor_name, event_id), _FRESH_ATTEMPT_STATE)

    def record_ticks(self, *, ticks: tuple[AdapterSensorTickRecord, ...]) -> None:
        self.recorded_ticks.extend(ticks)

    def polling_tick_state(self, *, sensor_name: str) -> PollingTickState:
        return self._polling_states.get(sensor_name, _IDLE_POLLING_STATE)

    def read_step(self, *, sensor_name: str, event_id: str, step_key: str) -> StepMarker | None:
        return self._step_markers.get((sensor_name, event_id, step_key))

    def record_step(self, *, step: AdapterSensorStepRecord) -> None:
        self.recorded_steps.append(step)
        self._step_markers[(step.sensor_name, step.event_id, step.step_key)] = StepMarker(
            status=step.status, result_json=step.result_json, attempt=step.attempt
        )
