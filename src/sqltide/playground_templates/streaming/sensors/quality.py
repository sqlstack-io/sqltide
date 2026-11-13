"""React to persisted audit results; events are derived, never emitted.

Sensors run at least once per event; handlers should tolerate re-execution.
`ctx.event.id` is the idempotency key; use `ctx.step()` to make re-runs resume
instead of redo.
"""

import json
from urllib.parse import quote

from providers.console import ConsoleNotifier

from sqltide.events import AuditCompleted, AuditTransition
from sqltide.sensors import (
    DefaultSensorStatus,
    EventSensorContext,
    SensorRetryPolicy,
    event_sensor,
)


@event_sensor(
    on=AuditCompleted,
    default_status=DefaultSensorStatus.RUNNING,
    retry_policy=SensorRetryPolicy(max_attempts=3, backoff_seconds=5),
)
def quality_alerts(
    ctx: EventSensorContext[AuditCompleted], console_notifier: ConsoleNotifier
) -> None:
    """Print a local notification when an audit newly fails or recovers."""

    event = ctx.event
    if event.transition not in {AuditTransition.NEW_FAILURE, AuditTransition.RECOVERED}:
        return

    def compose_message() -> str:
        link = f"http://127.0.0.1:8000/quality?audit={quote(event.audit_name)}"
        if event.transition == AuditTransition.RECOVERED:
            return f"RECOVERED: {event.audit_name} on {event.target}. Inspect: {link}"
        level = (event.severity or "error").upper()
        sample = ""
        if event.sample_rows:
            values = dict(zip(event.sample_column_names, event.sample_rows[0], strict=True))
            sample = f" Sample: {json.dumps(values, default=str, separators=(',', ':'))}"
        return (
            f"{level}: {event.audit_name} on {event.target} has {event.failure_count} "
            f"failing row(s). Inspect: {link}.{sample}"
        )

    message = ctx.step("compose", compose_message)
    ctx.step("console", lambda: console_notifier.notify(str(message)))
