"""Retain truthful warehouse health across temporary diagnostic failures."""

import time
from collections.abc import Callable
from dataclasses import replace

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterWarehouseHealth
from sqltide.adapter.types import AdapterWarehouseHealthAvailability
from sqltide.dev_server.constants import WAREHOUSE_HEALTH_CACHE_SECONDS


class WarehouseHealthReader:
    """Preserve the last usable snapshot while marking failed refreshes stale."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = WAREHOUSE_HEALTH_CACHE_SECONDS,
    ) -> None:
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._last_usable_by_target: dict[
            tuple[str, str, tuple[str, ...], bool], tuple[float, AdapterWarehouseHealth]
        ] = {}

    def read(
        self,
        *,
        connection: AdapterConnection,
        database: str,
        measured_at: str,
        managed_source_names: tuple[str, ...] = (),
        include_dead_letters: bool = False,
    ) -> AdapterWarehouseHealth:
        """Read diagnostics and retain explicit failure semantics."""

        target: tuple[str, str, tuple[str, ...], bool] = (
            connection.adapter_identity.name,
            database,
            managed_source_names,
            include_dead_letters,
        )
        now: float = self._clock()
        cached: tuple[float, AdapterWarehouseHealth] | None = self._last_usable_by_target.get(
            target
        )
        if cached is not None and now - cached[0] < self._cache_seconds:
            return cached[1]
        try:
            current: AdapterWarehouseHealth = connection.load_warehouse_health(
                database=database,
                managed_source_names=managed_source_names,
                include_dead_letters=include_dead_letters,
            )
        except Exception:
            current = AdapterWarehouseHealth(
                availability="unavailable",
                status="unknown",
                version=None,
                uptime_seconds=None,
                disks=(),
                inode_total=None,
                inode_free=None,
                inode_status="unknown",
                memory=None,
                activity=None,
                tables=None,
                collection_duration_ms=0,
                warnings=("Warehouse diagnostics are unavailable.",),
            )
        if current.availability is not AdapterWarehouseHealthAvailability.UNAVAILABLE:
            measured: AdapterWarehouseHealth = replace(current, measured_at=measured_at)
            self._last_usable_by_target[target] = (now, measured)
            return measured
        if cached is None:
            return replace(current, measured_at=measured_at)
        return replace(
            cached[1],
            availability=AdapterWarehouseHealthAvailability.PARTIAL,
            warnings=current.warnings,
            stale=True,
        )
