"""ClickHouse-backed implementation of the neutral connection contract."""

from sqltide._native import ClickHouseConnectionCore
from sqltide.adapter.classes.adapter_connection import AdapterConnection


class ClickHouseConnection(ClickHouseConnectionCore, AdapterConnection):
    """A neutral adapter connection backed by the ClickHouse driver."""
