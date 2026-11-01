"""Dependency graph constants."""

from sqltide.compiler.graph.types import DependencyEdgeType

UPSTREAM_SELECTOR_PREFIX: str = "+"
SELECTOR_NAMESPACE_SEPARATOR: str = ":"
PIPELINE_SELECTOR_NAMESPACE: str = "pipeline"
MODEL_SELECTOR_NAMESPACE: str = "model"

ALL_DEPENDENCY_EDGE_TYPES: frozenset[DependencyEdgeType] = frozenset(DependencyEdgeType)
DRIVING_DEPENDENCY_EDGE_TYPES: frozenset[DependencyEdgeType] = frozenset(
    {DependencyEdgeType.DRIVING_INPUT}
)
