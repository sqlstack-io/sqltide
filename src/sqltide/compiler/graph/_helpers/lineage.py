"""Apache-2.0: SQLBuild compiler/graph/_helpers/lineage.py@7e3b2f854f05."""

import sqltide._native as _native
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledProject,
    CompiledTableModel,
    CompiledViewModel,
    LogicalResourceKey,
    ParsedRef,
)
from sqltide.compiler.discovery.models import TransformStep, ViewStep
from sqltide.compiler.discovery.types import ModelReferenceScope
from sqltide.compiler.graph.exceptions import GraphInputError
from sqltide.compiler.graph.models import DependencyEdge, ProjectGraph
from sqltide.compiler.graph.types import DependencyEdgeType
from sqltide.compiler.sql_analysis.models import SqlSourceSpan
from sqltide.diagnostics.models import SourceLocation

_EDGE_TYPES: dict[str, DependencyEdgeType] = {member.value: member for member in DependencyEdgeType}


def build_project_graph(*, project: CompiledProject) -> ProjectGraph:
    """Build, validate, and order one typed logical graph natively."""

    try:
        key_inputs, upstream, downstream, ordered = _native.build_project_graph(
            [source.key.name for source in project.sources],
            [_graph_model(model) for model in project.models],
            [
                (pipeline.pipeline.name, str(pipeline.pipeline.mode))
                for pipeline in project.pipelines
            ],
            project.model_reference_scope != ModelReferenceScope.PROJECT,
            [
                (reference.upstream_pipeline, reference.downstream_pipeline)
                for reference in project.allowed_cross_pipeline_references
            ],
        )
    except ValueError as error:
        raise _graph_input_error(project=project, error=error) from None
    input_keys: tuple[LogicalResourceKey, ...] = (
        *(source.key for source in project.sources),
        *(model.key for model in project.models),
    )
    keys: tuple[LogicalResourceKey, ...] = tuple(input_keys[index] for index in key_inputs)
    upstream_edges: list[tuple[DependencyEdge, ...]] = [
        _upstream_edges(keys=keys, downstream_key=key, edges=edges)
        for key, edges in zip(keys, upstream, strict=True)
    ]
    downstream_edges: list[tuple[DependencyEdge, ...]] = [
        _downstream_edges(upstream_edges=upstream_edges, edges=edges) for edges in downstream
    ]
    return ProjectGraph(
        project=project,
        upstream_edges_by_key=dict(zip(keys, upstream_edges, strict=True)),
        downstream_edges_by_key=dict(zip(keys, downstream_edges, strict=True)),
        ordered_keys=tuple(keys[position] for position in ordered),
    )


def _graph_model(
    model: CompiledModel,
) -> tuple[str, str, str, str | None, list[tuple[str, str | None]]]:
    return (
        model.key.name,
        model.pipeline_name,
        _model_kind(model),
        model.transform.source if isinstance(model, CompiledTableModel) else None,
        [_graph_ref(parsed_ref) for parsed_ref in model.parsed_refs],
    )


def _graph_ref(parsed_ref: ParsedRef) -> tuple[str, str | None]:
    return (parsed_ref.name, None if parsed_ref.ref_type is None else str(parsed_ref.ref_type))


def _graph_input_error(*, project: CompiledProject, error: ValueError) -> GraphInputError:
    message, reference = error.args
    if reference is None:
        return GraphInputError(message)
    model_index, reference_index = reference
    model: CompiledModel = project.models[model_index]
    return GraphInputError(
        message,
        location=_reference_location(model=model, parsed_ref=model.parsed_refs[reference_index]),
    )


def _upstream_edges(
    *,
    keys: tuple[LogicalResourceKey, ...],
    downstream_key: LogicalResourceKey,
    edges: list[tuple[int, str]],
) -> tuple[DependencyEdge, ...]:
    return tuple(
        DependencyEdge(
            upstream_key=keys[upstream_position],
            downstream_key=downstream_key,
            edge_type=_EDGE_TYPES[edge_type],
        )
        for upstream_position, edge_type in edges
    )


def _downstream_edges(
    *, upstream_edges: list[tuple[DependencyEdge, ...]], edges: list[tuple[int, int]]
) -> tuple[DependencyEdge, ...]:
    return tuple(upstream_edges[position][index] for position, index in edges)


def _model_kind(model: CompiledModel) -> str:
    if isinstance(model, CompiledTableModel):
        return "table"
    if isinstance(model, CompiledViewModel):
        return "view"
    return "unsupported"


def _reference_location(*, model: CompiledModel, parsed_ref: ParsedRef) -> SourceLocation | None:
    if isinstance(model, CompiledTableModel):
        step: TransformStep | ViewStep = model.transform
    elif isinstance(model, CompiledViewModel):
        step = model.view
    else:
        return None
    if step.source_file_path is None or parsed_ref.span is None:
        return None
    span: SqlSourceSpan = parsed_ref.span
    return SourceLocation(
        path=step.source_file_path,
        line=step.source_line + span.line - 1,
        column=(step.source_column + span.column - 1 if span.line == 1 else span.column),
        end_line=step.source_line + span.end_line - 1,
        end_column=(
            step.source_column + span.end_column - 1 if span.end_line == 1 else span.end_column
        ),
    )
