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
