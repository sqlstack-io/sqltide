from dataclasses import dataclass


@dataclass(frozen=True)
class CompileSensorsTestCase:
    description: str
    sensor_files: tuple[tuple[str, str], ...]
    provider_files: tuple[tuple[str, str], ...] = ()
    reserved_names: frozenset[str] = frozenset()
    expected_sensor_names: tuple[str, ...] = ()
    expected_provider_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompileSensorsErrorTestCase:
    description: str
    sensor_files: tuple[tuple[str, str], ...]
    reserved_names: frozenset[str]
    expected_error_fragment: str


@dataclass(frozen=True)
class CompileSensorsEventTypeTestCase:
    description: str
    sensor_files: tuple[tuple[str, str], ...]
    expected_event_types: tuple[tuple[str, type], ...]


@dataclass(frozen=True)
class CompileSensorsProviderErrorTestCase:
    description: str
    provider_files: tuple[tuple[str, str], ...]
    expected_message: str
    expected_cause_type: type


@dataclass(frozen=True)
class CompileSensorsProviderPathTestCase:
    description: str
    provider_files: tuple[tuple[str, str], ...]
    expected_error_type: type[BaseException]
    expected_context_type_name: str


@dataclass(frozen=True)
class CompileSensorsProviderReloadTestCase:
    description: str
    project_provider_names: tuple[tuple[str, str], ...]
    expected_provider_names: tuple[str, ...]
