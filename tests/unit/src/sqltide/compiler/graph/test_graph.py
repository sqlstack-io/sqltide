from pathlib import Path
from types import MappingProxyType

import pytest

from sqltide._native import build_project_graph_from_compiled_project, collect_reachable_keys
from sqltide.compiler.compile.models import CompiledProject
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.graph.constants import (
    ALL_DEPENDENCY_EDGE_TYPES,
    DRIVING_DEPENDENCY_EDGE_TYPES,
)
from sqltide.compiler.graph.exceptions import GraphInputError
from sqltide.compiler.graph.models import DependencyEdge, ProjectGraph
from sqltide.compiler.graph.types import GraphTraversalDirection
from tests.unit.src.sqltide.compiler.compile.helpers import compile_logical_project
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import write_project_toml
from tests.unit.src.sqltide.compiler.graph._test_types import (
    CrossModeRelationshipTestCase,
    CrossPipelineUnknownTestCase,
    FilteredClosureTestCase,
    GraphCycleTestCase,
    ModelReferenceScopeErrorTestCase,
    ModelReferenceScopeSuccessTestCase,
    NonTerminalViewGraphTestCase,
    TypedProjectGraphTestCase,
    ViewAuxiliaryReferenceTestCase,
    ViewGraphTestCase,
)
from tests.unit.src.sqltide.compiler.graph.helpers import (
    build_cyclic_graph_project,
    build_nonterminal_view_graph_project,
    build_terminal_view_graph_project,
    build_typed_graph_project,
    logical_key,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CrossPipelineUnknownTestCase(
            description="rejects an unknown upstream pipeline in the directional allowlist",
            unknown_pipeline="pl__missing",
            expected_error_fragment="unknown pipeline.*pl__missing",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_allowlist_with_unknown_pipeline_when_compiling_then_discovery_rejects_it(
    test_case: CrossPipelineUnknownTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            "[[dependencies.allowed_cross_pipeline_references]]\n"
            f'upstream_pipeline = "{test_case.unknown_pipeline}"\n'
            'downstream_pipeline = "pl__orders"\n[targets.test]\n'
        ),
    )
    write_pipeline_file(
        tmp_path / "sources" / "orders.yml",
        """sources:
  - name: orders
    kind: kafka
    broker_list: kafka:9092
    topic: source.orders
    replay_boundary: {mode: offsets}
""",
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__orders" / "orders.sql",
        'MODEL (order_by ["order_id"]); '
        'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        compile_logical_project(tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        TypedProjectGraphTestCase(
            description="types logical dependencies and orders keys stably",
            expected_ordered_names=(
                "orders",
                "cleaned",
                "lookup",
                "mutable_rates",
                "enriched",
            ),
            expected_enriched_edges=(
                ("cleaned", "driving_input"),
                ("lookup", "reference"),
                ("mutable_rates", "mutable_reference"),
            ),
            expected_lookup_downstream_edges=(("enriched", "reference"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_project_when_building_graph_then_types_and_orders_dependencies(
    test_case: TypedProjectGraphTestCase,
) -> None:
    graph: ProjectGraph = build_project_graph_from_compiled_project(
        project=build_typed_graph_project()
    )
    enriched_edges: tuple[DependencyEdge, ...] = graph.upstream_edges_by_key[
        logical_key("enriched")
    ]
    lookup_edges: tuple[DependencyEdge, ...] = graph.downstream_edges_by_key[logical_key("lookup")]

    assert tuple(key.name for key in graph.ordered_keys) == test_case.expected_ordered_names
    assert tuple((edge.upstream_key.name, edge.edge_type) for edge in enriched_edges) == (
        test_case.expected_enriched_edges
    )
    assert tuple((edge.downstream_key.name, edge.edge_type) for edge in lookup_edges) == (
        test_case.expected_lookup_downstream_edges
    )
    assert isinstance(graph.upstream_edges_by_key, MappingProxyType)
    assert isinstance(graph.downstream_edges_by_key, MappingProxyType)


@pytest.mark.parametrize(
    "test_case",
    [
        FilteredClosureTestCase(
            description="separates execution closure from driving-only replay traversal",
            expected_all_downstream_names=("lookup", "enriched"),
            expected_driving_downstream_names=("lookup",),
            expected_all_upstream_names=(
                "orders",
                "cleaned",
                "lookup",
                "mutable_rates",
                "enriched",
            ),
            expected_driving_upstream_names=("orders", "cleaned", "enriched"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_typed_side_refs_when_collecting_closure_then_filters_by_edge_type(
    test_case: FilteredClosureTestCase,
) -> None:
    graph: ProjectGraph = build_project_graph_from_compiled_project(
        project=build_typed_graph_project()
    )

    assert (
        tuple(
            key.name
            for key in collect_reachable_keys(
                graph=graph,
                root_keys=frozenset({logical_key("lookup")}),
                direction=GraphTraversalDirection.DOWNSTREAM,
                edge_types=ALL_DEPENDENCY_EDGE_TYPES,
            )
        )
        == test_case.expected_all_downstream_names
    )
    assert (
        tuple(
            key.name
            for key in collect_reachable_keys(
                graph=graph,
                root_keys=frozenset({logical_key("lookup")}),
                direction=GraphTraversalDirection.DOWNSTREAM,
                edge_types=DRIVING_DEPENDENCY_EDGE_TYPES,
            )
        )
        == test_case.expected_driving_downstream_names
    )
    assert (
        tuple(
            key.name
            for key in collect_reachable_keys(
                graph=graph,
                root_keys=frozenset({logical_key("enriched")}),
                direction=GraphTraversalDirection.UPSTREAM,
                edge_types=ALL_DEPENDENCY_EDGE_TYPES,
            )
        )
        == test_case.expected_all_upstream_names
    )
    assert (
        tuple(
            key.name
            for key in collect_reachable_keys(
                graph=graph,
                root_keys=frozenset({logical_key("enriched")}),
                direction=GraphTraversalDirection.UPSTREAM,
                edge_types=DRIVING_DEPENDENCY_EDGE_TYPES,
            )
        )
        == test_case.expected_driving_upstream_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        GraphCycleTestCase(
            description="rejects a logical model dependency cycle deterministically",
            expected_error_fragment=(
                "Dependency cycle detected involving: model:alpha, model:beta"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cyclic_models_when_building_graph_then_raises_contextual_error(
    test_case: GraphCycleTestCase,
) -> None:
    with pytest.raises(GraphInputError, match=test_case.expected_error_fragment):
        build_project_graph_from_compiled_project(project=build_cyclic_graph_project())


@pytest.mark.parametrize(
    "test_case",
    [
        ViewGraphTestCase(
            description="adds every view upstream as an ordinary reference dependency",
            expected_upstream_edges=(
                ("lookup", "reference"),
                ("orders", "reference"),
            ),
            expected_downstream_edges=(("summary", "reference"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multi_upstream_terminal_view_when_building_graph_then_adds_reference_edges(
    test_case: ViewGraphTestCase,
) -> None:
    graph: ProjectGraph = build_project_graph_from_compiled_project(
        project=build_terminal_view_graph_project()
    )

    assert (
        tuple(
            (edge.upstream_key.name, edge.edge_type)
            for edge in graph.upstream_edges_by_key[logical_key("summary")]
        )
        == test_case.expected_upstream_edges
    )
    assert (
        tuple(
            (edge.downstream_key.name, edge.edge_type)
            for edge in graph.downstream_edges_by_key[logical_key("lookup")]
        )
        == test_case.expected_downstream_edges
    )


@pytest.mark.parametrize(
    "test_case",
    [
        NonTerminalViewGraphTestCase(
            description="rejects a view with a downstream side-reference model project-wide",
            expected_error_fragment=(
                "View model 'summary' must be terminal; referenced by downstream "
                r"model\(s\): consumer"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_view_with_downstream_model_when_building_graph_then_rejects_nonterminal_view(
    test_case: NonTerminalViewGraphTestCase,
) -> None:
    with pytest.raises(GraphInputError, match=test_case.expected_error_fragment):
        build_project_graph_from_compiled_project(project=build_nonterminal_view_graph_project())


@pytest.mark.parametrize(
    "test_case",
    [
        CrossModeRelationshipTestCase(
            description="rejects a direct model consumed by a virtual pipeline",
            upstream_mode="direct",
            downstream_mode="virtual",
            expected_error_fragment="Relations between direct and virtual",
        ),
        CrossModeRelationshipTestCase(
            description="rejects a virtual model consumed by a direct pipeline",
            upstream_mode="virtual",
            downstream_mode="direct",
            expected_error_fragment="Relations between direct and virtual",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_cross_mode_relationship_when_building_graph_then_it_is_rejected_symmetrically(
    test_case: CrossModeRelationshipTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[dependencies]\nmodel_reference_scope = "pipeline"\n'
            "[[dependencies.allowed_cross_pipeline_references]]\n"
            'upstream_pipeline = "pl__upstream"\n'
            'downstream_pipeline = "pl__downstream"\n'
            "[targets.test]\n"
        ),
    )
    write_pipeline_file(
        tmp_path / "sources" / "orders.yml",
        """
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__upstream" / "pipeline.toml",
        f'mode = "{test_case.upstream_mode}"',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__upstream" / "alpha.sql",
        'MODEL (order_by ["order_id"]); '
        'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__downstream" / "pipeline.toml",
        f'mode = "{test_case.downstream_mode}"',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__downstream" / "beta.sql",
        'MODEL (order_by ["order_id"]); SELECT order_id::UInt64 AS order_id FROM __ref("alpha")',
    )
    project: CompiledProject = compile_logical_project(tmp_path)

    with pytest.raises(GraphInputError, match=test_case.expected_error_fragment):
        build_project_graph_from_compiled_project(project=project)


@pytest.mark.parametrize(
    "test_case",
    [
        ModelReferenceScopeSuccessTestCase(
            description="compatibility default allows cross-pipeline model references",
            dependencies_toml="",
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__ref("alpha")',
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="driving_input",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="explicit project scope allows cross-pipeline model references",
            dependencies_toml=('[dependencies]\nmodel_reference_scope = "project"\n'),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__ref("alpha")',
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="driving_input",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="pipeline scope allows same-pipeline model references",
            dependencies_toml=('[dependencies]\nmodel_reference_scope = "pipeline"\n'),
            upstream_pipeline="pl__models",
            downstream_pipeline="pl__models",
            downstream_query='__ref("alpha")',
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="driving_input",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="pipeline scope allows shared source references",
            dependencies_toml=('[dependencies]\nmodel_reference_scope = "pipeline"\n'),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__source("orders")',
            expected_model_name="beta",
            expected_upstream_name="orders",
            expected_edge_type="driving_input",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="pipeline scope allows shared adopted source references",
            dependencies_toml=('[dependencies]\nmodel_reference_scope = "pipeline"\n'),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__source("orders")',
            expected_model_name="beta",
            expected_upstream_name="orders",
            expected_edge_type="driving_input",
            source_yaml="""sources:
  - name: orders
    kind: stream_table
    table_name: existing_orders
    replay_boundary:
      mode: offsets
      columns:
        _replay_partition: event_partition
        _replay_offset: event_offset
        _replay_timestamp: event_timestamp
""",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="directional allowlist permits a cross-pipeline driving model",
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__upstream"\n'
                'downstream_pipeline = "pl__downstream"\n'
            ),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__ref("alpha")',
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="driving_input",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="directional allowlist permits a cross-pipeline mutable reference",
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__upstream"\n'
                'downstream_pipeline = "pl__downstream"\n'
            ),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query=(
                '__source("orders") AS orders INNER JOIN '
                '__ref("alpha", ref_type="mutable") AS alpha USING order_id'
            ),
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="mutable_reference",
        ),
        ModelReferenceScopeSuccessTestCase(
            description="directional allowlist permits a cross-pipeline immutable reference",
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__upstream"\n'
                'downstream_pipeline = "pl__downstream"\n'
            ),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query=(
                '__source("orders") AS orders INNER JOIN '
                '__ref("alpha", ref_type="reference") AS alpha USING order_id'
            ),
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="reference",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_allowed_model_reference_scope_when_building_graph_then_dependency_is_retained(
    test_case: ModelReferenceScopeSuccessTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            f'name = "test"\ndefault_target = "test"\n{test_case.dependencies_toml}[targets.test]\n'
        ),
    )
    write_pipeline_file(
        tmp_path / "sources" / "orders.yml",
        test_case.source_yaml,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.upstream_pipeline / "alpha.sql",
        'MODEL (order_by ["order_id"]); '
        'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.downstream_pipeline / "beta.sql",
        'MODEL (order_by ["order_id"]); '
        f"SELECT order_id::UInt64 AS order_id FROM {test_case.downstream_query}",
    )
    project: CompiledProject = compile_logical_project(tmp_path)

    graph: ProjectGraph = build_project_graph_from_compiled_project(project=project)

    edges: tuple[DependencyEdge, ...] = graph.upstream_edges_by_key[
        logical_key(test_case.expected_model_name)
    ]
    edge_by_upstream_name: dict[str, DependencyEdge] = {
        edge.upstream_key.name: edge for edge in edges
    }
    expected_edge: DependencyEdge = edge_by_upstream_name[test_case.expected_upstream_name]
    assert str(expected_edge.edge_type) == test_case.expected_edge_type


@pytest.mark.parametrize(
    "test_case",
    [
        ModelReferenceScopeSuccessTestCase(
            description="directional allowlist permits a cross-pipeline terminal view",
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__upstream"\n'
                'downstream_pipeline = "pl__downstream"\n'
            ),
            upstream_pipeline="pl__upstream",
            downstream_pipeline="pl__downstream",
            downstream_query='__ref("alpha")',
            expected_model_name="beta",
            expected_upstream_name="alpha",
            expected_edge_type="reference",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_allowed_cross_pipeline_view_when_graphing_then_reference_edge_is_retained(
    test_case: ModelReferenceScopeSuccessTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            f'name = "test"\ndefault_target = "test"\n{test_case.dependencies_toml}[targets.test]\n'
        ),
    )
    write_pipeline_file(
        tmp_path / "sources" / "orders.yml",
        test_case.source_yaml,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.upstream_pipeline / "alpha.sql",
        'MODEL (order_by ["order_id"]); '
        'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.downstream_pipeline / "beta.sql",
        f"MODEL (kind view); SELECT order_id::UInt64 AS order_id FROM {test_case.downstream_query}",
    )
    project: CompiledProject = compile_logical_project(tmp_path)

    graph: ProjectGraph = build_project_graph_from_compiled_project(project=project)

    edge: DependencyEdge = graph.upstream_edges_by_key[logical_key(test_case.expected_model_name)][
        0
    ]
    assert (edge.upstream_key.name, str(edge.edge_type)) == (
        test_case.expected_upstream_name,
        test_case.expected_edge_type,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ModelReferenceScopeErrorTestCase(
            description="pipeline scope rejects a cross-pipeline driving model",
            downstream_model_sql=(
                'MODEL (order_by ["order_id"]); '
                'SELECT order_id::UInt64 AS order_id FROM __ref("alpha")'
            ),
            expected_error_fragment=(
                "Model 'beta' in pipeline 'pl__downstream' references model 'alpha' in "
                "pipeline 'pl__upstream'"
            ),
        ),
        ModelReferenceScopeErrorTestCase(
            description="pipeline scope rejects a cross-pipeline side model reference",
            downstream_model_sql=(
                'MODEL (order_by ["order_id"]); '
                'SELECT orders.order_id::UInt64 AS order_id FROM __source("orders") AS orders '
                'INNER JOIN __ref("alpha", ref_type="reference") AS alpha USING order_id'
            ),
            expected_error_fragment=("dependencies.model_reference_scope is 'pipeline'"),
        ),
        ModelReferenceScopeErrorTestCase(
            description="pipeline scope rejects a cross-pipeline view reference",
            downstream_model_sql=(
                'MODEL (kind view); SELECT order_id::UInt64 AS order_id FROM __ref("alpha")'
            ),
            expected_error_fragment=("dependencies.model_reference_scope is 'pipeline'"),
        ),
        ModelReferenceScopeErrorTestCase(
            description="allowlist remains directional",
            downstream_model_sql=(
                'MODEL (order_by ["order_id"]); '
                'SELECT order_id::UInt64 AS order_id FROM __ref("alpha")'
            ),
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__downstream"\n'
                'downstream_pipeline = "pl__upstream"\n'
            ),
            expected_error_fragment="no directional allowed_cross_pipeline_references entry",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_scope_cross_pipeline_ref_when_graphing_then_reports_authored_ref(
    test_case: ModelReferenceScopeErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            f'name = "test"\ndefault_target = "test"\n{test_case.dependencies_toml}[targets.test]\n'
        ),
    )
    write_pipeline_file(
        tmp_path / "sources" / "orders.yml",
        """
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__upstream" / "alpha.sql",
        'MODEL (order_by ["order_id"]); '
        'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )
    downstream_path: Path = tmp_path / "pipelines" / "pl__downstream" / "beta.sql"
    write_pipeline_file(downstream_path, test_case.downstream_model_sql)
    project: CompiledProject = compile_logical_project(tmp_path)

    with pytest.raises(GraphInputError, match=test_case.expected_error_fragment) as error_info:
        build_project_graph_from_compiled_project(project=project)

    assert error_info.value.location is not None
    assert error_info.value.location.path == downstream_path


@pytest.mark.parametrize(
    "test_case",
    [
        ViewAuxiliaryReferenceTestCase(
            description="allows SQL tests and audits to target a source-less terminal view",
            expected_test_case_count=1,
            expected_audit_count=1,
            expected_graph_names=("answer",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_terminal_view_auxiliaries_when_building_graph_then_excludes_auxiliary_edges(
    test_case: ViewAuxiliaryReferenceTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "test"\ndefault_target = "test"\n[targets.test]\n',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__views" / "answer.sql",
        "MODEL (kind view); SELECT 1::UInt8 AS value",
    )
    write_pipeline_file(
        tmp_path / "tests" / "answer.sql",
        """
        TEST (name "answer test");
        WITH
          __source__orders AS (SELECT 1::UInt8 AS value),
          __expected__answer AS (SELECT 1::UInt8 AS value)
        SELECT 1
        """,
    )
    write_pipeline_file(
        tmp_path / "audits" / "answer.sql",
        """
        AUDIT (name "answer audit");
        SELECT value FROM __ref("answer") WHERE value = 0
        """,
    )
    project: CompiledProject = compile_logical_project(tmp_path)

    graph: ProjectGraph = build_project_graph_from_compiled_project(project=project)

    assert len(project.test_cases) == test_case.expected_test_case_count
    assert len(project.audits) == test_case.expected_audit_count
    assert tuple(key.name for key in graph.ordered_keys) == test_case.expected_graph_names
