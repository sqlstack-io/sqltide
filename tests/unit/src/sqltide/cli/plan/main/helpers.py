from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import sqltide._native as _native
from sqltide.adapter.models import AdapterDirectFingerprintSnapshot
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.cli.plan.main._run_plan import run_plan
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.compiler.compile.main._compile_pipeline import compile_pipeline
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompilerAdapterProfile,
)
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    LoadedPipeline,
    Pipeline,
    ReplayBoundary,
    ReplayBoundaryColumns,
    TransformStep,
)
from sqltide.compiler.discovery.types import ReplayBoundaryMode, SourceKind
from sqltide.compiler.pipeline.main._realize_project import realize_project
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import DirectWarehouseSnapshot
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.executor.direct.models import DirectBuildRequest
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.compiler.compile.helpers import build_realization_analyzer
from tests.unit.src.sqltide.compiler.planner.helpers import (
    analyze_direct_scope_project,
    build_direct_fingerprint_snapshot,
    build_settled_direct_snapshot,
)
from tests.unit.src.sqltide.executor.direct.main.helpers import (
    build_managed_source_execution_request,
    build_managed_source_snapshot,
)

SELECTOR_PIPELINES_ROOT: Path = Path("tests/fixtures/selector_project/pipelines")
_ABSENT_DIRECT_FINGERPRINTS: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
    status="absent", baselines=()
)


def compile_selector_project_pipelines() -> tuple[CompiledPipeline, ...]:
    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(SELECTOR_PIPELINES_ROOT)
    sql_analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    return tuple(
        compile_pipeline(loaded_pipeline=loaded_pipeline, sql_analyzer=sql_analyzer)
        for loaded_pipeline in loaded_pipelines
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


def build_realized_external_source_project() -> RealizedProject:
    compiled_pipeline: CompiledPipeline = build_compiled_external_source_pipeline()
    compiled_project: CompiledProject = CompiledProject(
        sources=(cast(CompiledSource, compiled_pipeline.source),),
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
        sql_analyzer=build_realization_analyzer(compiled_project),
    )


def run_scope_project_plan(
    *,
    project_root: Path,
    json_output: bool,
    selectors: tuple[str, ...] = (),
    full_refresh: bool = False,
    start_time: str | None = None,
    virtual_environments: bool = False,
    deployment_id: str | None = None,
    changed: bool = False,
    include_missing_upstream: bool = False,
    direct_fingerprints: AdapterDirectFingerprintSnapshot = _ABSENT_DIRECT_FINGERPRINTS,
    warehouse_snapshot: DirectWarehouseSnapshot | None = None,
) -> int:
    """Run `sqt plan` against the direct scope project with a settled warehouse.

    The settled warehouse omits the managed source unless ``warehouse_snapshot`` replaces it.
    """

    settled: DirectWarehouseSnapshot = build_settled_direct_snapshot()
    snapshot: DirectWarehouseSnapshot = warehouse_snapshot or replace(
        settled,
        catalog=replace(
            settled.catalog,
            relations=settled.catalog.relations[3:],
        ),
    )
    return run_plan(
        options=PlanCommandOptions(
            pipelines_root=project_root / "pipelines",
            database=None,
            selectors=selectors,
            full_refresh=full_refresh,
            start_time=start_time,
            deployment_id=deployment_id,
            json_output=json_output,
            verbose=False,
            changed=changed,
            include_missing_upstream=include_missing_upstream,
        ),
        client=RecordingAdapterConnection(
            relations={
                False: snapshot.catalog.relations,
                True: (),
            }[virtual_environments],
            direct_fingerprints=direct_fingerprints,
        ),
        loaded_project=_native.load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )


def read_workflow_artifact(
    *, artifact_root: Path, is_template: bool = False
) -> tuple[bytes, bytes, tuple[str, ...], tuple[bytes, ...]]:
    """Read one complete workflow artifact without interpreting its contents."""

    step_pattern: str = {False: "*.sql", True: "*.sql.template"}[is_template]
    workflow_name: str = {
        False: "workflow.sql",
        True: "workflow.template.sql",
    }[is_template]
    step_paths: tuple[Path, ...] = tuple(sorted((artifact_root / "steps").glob(step_pattern)))
    return (
        (artifact_root / "plan.json").read_bytes(),
        (artifact_root / workflow_name).read_bytes(),
        tuple(path.name for path in step_paths),
        tuple(path.read_bytes() for path in step_paths),
    )


def fail_second_workflow_artifact_replace(
    *, monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    """Fail staged publication after the previous complete artifact has moved."""

    original_replace: Callable[[Path, Path], None] = os.replace

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(error_message)

    replacements: Iterator[Callable[[Path, Path], None]] = iter(
        (original_replace, fail_replace, original_replace)
    )

    def staged_replace(source: Path, target: Path) -> None:
        next(replacements)(source, target)

    monkeypatch.setattr(
        "sqltide.cli.workflow_artifacts._helpers.build_publication.os.replace",
        staged_replace,
    )


def build_drifted_source_plan_inputs(
    *,
    project_root: Path,
    declared_settings: tuple[tuple[str, str], ...],
    catalog_setting_overrides: tuple[tuple[str, str], ...],
    omitted_catalog_settings: tuple[str, ...],
    extra_reader_pipelines: tuple[tuple[str, str], ...] = (),
    changed_model_names: tuple[str, ...] = (),
    protected_pipelines: tuple[str, ...] = (),
) -> tuple[DirectWarehouseSnapshot, AdapterDirectFingerprintSnapshot]:
    """Write the managed-source scope project and observe it deployed with settled SQL."""

    request: DirectBuildRequest = build_managed_source_execution_request(
        project_root=project_root,
        declared_settings=declared_settings,
        extra_reader_pipelines=extra_reader_pipelines,
        protected_pipelines=protected_pipelines,
    )
    return (
        build_managed_source_snapshot(
            request=request,
            catalog_setting_overrides=catalog_setting_overrides,
            omitted_catalog_settings=omitted_catalog_settings,
        ),
        build_direct_fingerprint_snapshot(
            analysis=analyze_direct_scope_project(project_root=project_root),
            changed_model_names=changed_model_names,
        ),
    )
