from dataclasses import replace
from pathlib import Path
from typing import cast

from sqltide._native import (
    render_clickhouse_metadata_migration_workflow,
)
from sqltide.adapter.exceptions import AdapterAuthenticationError, AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterMutationResult,
    AdapterQueryResult,
    AdapterTable,
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapters.clickhouse.main.database_scoped_consumer_group import (
    database_scoped_consumer_group,
)
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    CompiledProject,
    LogicalResourceKey,
)
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import DirectPlan, DirectWarehouseSnapshot
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.executor.direct.models import DirectBuildRequest
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.compiler.planner.helpers import (
    analyze_direct_scope_project,
    build_direct_snapshot,
    plan_direct_scope,
    write_direct_scope_project,
)


class RecordingDirectBuildConnection(RecordingAdapterConnection):
    def __init__(self) -> None:
        snapshot: DirectWarehouseSnapshot = _direct_prerequisite_snapshot()
        super().__init__(relations=snapshot.catalog.relations)
        self.workflow_mutation_statements: list[str] = []

    def query(self, statement: str) -> AdapterQueryResult:
        self.statements.append(statement)
        return AdapterQueryResult(rows=())

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        self.workflow_mutation_statements.append(statement)
        return AdapterMutationResult(written_rows=7)

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        return render_clickhouse_metadata_migration_workflow(database)


class RejectingStagedSourceConnection(RecordingDirectBuildConnection):
    """Reject one statement the way the warehouse rejects an unknown Kafka setting."""

    def __init__(
        self,
        *,
        rejected_prefix: str = "CREATE TABLE analytics._sqltide_next__kafka__orders",
    ) -> None:
        super().__init__()
        self.attempted_mutations: list[str] = []
        self._rejected_prefix = rejected_prefix

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        self.attempted_mutations.append(statement)
        return {
            True: self._reject_mutation,
            False: super().execute_workflow_sql,
        }[statement.startswith(self._rejected_prefix)](statement)

    def _reject_mutation(self, statement: str) -> AdapterMutationResult:
        del statement
        raise AdapterWarehouseError("Unknown setting 'kafka_unknown_setting': for storage Kafka")


class FailingPausedSourceConnection(RecordingDirectBuildConnection):
    def __init__(self) -> None:
        super().__init__()
        self.attempted_mutations: list[str] = []

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        self.attempted_mutations.append(statement)
        return {
            True: self._reject_mutation,
            False: super().execute_workflow_sql,
        }[statement == "SELECT invalid;"](statement)

    def _reject_mutation(self, statement: str) -> AdapterMutationResult:
        del statement
        raise AdapterWarehouseError("injected failure while source is paused")


class DistinctCaptureDirectBuildConnection(RecordingDirectBuildConnection):
    def __init__(self) -> None:
        super().__init__()
        self._capture_results: list[AdapterQueryResult] = [
            AdapterQueryResult(rows=()),
            _capture_result(lower_value="10", upper_value="11"),
            _capture_result(lower_value="20", upper_value="21"),
        ]

    def query(self, statement: str) -> AdapterQueryResult:
        self.statements.append(statement)
        return self._capture_results.pop(0)


class MismatchedCaptureDirectBuildConnection(DistinctCaptureDirectBuildConnection):
    def __init__(self) -> None:
        super().__init__()
        self._capture_results[1] = _capture_result(
            lower_value="10",
            upper_value="11",
            driving_input_relation_name="tbl__wrong_root",
        )


class InvalidOffsetCaptureDirectBuildConnection(DistinctCaptureDirectBuildConnection):
    def __init__(self) -> None:
        super().__init__()
        invalid_result: AdapterQueryResult = self._capture_results[1]
        self._capture_results[1] = AdapterQueryResult(
            rows=(
                (
                    "tbl__alpha",
                    "offsets",
                    "_replay_offsets",
                    None,
                    *invalid_result.rows[0][4:],
                ),
            ),
            column_names=invalid_result.column_names,
        )


class DeniedFingerprintRenderingConnection(RecordingDirectBuildConnection):
    def render_direct_fingerprint_observations(
        self,
        *,
        database: str,
        fingerprints: tuple[AdapterDirectFingerprintRecord, ...],
    ) -> tuple[str, ...]:
        del database, fingerprints
        raise AdapterAuthenticationError("injected fingerprint rendering denial")


class RecordingFingerprintConnection(RecordingDirectBuildConnection):
    def __init__(self) -> None:
        super().__init__()
        self.fingerprints: tuple[AdapterDirectFingerprintRecord, ...] = ()

    def render_direct_fingerprint_observations(
        self,
        *,
        database: str,
        fingerprints: tuple[AdapterDirectFingerprintRecord, ...],
    ) -> tuple[str, ...]:
        del database
        self.fingerprints = fingerprints
        return ()


