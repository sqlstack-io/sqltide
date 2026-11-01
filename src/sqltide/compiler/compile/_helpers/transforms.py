"""Transform compile helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.compile._helpers.retention import render_model_retention
from sqltide.compiler.compile._helpers.sql_contract import analyze_transform_model_sql
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import (
    Column,
    CompiledTableModel,
    CompiledViewModel,
    LogicalResourceKey,
    ModelRetentionResolution,
    ParsedRef,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.constants import DEFAULT_SQL_MODEL_ENGINE
from sqltide.compiler.discovery.models import (
    ModelRetentionPolicy,
    ReplayOnChangePolicy,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    ModelKind,
    RefType,
    ReplayLineageMode,
)
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.models import SqlModelAnalysis


def compile_model(
    *,
    transform: TransformStep,
    pipeline_dir: Path,
    pipeline_name: str,
    replay_lineage_mode: ReplayLineageMode,
    relation_name: str,
    bounded_replay_fallback: BoundedReplayFallback,
    sql_analyzer: SqlModelAnalyzer,
    replay_on_change: ReplayOnChangePolicy | None = None,
    model_retention: ModelRetentionResolution | None = None,
) -> CompiledTableModel:
    """Compile one authored transform into a logical model."""

    query: str = load_transform_query(transform=transform, pipeline_dir=pipeline_dir)
    sql_analysis: SqlModelAnalysis = analyze_transform_model_sql(
        analyzer=sql_analyzer,
        transform_name=transform.name,
        query=query,
        engine=transform.engine,
        order_by=tuple(transform.order_by),
        partition_by=transform.partition_by,
        ttl=transform.ttl,
    )
    if sql_analyzer.uses_prewhere(analysis=sql_analysis):
        raise PipelineCompileError(
            f"Transform '{transform.name}' uses PREWHERE, which ClickHouse does not allow in "
            "materialized views; use WHERE instead"
        )
    output_columns: tuple[Column, ...] = tuple(
        Column(name=column.name, type=column.type) for column in sql_analysis.output_columns
    )
    retention: ModelRetentionResolution = model_retention or ModelRetentionResolution()
    retention_applied: bool = False
    if isinstance(retention.value, ModelRetentionPolicy):
        effective_ttl: str | None = render_model_retention(
            policy=retention.value,
            available_columns={column.name: column.type for column in output_columns},
            model_name=transform.name,
        )
        if effective_ttl is not None:
            retention_applied = True
            transform = replace(transform, ttl=effective_ttl)
            sql_analysis = analyze_transform_model_sql(
                analyzer=sql_analyzer,
                transform_name=transform.name,
                query=query,
                engine=transform.engine,
                order_by=tuple(transform.order_by),
                partition_by=transform.partition_by,
                ttl=effective_ttl,
            )
    parsed_refs: tuple[ParsedRef, ...] = tuple(
        ParsedRef(
            name=reference.name,
            relation_type=reference.relation_type,
            ref_type=None if reference.ref_type is None else RefType(reference.ref_type),
            span=reference.span,
        )
        for reference in sql_analysis.references
    )
    try:
        preserves_required_lineage, replay_anchor_eligible = _native.transform_contract(
            transform.name,
            transform.source,
            _reference_arguments(parsed_refs),
            [column.name for column in output_columns],
            replay_lineage_mode,
            transform.replay_anchor,
            sql_analysis.aggregate_facts.has_semantics,
        )
    except ValueError as error:
        raise PipelineCompileError(str(error)) from None
    return CompiledTableModel(
        key=LogicalResourceKey(resource_type=LogicalResourceType.MODEL, name=transform.name),
        pipeline_name=pipeline_name,
        relation_name=relation_name,
        kind=ModelKind.TABLE,
        sql_analysis=sql_analysis,
        transform=transform,
        preserves_required_lineage=preserves_required_lineage,
        replay_anchor_eligible=replay_anchor_eligible,
        effective_bounded_replay_fallback=bounded_replay_fallback,
        retention=retention,
        retention_applied=retention_applied,
        replay_on_change=replay_on_change,
    )


def compile_view(
    *,
    view: ViewStep,
    pipeline_dir: Path,
    pipeline_name: str,
    relation_name: str,
    sql_analyzer: SqlModelAnalyzer,
) -> CompiledViewModel:
    """Compile one authored terminal view without table or replay semantics."""

    query: str = load_model_query(model=view, pipeline_dir=pipeline_dir)
    sql_analysis: SqlModelAnalysis = analyze_transform_model_sql(
        analyzer=sql_analyzer,
        transform_name=view.name,
        query=query,
        engine=DEFAULT_SQL_MODEL_ENGINE,
        order_by=(),
        partition_by=None,
        ttl=None,
    )
    parsed_refs: tuple[ParsedRef, ...] = tuple(
        ParsedRef(
            name=reference.name,
            relation_type=reference.relation_type,
            ref_type=None if reference.ref_type is None else RefType(reference.ref_type),
            span=reference.span,
        )
        for reference in sql_analysis.references
    )
    view_error: str | None = _native.view_ref_type_error(
        view.name, _reference_arguments(parsed_refs)
    )
    if view_error is not None:
        raise PipelineCompileError(view_error)
    return CompiledViewModel(
        key=LogicalResourceKey(resource_type=LogicalResourceType.MODEL, name=view.name),
        pipeline_name=pipeline_name,
        relation_name=relation_name,
        kind=ModelKind.VIEW,
        sql_analysis=sql_analysis,
        view=view,
    )


def load_transform_query(*, transform: TransformStep, pipeline_dir: Path) -> str:
    """Load a transform query from inline SQL or a relative file."""

    return load_model_query(model=transform, pipeline_dir=pipeline_dir)


def load_model_query(*, model: TransformStep | ViewStep, pipeline_dir: Path) -> str:
    """Load a table or view query from inline SQL or a relative file."""

    if model.query is not None:
        return model.query.strip()

    if model.sql_file is None:
        raise PipelineCompileError(
            f"Model '{model.name}' must define exactly one of 'query' or 'sql_file'"
        )
    sql_file_path: Path = (pipeline_dir / model.sql_file).resolve()
    return sql_file_path.read_text(encoding="utf-8").strip()


def _reference_arguments(
    parsed_refs: tuple[ParsedRef, ...],
) -> list[tuple[str, str, str | None]]:
    return [
        (
            parsed_ref.name,
            parsed_ref.relation_type,
            None if parsed_ref.ref_type is None else parsed_ref.ref_type.value,
        )
        for parsed_ref in parsed_refs
    ]
