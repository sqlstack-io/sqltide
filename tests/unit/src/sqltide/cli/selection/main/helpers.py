from __future__ import annotations

from dataclasses import replace
from itertools import chain
from pathlib import Path
from typing import cast

import sqltide._native as _native
from sqltide._native import (
    SqlModelAnalyzer,
    build_compiler_adapter_profile,
    compile_pipeline,
    realize_project,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompiledTableModel,
    CompilerAdapterProfile,
)
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    LoadedPipeline,
    Pipeline,
    ReplayBoundary,
    ReplayBoundaryColumns,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import ReplayBoundaryMode, ReplayLineageMode, SourceKind
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.sql_analysis.models import SqlModelAnalysis
from tests.unit.src.sqltide.compiler.compile.helpers import build_realization_analyzer

SELECTOR_PIPELINES_ROOT: Path = Path("tests/fixtures/selector_project/pipelines")


def compile_selector_project_pipelines() -> tuple[CompiledPipeline, ...]:
    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(SELECTOR_PIPELINES_ROOT)
    sql_analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    return tuple(
        compile_pipeline(loaded_pipeline=loaded_pipeline, sql_analyzer=sql_analyzer)
        for loaded_pipeline in loaded_pipelines
    )


def compile_selector_project() -> RealizedProject:
    compiled_pipelines: tuple[CompiledPipeline, ...] = compile_selector_project_pipelines()
    return realize_selector_project(compiled_pipelines)


def realize_selector_project(
    compiled_pipelines: tuple[CompiledPipeline, ...],
) -> RealizedProject:
    compiled_project: CompiledProject = CompiledProject(
        sources=tuple(cast(CompiledSource, pipeline.source) for pipeline in compiled_pipelines),
        models=tuple(chain.from_iterable(pipeline.models for pipeline in compiled_pipelines)),
        pipelines=compiled_pipelines,
        tests=(),
        test_cases=(),
        audits=(),
    )
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    return realize_project(
        project=compiled_project,
        adapter_profile=adapter_profile,
        sql_analyzer=build_realization_analyzer(compiled_project),
    )


def realize_cross_pipeline_reference_project() -> RealizedProject:
    pipelines_by_name: dict[str, CompiledPipeline] = {
        pipeline.pipeline.name: pipeline for pipeline in compile_selector_project_pipelines()
    }
    orders_pipeline: CompiledPipeline = pipelines_by_name["pl__orders"]
    payments_pipeline: CompiledPipeline = pipelines_by_name["pl__payments"]
    orders_models_by_name: dict[str, CompiledModel] = {
        model.key.name: model for model in orders_pipeline.models
    }
    orders_enriched: CompiledTableModel = cast(
        CompiledTableModel,
        orders_models_by_name["orders_enriched"],
    )
    cross_pipeline_query: str = (
        f'{orders_enriched.query}\nJOIN __ref("payments_enriched", ref_type="reference") '
        "AS payments ON 1 = 1"
    )
    cross_pipeline_analysis: SqlModelAnalysis = SqlModelAnalyzer(dialect="clickhouse").analyze(
        sql=cross_pipeline_query,
        engine=orders_enriched.transform.engine,
        order_by=tuple(orders_enriched.transform.order_by),
        partition_by=orders_enriched.transform.partition_by,
        ttl=orders_enriched.transform.ttl,
    )
    mutated_orders_enriched: CompiledModel = replace(
        orders_enriched,
        transform=replace(
            orders_enriched.transform,
            query=cross_pipeline_query,
            sql_file=None,
        ),
        sql_analysis=cross_pipeline_analysis,
    )
    orders_model_names: tuple[str, ...] = tuple(model.key.name for model in orders_pipeline.models)
    orders_enriched_index: int = orders_model_names.index("orders_enriched")
    mutated_orders_models: tuple[CompiledModel, ...] = (
        *orders_pipeline.models[:orders_enriched_index],
        mutated_orders_enriched,
        *orders_pipeline.models[orders_enriched_index + 1 :],
    )
    mutated_orders_pipeline: CompiledPipeline = replace(
        orders_pipeline,
        models=mutated_orders_models,
        effective_replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
    )
    return realize_selector_project((mutated_orders_pipeline, payments_pipeline))


def realize_view_selector_project() -> RealizedProject:
    sql_analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    pipeline: Pipeline = Pipeline(
        name="consumer",
        source=None,
        transforms=[
            ViewStep(
                name="customer_orders",
                relation_name="customer_orders",
                query="SELECT 1::UInt64 AS order_id",
            )
        ],
    )
    compiled_pipeline: CompiledPipeline = compile_pipeline(
        loaded_pipeline=LoadedPipeline(
            pipeline=pipeline,
            file_path=Path("tests/fixtures/selector_project/pipelines/consumer"),
            project=None,
        ),
        sql_analyzer=sql_analyzer,
    )
    compiled_project: CompiledProject = CompiledProject(
        sources=(),
        models=compiled_pipeline.models,
        pipelines=(compiled_pipeline,),
        tests=(),
        test_cases=(),
        audits=(),
    )
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    return realize_project(
        project=compiled_project,
        adapter_profile=adapter_profile,
        sql_analyzer=sql_analyzer,
    )


def build_compiled_external_source_pipeline() -> CompiledPipeline:
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=ExternalTableSourceStep(
            name="orders",
            kind=SourceKind.KAFKA,
            table_name="orders_existing",
            replay_boundary=ReplayBoundary(
                mode=ReplayBoundaryMode.OFFSETS,
                columns=ReplayBoundaryColumns(
                    partition="event_partition",
                    offset="event_offset",
                    timestamp="event_timestamp",
                ),
            ),
        ),
        transforms=[
            TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=["order_id"],
                query=(
                    "SELECT CAST(order_id AS UInt64) AS order_id, "
                    "CAST(_replay_offset AS UInt64) AS _replay_offset "
                    'FROM __ref("orders")'
                ),
            )
        ],
    )
    return compile_pipeline(
        loaded_pipeline=LoadedPipeline(
            pipeline=pipeline,
            file_path=Path("tests/fixtures/selector_project/pipelines/orders"),
            project=None,
        ),
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )
