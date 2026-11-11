from sqltide.events import AuditCompleted
from sqltide.sensors import (
    DefaultSensorStatus,
    EventSensorContext,
    PollingSensorContext,
    PollingSensorResult,
    SensorRetryPolicy,
    SkipReason,
    event_sensor,
    polling_sensor,
)
from sqltide.sensors.models import SensorStreamPosition

EPOCH_POSITION: SensorStreamPosition = SensorStreamPosition(
    completed_at="1970-01-01 00:00:00.000", result_id=""
)
ROW_POSITION: SensorStreamPosition = SensorStreamPosition(
    completed_at="2024-01-01 00:00:01.000", result_id="result-1"
)


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def succeeding_sensor(ctx: object) -> None:
    """React to audit completions without side effects."""


@event_sensor(
    on=AuditCompleted,
    maximum_event_age_seconds=60,
    default_status=DefaultSensorStatus.RUNNING,
)
def fresh_events_sensor(ctx: object) -> None:
    """React only to events that are still operationally current."""


@event_sensor(
    on=AuditCompleted,
    default_status=DefaultSensorStatus.RUNNING,
    retry_policy=SensorRetryPolicy(max_attempts=2, backoff_seconds=0),
)
def failing_sensor(ctx: object) -> None:
    raise RuntimeError("boom")


@event_sensor(
    on=AuditCompleted,
    default_status=DefaultSensorStatus.RUNNING,
    retry_policy=SensorRetryPolicy(max_attempts=3, backoff_seconds=3600),
)
def backoff_sensor(ctx: object) -> None:
    raise RuntimeError("boom")


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def skipping_sensor(ctx: object) -> SkipReason:
    return SkipReason("not actionable")


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.STOPPED)
def stopped_sensor(ctx: object) -> None:
    """Declared stopped by default."""


@event_sensor(on=AuditCompleted, targets={"prod"}, default_status=DefaultSensorStatus.RUNNING)
def prod_only_sensor(ctx: EventSensorContext[AuditCompleted]) -> None:
    assert ctx.event.target == "prod"
    assert ctx.target == "prod"


@polling_sensor(minimum_interval_seconds=60, default_status=DefaultSensorStatus.RUNNING)
def polling_lag(ctx: PollingSensorContext) -> PollingSensorResult:
    assert ctx.target == "prod"
    return PollingSensorResult(cursor="42")


class CountingStep:
    def __init__(self, *, value: object = "ticket-42") -> None:
        self.calls: int = 0
        self._value: object = value

    def __call__(self) -> object:
        self.calls += 1
        return self._value


class FailingStep:
    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self) -> object:
        self.calls += 1
        raise RuntimeError("slack unavailable")


CATALOG_EVENT_HANDLERS_SOURCE: str = """
from sqltide.events import AuditCompleted, RunCompleted
from sqltide.sensors import DefaultSensorStatus, event_sensor


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def audit_watch(ctx):
    assert type(ctx.event) is AuditCompleted


@event_sensor(on=RunCompleted, default_status=DefaultSensorStatus.RUNNING)
def run_watch(ctx):
    assert type(ctx.event) is RunCompleted
"""
