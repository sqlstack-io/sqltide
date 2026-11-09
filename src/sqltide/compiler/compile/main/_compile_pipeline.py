"""Compilation entry points for desired state generation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from sqltide.compiler.compile._helpers.naming import resolve_model_relation_name
from sqltide.compiler.compile._helpers.replay_policies import (
    resolve_bounded_replay_fallback,
    resolve_execution_settings,
    resolve_replay_lineage_mode,
    resolve_replay_on_change,
)
from sqltide.compiler.compile._helpers.retention import effective_model_retention
from sqltide.compiler.compile._helpers.transforms import (
    compile_model,
    compile_view,
    inherited_row_error_policy,
)
from sqltide.compiler.compile.exceptions import (
    PipelineCompileError,
    TransformSqlContractError,
)
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledSource,
    LogicalResourceKey,
    RowErrorContext,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.models import (
    LoadedPipeline,
    Pipeline,
    Project,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import PipelineMode, ReplayLineageMode
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.diagnostics.models import CompilerDiagnostic, SourceLocation
from sqltide.diagnostics.types import DiagnosticPhase, DiagnosticSeverity


def compile_pipeline(
    *, loaded_pipeline: LoadedPipeline, sql_analyzer: SqlModelAnalyzer
) -> CompiledPipeline:
    """Compile an authored pipeline into a minimal desired state representation."""

    pipeline: Pipeline = loaded_pipeline.pipeline
    project: Project | None = loaded_pipeline.project
    replay_lineage_mode: ReplayLineageMode | None = (
        None
        if pipeline.source is None
        else resolve_replay_lineage_mode(loaded_pipeline=loaded_pipeline)
    )
    compiled_source: CompiledSource | None = (
        None
        if pipeline.source is None or replay_lineage_mode is None
        else CompiledSource(
            key=LogicalResourceKey(
                resource_type=LogicalResourceType.SOURCE,
                name=pipeline.source.name,
            ),
            source=pipeline.source,
            effective_replay_lineage_mode=replay_lineage_mode,
        )
    )
    compiled_models: tuple[CompiledModel, ...] = _compile_models(
        loaded_pipeline=loaded_pipeline,
        replay_lineage_mode=replay_lineage_mode,
        sql_analyzer=sql_analyzer,
    )
    return CompiledPipeline(
        pipeline=pipeline,
        project=project,
        file_path=loaded_pipeline.file_path,
        effective_replay_lineage_mode=replay_lineage_mode,
        source=compiled_source,
        models=compiled_models,
    )


def _compile_models(
    *,
    loaded_pipeline: LoadedPipeline,
    replay_lineage_mode: ReplayLineageMode | None,
    sql_analyzer: SqlModelAnalyzer,
) -> tuple[CompiledModel, ...]:
    compiled_models: list[CompiledModel] = []
    model: TransformStep | ViewStep
    for model in loaded_pipeline.pipeline.transforms:
        try:
            compiled_model: CompiledModel
            relation_name: str = resolve_model_relation_name(
                model=model,
                pipeline=loaded_pipeline.pipeline,
                project=loaded_pipeline.project,
            )
            if isinstance(model, ViewStep):
                compiled_model = compile_view(
                    view=model,
                    pipeline_name=loaded_pipeline.pipeline.name,
                    pipeline_dir=loaded_pipeline.file_path,
                    relation_name=relation_name,
                    sql_analyzer=sql_analyzer,
                )
            else:
                if replay_lineage_mode is None:
                    raise PipelineCompileError(
                        f"Table model '{model.name}' belongs to a source-less pipeline"
                    )
                transform: TransformStep = (
                    replace(model, ttl=loaded_pipeline.project.model_ttl)
                    if model.ttl is None
                    and model.retention is None
                    and loaded_pipeline.pipeline.model_retention is None
                    and loaded_pipeline.project is not None
                    and loaded_pipeline.project.model_ttl is not None
                    else model
                )
                transform = replace(
                    transform,
                    execution_settings=resolve_execution_settings(
                        loaded_pipeline=loaded_pipeline,
                        transform=transform,
                    ),
                )
                compiled_model = compile_model(
                    transform=transform,
                    pipeline_name=loaded_pipeline.pipeline.name,
                    pipeline_dir=loaded_pipeline.file_path,
                    replay_lineage_mode=replay_lineage_mode,
                    relation_name=relation_name,
                    sql_analyzer=sql_analyzer,
                    replay_on_change=resolve_replay_on_change(
                        loaded_pipeline=loaded_pipeline,
                        transform=transform,
                    ),
                    bounded_replay_fallback=resolve_bounded_replay_fallback(
                        loaded_pipeline=loaded_pipeline, transform=transform
                    ),
                    model_retention=effective_model_retention(
                        loaded_pipeline=loaded_pipeline,
                        transform=transform,
                    ),
                    row_error_context=RowErrorContext(
                        inherited_policy=inherited_row_error_policy(
                            loaded_pipeline=loaded_pipeline
                        ),
                        pipeline_mode=PipelineMode(loaded_pipeline.pipeline.mode),
                    ),
                )
        except (TransformSqlContractError, PipelineCompileError) as error:
            if error.diagnostic is None:
                error.diagnostic = CompilerDiagnostic(
                    phase=DiagnosticPhase.COMPILATION,
                    severity=DiagnosticSeverity.ERROR,
                    code="SQT-COMPILE-001",
                    message=str(error),
                    resource_name=model.name,
                    location=_transform_error_location(
                        error=error,
                        model=model,
                        pipeline_file_path=loaded_pipeline.file_path,
                    ),
                )
            raise
        compiled_models.append(compiled_model)
    return tuple(compiled_models)


def _transform_error_location(
    *,
    error: TransformSqlContractError | PipelineCompileError,
    model: TransformStep | ViewStep,
    pipeline_file_path: Path,
) -> SourceLocation:
    path: Path = model.source_file_path or pipeline_file_path
    if error.span is None:
        return SourceLocation(
            path=path,
            line=model.source_line,
            column=model.source_column,
        )
    line: int = model.source_line + error.span.line - 1
    end_line: int = model.source_line + error.span.end_line - 1
    column: int = (
        model.source_column + error.span.column - 1 if error.span.line == 1 else error.span.column
    )
    end_column: int = (
        model.source_column + error.span.end_column - 1
        if error.span.end_line == 1
        else error.span.end_column
    )
    return SourceLocation(
        path=path,
        line=line,
        column=column,
        end_line=end_line,
        end_column=end_column,
    )
