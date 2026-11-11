"""Authored sensor declarations and policies, plus the native sensor runtime shapes."""

from __future__ import annotations

from dataclasses import dataclass

import sqltide._native as _native
from sqltide._native import CompiledSensors as CompiledSensors
from sqltide._native import LoadedSensor as LoadedSensor
from sqltide._native import PollingTickState as PollingTickState
from sqltide._native import SensorDeliveryOutcome as SensorDeliveryOutcome
from sqltide._native import SensorDispatchSummary as SensorDispatchSummary
from sqltide._native import SensorEvaluation as SensorEvaluation
from sqltide._native import SensorEventDelivery as SensorEventDelivery
from sqltide._native import SensorRegistry as SensorRegistry
from sqltide._native import SensorStreamPosition as SensorStreamPosition
from sqltide._native import SensorTickView as SensorTickView
from sqltide._native import StepMarker as StepMarker
from sqltide._native import TickAttemptState as TickAttemptState
from sqltide.sensors.types import DefaultSensorStatus, SensorFunction


@dataclass(frozen=True)
class SensorRetryPolicy:
    """Bounded re-attempt policy for failed event sensor ticks."""

    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        _native.validate_sensor_retry_policy(
            max_attempts=self.max_attempts, backoff_seconds=self.backoff_seconds
        )


@dataclass(frozen=True)
class SkipReason:
    """Explicit handler decision to skip one event with a recorded reason."""

    reason: str


@dataclass(frozen=True)
class PollingSensorResult:
    """Terminal result of one polling sensor tick."""

    cursor: str | None = None


@dataclass(frozen=True)
class EventSensorDeclaration:
    """One authored event sensor produced by the event_sensor decorator."""

    function: SensorFunction
    event_type: type
    name: str
    targets: frozenset[str] | None
    triggers: frozenset[str] | None
    maximum_event_age_seconds: float | None
    default_status: DefaultSensorStatus
    retry_policy: SensorRetryPolicy
    timeout_seconds: float

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self.function(*args, **kwargs)


@dataclass(frozen=True)
class PollingSensorDeclaration:
    """One authored polling sensor produced by the polling_sensor decorator."""

    function: SensorFunction
    name: str
    minimum_interval_seconds: float
    default_status: DefaultSensorStatus
    retry_policy: SensorRetryPolicy
    timeout_seconds: float

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self.function(*args, **kwargs)
