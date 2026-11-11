"""Closed sensor vocabulary, shared aliases, and storage protocols."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from sqltide._native import DefaultSensorStatus as DefaultSensorStatus
from sqltide._native import SensorKind as SensorKind
from sqltide._native import SensorOverrideStatus as SensorOverrideStatus
from sqltide._native import SensorTickStatus as SensorTickStatus
from sqltide._native import StepPolicy as StepPolicy

if TYPE_CHECKING:
    from sqltide.sensors.models import (
        EventSensorDeclaration,
        PollingSensorDeclaration,
        StepMarker,
    )

type SensorFunction = Callable[..., object]
type SensorDeclaration = EventSensorDeclaration | PollingSensorDeclaration


class SensorStepStore(Protocol):
    """Persisted step marker storage keyed by (sensor, event, step)."""

    def read_step(self, *, step_key: str) -> StepMarker | None:
        """Return the reduced marker state for one step, if any."""
        ...

    def record_step(
        self,
        *,
        step_key: str,
        policy: StepPolicy,
        status: SensorTickStatus,
        attempt: int,
        result_json: str | None,
        error_message: str | None,
    ) -> None:
        """Append one step marker row."""
        ...
