from dataclasses import dataclass

from sqltide.sensors.models import LoadedSensor
from sqltide.sensors.types import SensorTickStatus


@dataclass(frozen=True)
class EvaluationTestCase:
    description: str
    sensor: LoadedSensor
    expected_status: SensorTickStatus
    expected_skip_reason: str | None = None
    expected_error_fragment: str | None = None


@dataclass(frozen=True)
class SensorRegistryLoadTestCase:
    description: str
    files: tuple[tuple[str, str], ...]
    expected_names: tuple[str, ...]
    expected_kinds: tuple[str, ...]
    expected_descriptions: tuple[str | None, ...]


@dataclass(frozen=True)
class SensorRegistryErrorTestCase:
    description: str
    files: tuple[tuple[str, str], ...]
    expected_error_fragment: str
    expected_location_line: int | None = None


@dataclass(frozen=True)
class SensorFingerprintTestCase:
    description: str
    files: tuple[tuple[str, str], ...]
    expected_fingerprints: tuple[tuple[str, str], ...]
