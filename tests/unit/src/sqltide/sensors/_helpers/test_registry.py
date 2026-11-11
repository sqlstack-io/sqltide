"""Behavior tests for loading authored sensor modules from sensors/."""

from pathlib import Path

import pytest

from sqltide.sensors.exceptions import SensorError
from sqltide.sensors.models import SensorRegistry
from tests.unit.src.sqltide.sensors._helpers._test_types import (
    SensorFingerprintTestCase,
    SensorRegistryErrorTestCase,
    SensorRegistryLoadTestCase,
)
from tests.unit.src.sqltide.sensors._helpers.helpers import (
    LAG_SENSOR_SOURCE,
    PAIRED_SENSORS_SOURCE,
    QUALITY_SENSOR_SOURCE,
    RETRYING_SENSOR_SOURCE,
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
        SensorFingerprintTestCase(
            description="an event sensor hashes its name, path and module source",
            files=(("sensors/quality.py", QUALITY_SENSOR_SOURCE),),
            expected_fingerprints=(
                (
                    "quality_alerts",
                    "b0d8ddb99aa4f7c889765b017e571e215902f120a009d2fbc97e1ae6396bb4fb",
                ),
            ),
        ),
        SensorFingerprintTestCase(
            description="a polling sensor hashes its name, path and module source",
            files=(("sensors/lag.py", LAG_SENSOR_SOURCE),),
            expected_fingerprints=(
                (
                    "kafka_lag_watch",
                    "4618e4883fc4b13596ee900059f4b3b0a5b1ed8e2b2902fdb37c49b6d6453fa9",
                ),
            ),
        ),
        SensorFingerprintTestCase(
            description="a nested sensor with retries, steps and a stopped status",
            files=(("sensors/alerts/retrying.py", RETRYING_SENSOR_SOURCE),),
            expected_fingerprints=(
                (
                    "announce_run",
                    "f16b2d3541a1050043ccf0021655fe68e8f410e51d809710339787c618eb20c1",
                ),
            ),
        ),
        SensorFingerprintTestCase(
            description="sensors sharing a non-ASCII module differ by name",
            files=(("sensors/paired.py", PAIRED_SENSORS_SOURCE),),
            expected_fingerprints=(
                (
                    "inventory_poll",
                    "e49e2925abfe4569ae920de86136b12e9678a2ba2e8456d217d65bfd28c2c8c8",
                ),
                (
                    "stale_audits",
                    "0fadd05926fc89caf1abf02f8c30657ffcc1d954298279d8bc9cb943201bbadb",
                ),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_authored_sensors_when_loading_then_persisted_fingerprints_are_unchanged(
    test_case: SensorFingerprintTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.files)

    registry: SensorRegistry = load_registry(project_dir=tmp_path)

    assert (
        tuple((sensor.name, sensor.identity_fingerprint) for sensor in registry.ordered())
        == test_case.expected_fingerprints
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
