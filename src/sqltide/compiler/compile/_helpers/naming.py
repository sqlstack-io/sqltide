"""Compile-local authored model relation naming over the native rules."""

import sqltide._native as _native
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import CompiledProject
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    Pipeline,
    Project,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import ModelKind


def resolve_model_relation_name(
    *, model: TransformStep | ViewStep, pipeline: Pipeline, project: Project | None
) -> str:
    """Resolve and validate one model relation name using authored precedence."""

    is_table: bool = isinstance(model, TransformStep)
    try:
        return _native.resolve_model_relation_name(
            model.name,
            is_table,
            model.relation_name,
            pipeline.naming.table_prefix if is_table else pipeline.naming.view_prefix,
            None
            if project is None
            else project.naming.table_prefix
            if is_table
            else project.naming.view_prefix,
        )
    except ValueError as error:
        raise PipelineCompileError(str(error)) from None


def validate_compiled_project_relation_names(*, project: CompiledProject) -> None:
    """Reject project-wide physical relation collisions before graph or realization work."""

    try:
        _native.validate_project_relation_names(
            [
                (
                    source.key.name,
                    source.source.table_name
                    if isinstance(source.source, ExternalTableSourceStep)
                    else None,
                )
                for source in project.sources
            ],
            [
                (model.key.name, model.relation_name, model.kind == ModelKind.TABLE)
                for model in project.models
            ],
        )
    except ValueError as error:
        raise PipelineCompileError(str(error)) from None
