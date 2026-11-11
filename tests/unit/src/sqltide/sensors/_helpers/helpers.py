import time
from pathlib import Path
from textwrap import dedent

from sqltide._native import compile_sensors
from sqltide.events import AuditCompleted
from sqltide.sensors import DefaultSensorStatus, SkipReason, event_sensor
from sqltide.sensors.models import CompiledSensors, SensorRegistry

QUALITY_SENSOR_SOURCE: str = """
from sqltide.events import AuditCompleted
from sqltide.sensors import DefaultSensorStatus, event_sensor


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def quality_alerts(ctx):
    \"\"\"Alert on audit transitions.

    Further details live here.
    \"\"\"
"""

LAG_SENSOR_SOURCE: str = """
from sqltide.sensors import polling_sensor


@polling_sensor(minimum_interval_seconds=60)
def kafka_lag_watch(ctx):
    \"\"\"Watch consumer lag.\"\"\"
"""

RETRYING_SENSOR_SOURCE: str = """
from sqltide.events import RunCompleted
from sqltide.sensors import (
    DefaultSensorStatus,
    SensorRetryPolicy,
    StepPolicy,
    event_sensor,
)


@event_sensor(
    on=RunCompleted,
    targets={"prod"},
    default_status=DefaultSensorStatus.STOPPED,
    retry_policy=SensorRetryPolicy(max_attempts=3, backoff_seconds=30),
    timeout_seconds=12.5,
)
def announce_run(ctx):
    \"\"\"Announce completed runs.\"\"\"
    ctx.step("notify", lambda: ctx.event.invocation_id, policy=StepPolicy.AT_MOST_ONCE)
"""

PAIRED_SENSORS_SOURCE: str = """
from sqltide.events import AuditCompleted
from sqltide.sensors import SkipReason, event_sensor, polling_sensor


@event_sensor(on=AuditCompleted, maximum_event_age_seconds=60)
def stale_audits(ctx):
    \"\"\"Café orders: skip stale audit results.\"\"\"
    return SkipReason("stale")


@polling_sensor(minimum_interval_seconds=5)
def inventory_poll(ctx):
    return None
"""


def write_project_files(*, project_dir: Path, files: tuple[tuple[str, str], ...]) -> None:
    for relative_path, contents in files:
        path: Path = project_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(contents).strip() + "\n", encoding="utf-8")


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def quiet_handler(ctx: object) -> None:
    """Succeed without output."""


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def skipping_handler(ctx: object) -> SkipReason:
    return SkipReason("nothing to do")


@event_sensor(on=AuditCompleted, default_status=DefaultSensorStatus.RUNNING)
def raising_handler(ctx: object) -> None:
    raise ValueError("bad webhook")


@event_sensor(
    on=AuditCompleted,
    default_status=DefaultSensorStatus.RUNNING,
    timeout_seconds=0.05,
)
def hanging_handler(ctx: object) -> None:
    time.sleep(0.5)


def load_registry(*, project_dir: Path) -> SensorRegistry:
    compiled: CompiledSensors | None = compile_sensors(
        project_dir=project_dir, reserved_names=frozenset()
    )
    assert compiled is not None
    return compiled.registry
