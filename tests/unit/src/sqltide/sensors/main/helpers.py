from pathlib import Path
from textwrap import dedent

from sqltide._native import compile_sensors
from sqltide.sensors.models import CompiledSensors

QUALITY_SENSOR_SOURCE: str = """
from sqltide.events import AuditCompleted
from sqltide.sensors import event_sensor


@event_sensor(on=AuditCompleted)
def quality_alerts(ctx):
    \"\"\"Alert on audit transitions.\"\"\"
"""

PROVIDER_SOURCE: str = """
from sqltide.providers import Provider


class OpsSlack(Provider):
    webhook_url: str = "https://hooks.example.invalid/ops"
"""

UNSUPPORTED_EVENT_SENSOR_SOURCE: str = """
from sqltide.sensors import event_sensor


class HomemadeEvent:
    pass


@event_sensor(on=HomemadeEvent)
def homemade(ctx):
    \"\"\"React to a non-catalog event.\"\"\"
"""

CATALOG_EVENT_SENSORS_SOURCE: str = """
from sqltide.events import AuditCompleted, RunCompleted
from sqltide.sensors import event_sensor


@event_sensor(on=AuditCompleted)
def audit_watch(ctx):
    \"\"\"React to audit completions.\"\"\"


@event_sensor(on=RunCompleted)
def run_watch(ctx):
    \"\"\"React to run completions.\"\"\"
"""

LOOKALIKE_EVENT_SENSOR_SOURCE: str = """
from sqltide.sensors import event_sensor


class AuditCompleted:
    pass


@event_sensor(on=AuditCompleted)
def lookalike(ctx):
    \"\"\"React to a class that only shares the catalog event name.\"\"\"
"""

DUPLICATE_PROVIDER_SOURCE: str = PROVIDER_SOURCE

UNSET_SETTINGS_PROVIDER_SOURCE: str = """
from sqltide.providers import Provider


class OrdersWebhook(Provider):
    orders_webhook_url: str
    orders_webhook_retry_limit: int = 3
    orders_webhook_timeout: float
"""

BROKEN_PROVIDER_SOURCE: str = """
VALUE = 1
raise RuntimeError("cannot import")
"""

INVALID_NAME_PROVIDER_SOURCE: str = """
from typing import ClassVar

from sqltide.providers import Provider


class BadName(Provider):
    provider_name: ClassVar[str | None] = "Not Valid"
"""

MIXED_PROVIDER_SOURCE: str = """
from abc import abstractmethod

from providers.zeta import OpsSlack
from sqltide.providers import Provider


class Helper:
    pass


class AbstractNotifier(Provider):
    @abstractmethod
    def send(self, message: str) -> None: ...


class ConsoleNotifier(AbstractNotifier):
    def send(self, message: str) -> None:
        del message


class AuditLog(Provider):
    pass
"""

SHARED_NAME_PROVIDER_SOURCE: str = """
from typing import ClassVar

from providers._shared import PROVIDER_NAME
from sqltide.providers import Provider


class OpsSlack(Provider):
    provider_name: ClassVar[str | None] = PROVIDER_NAME
"""


def write_project_files(*, project_dir: Path, files: tuple[tuple[str, str], ...]) -> None:
    for relative_path, contents in files:
        path: Path = project_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(contents).strip() + "\n", encoding="utf-8")


def compile_provider_names_in_turn(
    *, root: Path, project_provider_names: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    names: list[str] = []
    for project_name, provider_name in project_provider_names:
        project_dir: Path = root / project_name
        write_project_files(
            project_dir=project_dir,
            files=(
                ("providers/_shared.py", f'PROVIDER_NAME = "{provider_name}"'),
                ("providers/slack.py", SHARED_NAME_PROVIDER_SOURCE),
            ),
        )
        compiled: CompiledSensors | None = compile_sensors(
            project_dir=project_dir, reserved_names=frozenset()
        )
        assert compiled is not None
        names.extend(provider.name for provider in compiled.providers)
    return tuple(names)
