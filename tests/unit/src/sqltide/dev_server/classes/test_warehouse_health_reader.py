from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock

import pytest

from sqltide._native import WarehouseHealthReader
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterIdentity, AdapterWarehouseDisk, AdapterWarehouseHealth
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    WarehouseHealthCacheTestCase,
    WarehouseHealthRetentionTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthRetentionTestCase(
            description="failed refresh retains prior evidence and marks it stale",
            expected_availability="partial",
            expected_status="healthy",
            expected_stale=True,
            expected_warning="Warehouse diagnostics are unavailable.",
            first_measured_at="2026-08-23 10:00:00.000",
            second_measured_at="2026-08-23 10:00:15.000",
            second_database="analytics",
            expected_disk_count=1,
            expected_measured_at="2026-08-23 10:00:00.000",
        ),
        WarehouseHealthRetentionTestCase(
            description="failed refresh does not reuse evidence from another target",
            expected_availability="unavailable",
            expected_status="unknown",
            expected_stale=False,
            expected_warning="Warehouse diagnostics are unavailable.",
            first_measured_at="2026-08-23 10:00:00.000",
            second_measured_at="2026-08-23 10:00:15.000",
            second_database="other_analytics",
            expected_disk_count=0,
            expected_measured_at="2026-08-23 10:00:15.000",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_usable_health_then_failed_refresh_when_reading_then_evidence_stays_target_scoped(
    test_case: WarehouseHealthRetentionTestCase,
) -> None:
    usable: AdapterWarehouseHealth = AdapterWarehouseHealth(
        availability="available",
        status="healthy",
        version="25.8.1.1",
        uptime_seconds=100,
        disks=(
            AdapterWarehouseDisk(
                name="default",
                path="/data/",
                disk_type="Local",
                total_bytes=100,
                free_bytes=50,
                unreserved_bytes=50,
                keep_free_bytes=0,
                status="healthy",
            ),
        ),
        inode_total=100,
        inode_free=50,
        inode_status="healthy",
        memory=None,
        activity=None,
        tables=(),
        collection_duration_ms=2,
    )
    connection: MagicMock = MagicMock(spec=AdapterConnection)
    connection.adapter_identity = AdapterIdentity(name="clickhouse")
    connection.load_warehouse_health.side_effect = [
        usable,
        RuntimeError("provider detail must not escape"),
    ]
    clock_values: Iterator[float] = iter((0.0, 16.0))
    reader: WarehouseHealthReader = WarehouseHealthReader(clock=lambda: next(clock_values))

    first: AdapterWarehouseHealth = reader.read(
        connection=cast(AdapterConnection, connection),
        database="analytics",
        measured_at=test_case.first_measured_at,
    )
    second: AdapterWarehouseHealth = reader.read(
        connection=cast(AdapterConnection, connection),
        database=test_case.second_database,
        measured_at=test_case.second_measured_at,
    )

    assert first.measured_at == test_case.first_measured_at
    assert str(second.availability) == test_case.expected_availability
    assert str(second.status) == test_case.expected_status
    assert second.stale is test_case.expected_stale
    assert len(second.disks) == test_case.expected_disk_count
    assert second.warnings == (test_case.expected_warning,)
    assert second.measured_at == test_case.expected_measured_at


@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthCacheTestCase(
            description="health inside the snapshot interval reuses measured evidence",
            clock_values=(0.0, 10.0),
            expected_provider_reads=1,
            expected_second_measured_at="2026-08-23 10:00:00.000",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_recent_health_when_reading_again_then_provider_work_is_not_repeated(
    test_case: WarehouseHealthCacheTestCase,
) -> None:
    usable: AdapterWarehouseHealth = AdapterWarehouseHealth(
        availability="available",
        status="healthy",
        version="25.8.1.1",
        uptime_seconds=100,
        disks=(),
        inode_total=100,
        inode_free=50,
        inode_status="healthy",
        memory=None,
        activity=None,
        tables=(),
        collection_duration_ms=2,
    )
    connection: MagicMock = MagicMock(spec=AdapterConnection)
    connection.adapter_identity = AdapterIdentity(name="clickhouse")
    connection.load_warehouse_health.return_value = usable
    clock_values: Iterator[float] = iter(test_case.clock_values)
    reader: WarehouseHealthReader = WarehouseHealthReader(clock=lambda: next(clock_values))

    _ = reader.read(
        connection=cast(AdapterConnection, connection),
        database="analytics",
        measured_at="2026-08-23 10:00:00.000",
    )
    second: AdapterWarehouseHealth = reader.read(
        connection=cast(AdapterConnection, connection),
        database="analytics",
        measured_at="2026-08-23 10:00:10.000",
    )

    assert connection.load_warehouse_health.call_count == test_case.expected_provider_reads
    assert second.measured_at == test_case.expected_second_measured_at
