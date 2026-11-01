"""Deployment lifecycle types."""

from enum import StrEnum


class DeploymentLifecycleState(StrEnum):
    """Authoritative state derived from metadata and live catalog evidence."""

    ACTIVE = "active"
    STAGED = "staged"
    SUPERSEDED = "superseded"
    INCOMPLETE = "incomplete"
    METADATA_MISSING = "metadata_missing"
    PHYSICAL_MISSING = "physical_missing"


class DeploymentDiffStatus(StrEnum):
    """Summary classification for one logical relation comparison."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    PHYSICAL_MISSING = "physical_missing"


type NativeInventoryRecord = tuple[str, str, str, list[str], list[tuple[str, str]]]
type NativeDeploymentSummary = tuple[
    str,
    str,
    str | None,
    str | None,
    list[str],
    list[str],
    list[str],
    list[str],
    str | None,
]
