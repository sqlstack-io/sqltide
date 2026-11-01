"""Backfill decision types."""

from enum import StrEnum


class BoundedReplayOutcome(StrEnum):
    """How a seeded bounded rebuild proceeds after its live root table is inspected."""

    KEEP = "keep"
    WITHOUT_HISTORY = "without_history"
    FULL = "full"
