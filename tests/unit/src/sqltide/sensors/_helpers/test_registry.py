"""Behavior tests for loading authored sensor modules from sensors/."""

from pathlib import Path

import pytest

from sqltide.sensors.exceptions import SensorError
from sqltide.sensors.models import SensorRegistry
from tests.unit.src.sqltide.sensors._helpers._test_types import (
    SensorRegistryErrorTestCase,
    SensorRegistryLoadTestCase,
)
from tests.unit.src.sqltide.sensors._helpers.helpers import (
    LAG_SENSOR_SOURCE,
    QUALITY_SENSOR_SOURCE,
    load_registry,
    write_project_files,
)


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRegistryLoadTestCase(
            description="public sensor modules register declarations deterministically",
            files=(
                ("sensors/quality.py", QUALITY_SENSOR_SOURCE),
                ("sensors/lag.py", LAG_SENSOR_SOURCE),
            ),
            expected_names=("kafka_lag_watch", "quality_alerts"),
            expected_kinds=("polling", "event"),
            expected_descriptions=("Watch consumer lag.", "Alert on audit transitions."),
        ),
        SensorRegistryLoadTestCase(
            description="private and initializer files are skipped",
            files=(
                ("sensors/quality.py", QUALITY_SENSOR_SOURCE),
                ("sensors/__init__.py", ""),
                ("sensors/_shared.py", LAG_SENSOR_SOURCE),
                ("sensors/_private/inner.py", LAG_SENSOR_SOURCE),
            ),
            expected_names=("quality_alerts",),
            expected_kinds=("event",),
            expected_descriptions=("Alert on audit transitions.",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sensor_modules_when_loading_then_registry_contains_declarations(
    test_case: SensorRegistryLoadTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.files)

    registry: SensorRegistry = load_registry(project_dir=tmp_path)

    assert tuple(sensor.name for sensor in registry.ordered()) == test_case.expected_names
    assert tuple(str(sensor.kind) for sensor in registry.ordered()) == test_case.expected_kinds
    assert (
        tuple(sensor.description for sensor in registry.ordered())
        == test_case.expected_descriptions
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRegistryLoadTestCase(
            description="fingerprints are stable across repeated loads",
            files=(("sensors/quality.py", QUALITY_SENSOR_SOURCE),),
            expected_names=("quality_alerts",),
            expected_kinds=("event",),
            expected_descriptions=("Alert on audit transitions.",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unchanged_sources_when_reloading_then_fingerprints_are_stable(
    test_case: SensorRegistryLoadTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.files)

    first: SensorRegistry = load_registry(project_dir=tmp_path)
    second: SensorRegistry = load_registry(project_dir=tmp_path)

    assert tuple(sensor.name for sensor in first.ordered()) == test_case.expected_names
    assert tuple(sensor.identity_fingerprint for sensor in first.ordered()) == tuple(
        sensor.identity_fingerprint for sensor in second.ordered()
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SensorRegistryErrorTestCase(
            description="duplicate sensor names collide with both files reported",
            files=(
                ("sensors/one.py", QUALITY_SENSOR_SOURCE),
                ("sensors/two.py", QUALITY_SENSOR_SOURCE),
            ),
            expected_error_fragment="Sensor name collision for 'quality_alerts'",
            expected_location_line=5,
        ),
        SensorRegistryErrorTestCase(
            description="module import failures locate the raising line",
            files=(
                (
                    "sensors/broken.py",
                    """
                    VALUE = 1
                    raise RuntimeError("cannot import")
                    """,
                ),
            ),
            expected_error_fragment="cannot import",
            expected_location_line=2,
        ),
        SensorRegistryErrorTestCase(
            description="syntax errors locate the offending line",
            files=(("sensors/syntax.py", "def broken(:\n    pass"),),
            expected_error_fragment="Failed to load",
            expected_location_line=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sensor_modules_when_loading_then_diagnostics_locate_the_source(
    test_case: SensorRegistryErrorTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.files)

    with pytest.raises(SensorError, match=test_case.expected_error_fragment) as error_info:
        _ = load_registry(project_dir=tmp_path)

    assert error_info.value.location is not None
    assert error_info.value.location.line == test_case.expected_location_line
    assert error_info.value.diagnostic.location is error_info.value.location
