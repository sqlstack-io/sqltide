"""Apache-2.0: SQLBuild compiler/dag/_helpers/artifact.py@7e3b2f854f05."""

import sqltide._native as _native
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.dag.models import DagArtifact, DagEdge, DagNode
from sqltide.compiler.dag.types import DagCheckEdgeType, DagNodeType
from sqltide.compiler.graph.models import DependencyEdge, ProjectGraph
from sqltide.compiler.macros.models import MacroRegistry
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase


def build_dag_artifact(*, graph: ProjectGraph, macro_registry: MacroRegistry) -> DagArtifact:
    """Build one logical source/model/test/audit DAG from the compiled project."""

    nodes: list[DagNode] = []
    nodes.extend(
        DagNode(id=f"source:{source.key.name}", node_type=DagNodeType.SOURCE, name=source.key.name)
        for source in graph.project.sources
    )
    nodes.extend(
        DagNode(id=f"model:{model.key.name}", node_type=DagNodeType.MODEL, name=model.key.name)
        for model in graph.project.models
    )
    loaded_test: LoadedSqlTest
    for loaded_test in sorted(graph.project.tests, key=_test_identity):
        test_name: str = _test_identity(loaded_test)
        nodes.append(DagNode(id=f"test:{test_name}", node_type=DagNodeType.TEST, name=test_name))
    audit: LoadedSqlAudit
    for audit in sorted(graph.project.audits, key=_audit_identity):
        audit_name: str = _audit_identity(audit)
        nodes.append(
            DagNode(id=f"audit:{audit_name}", node_type=DagNodeType.AUDIT, name=audit_name)
        )
    edges: list[DagEdge] = _lineage_edges(graph)
    edges.extend(_test_edges(graph=graph))
    edges.extend(_audit_edges(graph=graph))
    return DagArtifact(
        nodes=tuple(nodes),
        edges=tuple(sorted(set(edges), key=_edge_sort_key)),
        macro_names=tuple(sorted(macro_registry.macros)),
    )


def format_dag_artifact(*, artifact: DagArtifact) -> str:
    """Serialize one logical DAG deterministically with a trailing newline."""

    payload: dict[str, object] = {
        "edges": tuple(
            {
                "edge_type": edge.edge_type,
                "from_id": edge.from_id,
                "to_id": edge.to_id,
            }
            for edge in artifact.edges
        ),
        "metadata": {"dag_version": 1, "tool": "sqltide"},
        "macros": artifact.macro_names,
        "nodes": tuple(
            {"id": node.id, "name": node.name, "type": node.node_type} for node in artifact.nodes
        ),
    }
    return _native.json_dumps(payload, 2, True) + "\n"


def _lineage_edges(graph: ProjectGraph) -> list[DagEdge]:
    edges: list[DagEdge] = []
    dependency_edges: tuple[DependencyEdge, ...]
    for dependency_edges in graph.downstream_edges_by_key.values():
        dependency_edge: DependencyEdge
        for dependency_edge in dependency_edges:
            edges.append(
                DagEdge(
                    from_id=(
                        f"{dependency_edge.upstream_key.resource_type}:"
                        f"{dependency_edge.upstream_key.name}"
                    ),
                    to_id=(
                        f"{dependency_edge.downstream_key.resource_type}:"
                        f"{dependency_edge.downstream_key.name}"
                    ),
                    edge_type=dependency_edge.edge_type,
                )
            )
    return edges


def _test_edges(*, graph: ProjectGraph) -> list[DagEdge]:
    edges: list[DagEdge] = []
    test_case_by_identity: dict[str, SqlTestCase] = {
        _test_case_identity(test_case): test_case for test_case in graph.project.test_cases
    }
    model_names: frozenset[str] = frozenset(model.key.name for model in graph.project.models)
    loaded_test: LoadedSqlTest
    for loaded_test in graph.project.tests:
        test_name: str = _test_identity(loaded_test)
        test_case: SqlTestCase | None = test_case_by_identity.get(test_name)
        if test_case is None:
            continue
        target_names: tuple[str, ...] = tuple(
            sorted(
                {
                    target.target_model_name
                    for target in test_case.target_cases
                    if target.target_model_name in model_names
                }
            )
        )
        target_name: str
        for target_name in target_names:
            edges.append(
                DagEdge(
                    from_id=f"model:{target_name}",
                    to_id=f"test:{test_name}",
                    edge_type=DagCheckEdgeType.TEST,
                )
            )
    return edges


def _audit_edges(*, graph: ProjectGraph) -> list[DagEdge]:
    edges: list[DagEdge] = []
    audit: LoadedSqlAudit
    for audit in graph.project.audits:
        audit_name: str = _audit_identity(audit)
        model_name: str
        for model_name in sorted(set(audit.referenced_model_names)):
            edges.append(
                DagEdge(
                    from_id=f"model:{model_name}",
                    to_id=f"audit:{audit_name}",
                    edge_type=DagCheckEdgeType.AUDIT,
                )
            )
    return edges


def _test_identity(test: LoadedSqlTest) -> str:
    return test.name or test.file_path.stem


def _test_case_identity(test_case: SqlTestCase) -> str:
    return test_case.name or test_case.file_path.stem


def _audit_identity(audit: LoadedSqlAudit) -> str:
    return audit.name or audit.file_path.stem


def _edge_sort_key(edge: DagEdge) -> tuple[str, str, str]:
    return (edge.from_id, edge.to_id, edge.edge_type)
