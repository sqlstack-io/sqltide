"""ClickHouse implementation of the neutral adapter contract."""

from sqltide._native import ClickHouseAdapterCore
from sqltide.adapter.classes.adapter import Adapter


class ClickHouseAdapter(ClickHouseAdapterCore, Adapter):
    """The built-in ClickHouse adapter."""
