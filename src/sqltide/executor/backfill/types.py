"""Backfill decision types."""

from enum import StrEnum

from sqltide.adapter.models import AdapterReplayRequest


class BoundedReplayOutcome(StrEnum):
    """How a seeded bounded rebuild proceeds after its live root table is inspected."""

    KEEP = "keep"
    WITHOUT_HISTORY = "without_history"
    FULL = "full"


type NativeVirtualRoot = tuple[
    str, AdapterReplayRequest, bool, tuple[str | None, str, str], bool, list[str]
]
