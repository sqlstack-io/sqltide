"""Behavior tests for compiling project sensors and providers."""

from pathlib import Path

import pytest

from sqltide._native import compile_sensors
from sqltide.sensors.exceptions import SensorError
from sqltide.sensors.models import CompiledSensors
from tests.unit.src.sqltide.sensors.main._test_types import (
    CompileSensorsErrorTestCase,
    CompileSensorsTestCase,
)
from tests.unit.src.sqltide.sensors.main.helpers import (
    PROVIDER_SOURCE,
    QUALITY_SENSOR_SOURCE,
    UNSUPPORTED_EVENT_SENSOR_SOURCE,
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