def build_direct_execution_request(
    *,
    project_root: Path,
    selected_model_names: tuple[str, ...],
    selection_mode: DirectSelectionMode | None = None,
) -> DirectBuildRequest:
    write_direct_scope_project(project_root=project_root)
    return _analyze_direct_execution_request(
        project_root=project_root,
        selected_model_names=selected_model_names,
        selection_mode=selection_mode,
    )


def build_direct_execution_request_with_unrelated_pipeline(
    *, project_root: Path, selected_model_names: tuple[str, ...]
) -> DirectBuildRequest:
    write_direct_scope_project(project_root=project_root)
    _write_unrelated_pipeline(project_root=project_root)
    return _analyze_direct_execution_request(
        project_root=project_root,
        selected_model_names=selected_model_names,
    )


def _analyze_direct_execution_request(
    *,
    project_root: Path,
    selected_model_names: tuple[str, ...],
    selection_mode: DirectSelectionMode | None = None,
) -> DirectBuildRequest:
    analysis: CompileAnalysis = analyze_direct_scope_project(project_root=project_root)
    plan: DirectPlan = plan_direct_scope(
        analysis=analysis,
        snapshot=_direct_prerequisite_snapshot(),
        selected_model_names=selected_model_names,
        selection_mode=selection_mode,
    )
    return DirectBuildRequest(
        plan=plan,
        realized_project=analysis.realized_project,
        database="analytics",
        metadata_database="analytics",
        tool_version="test",
        stabilization_seconds=0,
    )


def _write_unrelated_pipeline(*, project_root: Path) -> None:
    (project_root / "sources" / "unrelated.yml").write_text(
        "sources:\n"
        "  - name: unrelated\n"
        "    kind: kafka\n"
        "    broker_list: kafka:9092\n"
        "    topic: source.unrelated\n"
        "    replay_boundary: {mode: offsets}\n",
        encoding="utf-8",
    )
    pipeline_root: Path = project_root / "pipelines" / "pl__unrelated"
    pipeline_root.mkdir(parents=True)
    (pipeline_root / "unrelated_model.sql").write_text(
        'MODEL (order_by ["event_id"]);\n'
        'SELECT event_id::UInt64 AS event_id FROM __source("unrelated")\n',
        encoding="utf-8",
    )


def build_direct_execution_snapshot() -> DirectWarehouseSnapshot:
    return _direct_prerequisite_snapshot()


def _capture_result(
    *,
    lower_value: str,
    upper_value: str,
    driving_input_relation_name: str = "tbl__alpha",
) -> AdapterQueryResult:
    return AdapterQueryResult(
        rows=(
            (
                driving_input_relation_name,
                "offsets",
                "_replay_partition=0",
                "0",
                "_replay_partition",
                "_replay_offset",
                "_replay_timestamp",
                lower_value,
                upper_value,
                upper_value,
                True,
                "2026-08-07 12:00:00.000",
            ),
        ),
        column_names=(
            "driving_input_relation_name",
            "replay_boundary_mode",
            "boundary_key",
            "partition_value",
            "source_partition_column_name",
            "source_position_column_name",
            "source_timestamp_column_name",
            "lower_value",
            "upper_value",
            "replay_cutoff_value",
            "cutoff_inclusive",
            "captured_at",
        ),
    )


def _direct_prerequisite_snapshot() -> DirectWarehouseSnapshot:
    return build_direct_snapshot(
        relation_names=("tbl__alpha", "mv__alpha"),
    )


