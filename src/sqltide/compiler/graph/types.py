"""Dependency graph type declarations."""

from enum import StrEnum


class DependencyEdgeType(StrEnum):
    """Semantic relationship between two logical resources."""

    DRIVING_INPUT = "driving_input"
    REFERENCE = "reference"
    MUTABLE_REFERENCE = "mutable_reference"


class GraphTraversalDirection(StrEnum):
    """Direction followed while collecting graph closure."""

    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


type NativeReplayRule = tuple[str, int | None]
type NativeReplayPolicy = tuple[NativeReplayRule | None, NativeReplayRule | None]
type NativeDesiredObject = tuple[
    int, list[int], str, int | None, str | None, NativeReplayPolicy | None
]
