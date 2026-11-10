import json

import pytest

from sqltide._native import build_dag_json
from sqltide.compiler.pipeline.models import CompileAnalysis
from tests.unit.src.sqltide.compiler.dag._test_types import DagArtifactTestCase
from tests.unit.src.sqltide.compiler.dag.helpers import analyze_orders_demo


@pytest.mark.parametrize(
    "test_case",
    [
        DagArtifactTestCase(
            description="emits logical resources checks and typed lineage without SQL payloads",
            expected_node_types=("audit", "model", "source", "test"),
            expected_source_node_count=1,
            expected_model_node_count=4,
            expected_has_test_edge=True,
            expected_has_audit_edge=True,
            expected_has_typed_lineage_edge=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_project_graph_when_building_dag_then_emits_logical_typed_artifact(
    test_case: DagArtifactTestCase,
) -> None:
    analysis: CompileAnalysis = analyze_orders_demo()
    rendered: str = build_dag_json(
        graph=analysis.graph,
        macro_registry=analysis.compile_inputs.macro_registry,
    )
    payload: dict[str, object] = json.loads(rendered)
    nodes: tuple[dict[str, object], ...] = tuple(payload["nodes"])
    edges: tuple[dict[str, object], ...] = tuple(payload["edges"])

    assert tuple(sorted({node["type"] for node in nodes})) == test_case.expected_node_types
    assert sum(node["type"] == "source" for node in nodes) == test_case.expected_source_node_count
    assert sum(node["type"] == "model" for node in nodes) == test_case.expected_model_node_count
    assert any(edge["edge_type"] == "test" for edge in edges) is test_case.expected_has_test_edge
    assert any(edge["edge_type"] == "audit" for edge in edges) is test_case.expected_has_audit_edge
    assert any(edge["edge_type"] == "driving_input" for edge in edges) is (
        test_case.expected_has_typed_lineage_edge
    )
    assert "SELECT" not in rendered
