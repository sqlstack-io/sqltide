"""Authored sensor API: durable event and polling automation."""

from sqltide._native import event_sensor, polling_sensor
from sqltide.sensors.classes.event_sensor_context import EventSensorContext
from sqltide.sensors.classes.polling_sensor_context import PollingSensorContext
from sqltide.sensors.models import PollingSensorResult, SensorRetryPolicy, SkipReason
from sqltide.sensors.types import DefaultSensorStatus, StepPolicy

__all__ = [
    "DefaultSensorStatus",
    "EventSensorContext",
    "PollingSensorContext",
    "PollingSensorResult",
    "SensorRetryPolicy",
    "SkipReason",
    "StepPolicy",
    "event_sensor",
    "polling_sensor",
]
