"""Immutable dependency graph models."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqltide.compiler.compile.models import CompiledProject, LogicalResourceKey
from sqltide.compiler.graph.types import DependencyEdgeType


@dataclass(frozen=True)
class DependencyEdge:
    """One typed dependency from an upstream resource to a dependent resource."""

    upstream_key: LogicalResourceKey
    downstream_key: LogicalResourceKey
    edge_type: DependencyEdgeType


@dataclass(frozen=True)
class ProjectGraph:
    """One immutable typed graph over a compiled logical project."""

    project: CompiledProject
    upstream_edges_by_key: Mapping[LogicalResourceKey, tuple[DependencyEdge, ...]]
    downstream_edges_by_key: Mapping[LogicalResourceKey, tuple[DependencyEdge, ...]]
    ordered_keys: tuple[LogicalResourceKey, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "upstream_edges_by_key",
            MappingProxyType(dict(self.upstream_edges_by_key)),
        )
        object.__setattr__(
            self,
            "downstream_edges_by_key",
            MappingProxyType(dict(self.downstream_edges_by_key)),
        )