def build_managed_source_execution_request(
    *,
    project_root: Path,
    declared_settings: tuple[tuple[str, str], ...],
    selected_model_names: tuple[str, ...] = ("alpha",),
    extra_reader_pipelines: tuple[tuple[str, str], ...] = (),
    protected_pipelines: tuple[str, ...] = (),
) -> DirectBuildRequest:
    """Plan the scope project plus a downstream pipeline that only reads ``alpha``.

    Each ``(pipeline name, mode)`` in ``extra_reader_pipelines`` adds a pipeline whose single
    ``<name>_model`` reads the ``orders`` source directly; ``protected_pipelines`` require a
    confirmation to rebuild.
    """

    write_direct_scope_project(project_root=project_root)
    pipeline_name: str
    pipeline_mode: str
    for pipeline_name, pipeline_mode in extra_reader_pipelines:
        reader_root: Path = project_root / "pipelines" / pipeline_name
        reader_root.mkdir(parents=True)
        (reader_root / "pipeline.toml").write_text(f'mode = "{pipeline_mode}"\n', encoding="utf-8")
        (reader_root / f"{pipeline_name.removeprefix('pl__')}_model.sql").write_text(
            'MODEL (order_by ["order_id"]);\n'
            'SELECT order_id::UInt64 AS order_id FROM __source("orders")\n',
            encoding="utf-8",
        )
    protected_name: str
    for protected_name in protected_pipelines:
        with (project_root / "pipelines" / protected_name / "pipeline.toml").open(
            "a", encoding="utf-8"
        ) as config:
            config.write(
                f'[protection]\nwarning = "{protected_name} serves customers"\n'
                f'confirmation = "rebuild:{protected_name}"\n'
            )
    summary_root: Path = project_root / "pipelines" / "pl__order_summary"
    summary_root.mkdir(parents=True)
    (summary_root / "order_summary.sql").write_text(
        'MODEL (order_by ["order_id"]);\nSELECT order_id::UInt64 AS order_id FROM __ref("alpha")\n',
        encoding="utf-8",
    )
    source_path: Path = project_root / "sources" / "orders.yml"
    settings_yml: str = "".join(
        f"      {setting_name}: '{setting_value}'\n"
        for setting_name, setting_value in declared_settings
    )
    source_path.write_text(
        source_path.read_text(encoding="utf-8")
        + {True: f"    settings:\n{settings_yml}", False: ""}[bool(declared_settings)],
        encoding="utf-8",
    )
    return _analyze_direct_execution_request(
        project_root=project_root,
        selected_model_names=selected_model_names,
    )


def without_models(
    *, request: DirectBuildRequest, model_names: frozenset[str]
) -> DirectBuildRequest:
    """Drop the named models from every pipeline while keeping each pipeline's source."""

    project: CompiledProject = request.realized_project.project
    pipelines: tuple[CompiledPipeline, ...] = tuple(
        replace(
            pipeline,
            models=tuple(filter(lambda model: model.key.name not in model_names, pipeline.models)),
        )
        for pipeline in project.pipelines
    )
    return replace(
        request,
        realized_project=replace(
            request.realized_project,
            project=replace(project, pipelines=pipelines),
        ),
    )


def build_managed_source_snapshot(
    *,
    request: DirectBuildRequest,
    catalog_setting_overrides: tuple[tuple[str, str], ...],
    omitted_catalog_settings: tuple[str, ...] = (),
    extra_landing_columns: tuple[tuple[str, str], ...] = (),
) -> DirectWarehouseSnapshot:
    """Observe the request's own managed source as deployed, then apply out-of-band drift."""

    kafka: AdapterManagedSource
    landing: AdapterTable
    landing_view: AdapterMaterializedView
    kafka, landing, landing_view = cast(
        tuple[AdapterManagedSource, AdapterTable, AdapterMaterializedView],
        request.realized_project.resources_by_logical_key[LogicalResourceKey("source", "orders")],
    )
    deployed_settings: dict[str, str] = {
        "kafka_broker_list": kafka.broker_list,
        "kafka_topic_list": kafka.topic,
        "kafka_group_name": database_scoped_consumer_group(
            consumer_group=kafka.consumer_group, database=request.database
        ),
        "kafka_format": kafka.format,
        **dict(kafka.settings),
        **dict(catalog_setting_overrides),
    }
    setting_name: str
    for setting_name in omitted_catalog_settings:
        del deployed_settings[setting_name]
    landing_columns: tuple[CatalogColumn, ...] = tuple(
        CatalogColumn(name=column.name, type=column.type) for column in landing.columns
    )
    extra_columns: tuple[CatalogColumn, ...] = tuple(
        CatalogColumn(name=column_name, type=column_type)
        for column_name, column_type in extra_landing_columns
    )
    base: DirectWarehouseSnapshot = _direct_prerequisite_snapshot()
    return DirectWarehouseSnapshot(
        catalog=CatalogSnapshot(
            identity=base.catalog.identity,
            warehouse_timezone=base.catalog.warehouse_timezone,
            relations=(
                *base.catalog.relations,
                CatalogRelation(
                    name=kafka.name,
                    engine="Kafka",
                    columns=tuple(
                        CatalogColumn(name=column.name, type=column.type)
                        for column in kafka.columns
                    ),
                    settings=tuple(
                        (name, f"'{value}'") for name, value in deployed_settings.items()
                    ),
                ),
                CatalogRelation(
                    name=landing.name,
                    engine="MergeTree",
                    columns=(*landing_columns, *extra_columns),
                ),
                CatalogRelation(
                    name=landing_view.name,
                    engine="MaterializedView",
                    columns=(),
                    source_relation_names=(kafka.name,),
                    target_relation_name=landing_view.target_relation_name,
                ),
            ),
        ),
    )
