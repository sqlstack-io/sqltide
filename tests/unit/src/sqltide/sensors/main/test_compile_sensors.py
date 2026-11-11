"""Behavior tests for compiling project sensors and providers."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sqltide._native import compile_sensors
from sqltide.events import AuditCompleted, RunCompleted
from sqltide.provider.exceptions import ProviderDiscoveryError, ProviderInputError
from sqltide.sensors.exceptions import SensorError
from sqltide.sensors.models import CompiledSensors
from tests.unit.src.sqltide.sensors.main._test_types import (
    CompileSensorsErrorTestCase,
    CompileSensorsEventTypeTestCase,
    CompileSensorsProviderErrorTestCase,
    CompileSensorsProviderPathTestCase,
    CompileSensorsProviderReloadTestCase,
    CompileSensorsTestCase,
)
from tests.unit.src.sqltide.sensors.main.helpers import (
    BROKEN_PROVIDER_SOURCE,
    CATALOG_EVENT_SENSORS_SOURCE,
    DUPLICATE_PROVIDER_SOURCE,
    INVALID_NAME_PROVIDER_SOURCE,
    LOOKALIKE_EVENT_SENSOR_SOURCE,
    MIXED_PROVIDER_SOURCE,
    PROVIDER_SOURCE,
    QUALITY_SENSOR_SOURCE,
    UNPRINTABLE_ERROR_PROVIDER_SOURCE,
    UNSET_SETTINGS_PROVIDER_SOURCE,
    UNSUPPORTED_EVENT_SENSOR_SOURCE,
    compile_provider_names_in_turn,
    write_project_files,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsTestCase(
            description="projects without sensors or providers compile to no artifact",
            sensor_files=(),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_automation_dirs_when_compiling_then_artifact_is_absent(
    test_case: CompileSensorsTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.sensor_files)

    compiled: CompiledSensors | None = compile_sensors(
        project_dir=tmp_path, reserved_names=test_case.reserved_names
    )

    assert compiled is None
    assert test_case.expected_sensor_names == ()


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsTestCase(
            description="sensors and providers compile into one artifact",
            sensor_files=(("sensors/quality.py", QUALITY_SENSOR_SOURCE),),
            provider_files=(("providers/slack.py", PROVIDER_SOURCE),),
            expected_sensor_names=("quality_alerts",),
            expected_provider_names=("ops_slack",),
        ),
        CompileSensorsTestCase(
            description="providers alone still compile into an artifact",
            sensor_files=(),
            provider_files=(("providers/slack.py", PROVIDER_SOURCE),),
            expected_sensor_names=(),
            expected_provider_names=("ops_slack",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_authored_automation_when_compiling_then_artifact_is_complete(
    test_case: CompileSensorsTestCase, tmp_path: Path
) -> None:
    write_project_files(
        project_dir=tmp_path,
        files=test_case.sensor_files + test_case.provider_files,
    )

    compiled: CompiledSensors | None = compile_sensors(
        project_dir=tmp_path, reserved_names=test_case.reserved_names
    )

    assert compiled is not None
    assert (
        tuple(sensor.name for sensor in compiled.registry.ordered())
        == test_case.expected_sensor_names
    )
    assert (
        tuple(provider.name for provider in compiled.providers) == test_case.expected_provider_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsErrorTestCase(
            description="sensor names must not collide with project resources",
            sensor_files=(("sensors/quality.py", QUALITY_SENSOR_SOURCE),),
            reserved_names=frozenset({"quality_alerts"}),
            expected_error_fragment="collides with another project resource",
        ),
        CompileSensorsErrorTestCase(
            description="event sensors must react to catalog events",
            sensor_files=(("sensors/homemade.py", UNSUPPORTED_EVENT_SENSOR_SOURCE),),
            reserved_names=frozenset(),
            expected_error_fragment="Unsupported sensor event type 'HomemadeEvent'",
        ),
        CompileSensorsErrorTestCase(
            description="a class that only shares a catalog event name is not that event",
            sensor_files=(("sensors/lookalike.py", LOOKALIKE_EVENT_SENSOR_SOURCE),),
            reserved_names=frozenset(),
            expected_error_fragment="Unsupported sensor event type 'AuditCompleted'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sensors_when_compiling_then_diagnostics_surface(
    test_case: CompileSensorsErrorTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.sensor_files)

    with pytest.raises(SensorError, match=test_case.expected_error_fragment) as error_info:
        _ = compile_sensors(project_dir=tmp_path, reserved_names=test_case.reserved_names)

    assert error_info.value.location is not None


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsEventTypeTestCase(
            description="sensors on imported catalog events keep the catalog classes",
            sensor_files=(("sensors/catalog.py", CATALOG_EVENT_SENSORS_SOURCE),),
            expected_event_types=(("audit_watch", AuditCompleted), ("run_watch", RunCompleted)),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_catalog_event_sensors_when_compiling_then_event_types_are_the_catalog_classes(
    test_case: CompileSensorsEventTypeTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.sensor_files)

    compiled: CompiledSensors | None = compile_sensors(
        project_dir=tmp_path, reserved_names=frozenset()
    )

    assert compiled is not None
    observed: tuple[tuple[str, object], ...] = tuple(
        (sensor.name, getattr(sensor.declaration, "event_type", None))
        for sensor in compiled.registry.ordered()
    )
    assert len(observed) == len(test_case.expected_event_types)
    assert all(
        name == expected_name and event_type is expected_type
        for (name, event_type), (expected_name, expected_type) in zip(
            observed, test_case.expected_event_types, strict=True
        )
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsTestCase(
            description="providers are discovered by file then class name, skipping the rest",
            sensor_files=(),
            provider_files=(
                ("providers/zeta.py", PROVIDER_SOURCE),
                ("providers/alpha.py", MIXED_PROVIDER_SOURCE),
                ("providers/_private.py", PROVIDER_SOURCE),
                ("providers/__init__.py", ""),
            ),
            expected_provider_names=("audit_log", "console_notifier", "ops_slack"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_provider_modules_when_compiling_then_only_concrete_providers_are_found(
    test_case: CompileSensorsTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.provider_files)

    compiled: CompiledSensors | None = compile_sensors(
        project_dir=tmp_path, reserved_names=test_case.reserved_names
    )

    assert compiled is not None
    assert (
        tuple(provider.name for provider in compiled.providers) == test_case.expected_provider_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsProviderErrorTestCase(
            description="provider names must be unique across provider files",
            provider_files=(
                ("providers/alerts.py", DUPLICATE_PROVIDER_SOURCE),
                ("providers/slack.py", DUPLICATE_PROVIDER_SOURCE),
            ),
            expected_message=(
                "Duplicate provider name 'ops_slack' found in providers/alerts.py and "
                "providers/slack.py"
            ),
            expected_cause_type=type(None),
        ),
        CompileSensorsProviderErrorTestCase(
            description="unset provider settings list every failing field",
            provider_files=(("providers/orders.py", UNSET_SETTINGS_PROVIDER_SOURCE),),
            expected_message=(
                "Provider 'orders_webhook' in providers/orders.py has invalid settings:\n"
                "orders_webhook_url: Field required [missing]\n"
                "orders_webhook_timeout: Field required [missing]"
            ),
            expected_cause_type=ValidationError,
        ),
        CompileSensorsProviderErrorTestCase(
            description="provider import failures name the provider file",
            provider_files=(("providers/broken.py", BROKEN_PROVIDER_SOURCE),),
            expected_message="Failed to import provider file providers/broken.py: cannot import",
            expected_cause_type=RuntimeError,
        ),
        CompileSensorsProviderErrorTestCase(
            description="invalid provider names name the provider class and file",
            provider_files=(("providers/bad.py", INVALID_NAME_PROVIDER_SOURCE),),
            expected_message=(
                "Provider class BadName in providers/bad.py has an invalid provider name: "
                "provider BadName has invalid provider name 'Not Valid'; provider names must "
                "be lower snake_case Python identifiers"
            ),
            expected_cause_type=ProviderInputError,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_providers_when_compiling_then_discovery_error_explains_the_failure(
    test_case: CompileSensorsProviderErrorTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.provider_files)

    with pytest.raises(ProviderDiscoveryError) as error_info:
        _ = compile_sensors(project_dir=tmp_path, reserved_names=frozenset())

    assert error_info.value.args == (test_case.expected_message,)
    assert type(error_info.value.__cause__) is test_case.expected_cause_type


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsProviderPathTestCase(
            description="an import error whose message cannot be rendered still restores sys.path",
            provider_files=(("providers/unprintable.py", UNPRINTABLE_ERROR_PROVIDER_SOURCE),),
            expected_error_type=ZeroDivisionError,
            expected_context_type_name="UnprintableError",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unprintable_provider_import_error_when_compiling_then_sys_path_is_restored(
    test_case: CompileSensorsProviderPathTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.provider_files)
    path_before: list[str] = list(sys.path)

    with pytest.raises(test_case.expected_error_type) as error_info:
        _ = compile_sensors(project_dir=tmp_path, reserved_names=frozenset())

    assert sys.path == path_before
    assert type(error_info.value.__context__).__name__ == test_case.expected_context_type_name


@pytest.mark.parametrize(
    "test_case",
    [
        CompileSensorsProviderReloadTestCase(
            description="a second project reloads its own provider package modules",
            project_provider_names=(
                ("first", "alpha_slack"),
                ("second", "beta_slack"),
                ("first", "alpha_slack"),
            ),
            expected_provider_names=("alpha_slack", "beta_slack", "alpha_slack"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_projects_when_compiling_in_turn_then_stale_provider_modules_are_evicted(
    test_case: CompileSensorsProviderReloadTestCase, tmp_path: Path
) -> None:
    names: tuple[str, ...] = compile_provider_names_in_turn(
        root=tmp_path, project_provider_names=test_case.project_provider_names
    )

    assert names == test_case.expected_provider_names
