"""Build one typed graph from a compiled logical project."""

from sqltide.compiler.compile.models import CompiledProject
from sqltide.compiler.graph._helpers.lineage import build_project_graph
from sqltide.compiler.graph.models import ProjectGraph


def build_project_graph_from_compiled_project(*, project: CompiledProject) -> ProjectGraph:
    """Build and validate one immutable typed logical graph."""

    return build_project_graph(project=project)
