from dataclasses import dataclass, field

from sqltide.events.models import InvocationObservation, NodeResultObservation
from sqltide.executor.auditing.types import QualityResultStatus
from sqltide.sensors.models import (
    LoadedSensor,
    PollingTickState,
    SensorDispatchSummary,
    SensorStreamPosition,
    TickAttemptState,
)
from sqltide.sensors.types import SensorOverrideStatus, SensorTickStatus


@dataclass(frozen=True)
class DispatcherScenarioTestCase:
    description: str
    sensor: LoadedSensor
    expected_tick_statuses: tuple[str, ...]
    expected_advanced_count: int
    expected_summary: SensorDispatchSummary
    checkpoints: dict[tuple[str, str], SensorStreamPosition] = field(default_factory=dict)
    node_results: tuple[NodeResultObservation, ...] = ()
    invocations: tuple[InvocationObservation, ...] = ()
    previous_statuses: dict[str, QualityResultStatus] = field(default_factory=dict)
    attempt_states: dict[tuple[str, str], TickAttemptState] = field(default_factory=dict)
    overrides: dict[str, SensorOverrideStatus] = field(default_factory=dict)
    polling_states: dict[str, PollingTickState] = field(default_factory=dict)
    newest_position: SensorStreamPosition | None = SensorStreamPosition(
        completed_at="2024-01-01 00:00:01.000", result_id="result-1"
    )
    lease_acquired: bool = True
    event_target: str = "prod"


@dataclass(frozen=True)
class EventCheckpointInitializationTestCase:
    description: str
    sensors: tuple[LoadedSensor, ...]
    checkpoints: dict[tuple[str, str], SensorStreamPosition]
    newest_position: SensorStreamPosition
    expected_advanced: tuple[tuple[str, str, SensorStreamPosition], ...]


@dataclass(frozen=True)
class MultiBatchDrainTestCase:
    description: str
    result_count: int
    batch_limit: int
    expected_result_id: str


@dataclass(frozen=True)
class StreamHeadBoundTestCase:
    description: str
    batch_limit: int
    stream_head: SensorStreamPosition
    expected_result_id: str


@dataclass(frozen=True)
class LeaseLossTestCase:
    description: str
    result_count: int
    batch_limit: int
    lease_results: tuple[bool, ...]
    expected_result_id: str


@dataclass(frozen=True)
class DeadLetterActionTestCase:
    description: str
    sensor: LoadedSensor
    node_results: tuple[NodeResultObservation, ...]
    attempt_states: dict[tuple[str, str], TickAttemptState]
    event_id: str
    expected_status: SensorTickStatus
    expected_tick_statuses: tuple[str, ...]
    expected_tick_attempts: tuple[int, ...]
    expected_skip_reason: str | None = None


@dataclass(frozen=True)
class StepRunnerTestCase:
    description: str
    expected_first_value: object
    expected_second_value: object
    expected_call_count: int


@dataclass(frozen=True)
class CompiledSensorDispatchTestCase:
    description: str
    sensor_files: tuple[tuple[str, str], ...]
    checkpoints: dict[tuple[str, str], SensorStreamPosition]
    expected_ticks: tuple[tuple[str, str], ...]
    expected_summary: SensorDispatchSummary
