import json
from collections.abc import Callable
from dataclasses import replace
from itertools import chain
from pathlib import Path
from typing import cast

import sqltide._native as _native
from sqltide._native import (
    DirectModelFingerprint,
    SqlModelAnalyzer,
    analyze_project,
    plan_deployment,
    plan_direct_build,
    realize_project,
)
from sqltide._native import compile_pipeline as compile_pipeline_impl
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterCapabilities,
    AdapterDeploymentInventory,
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterMutationResult,
    AdapterQueryResult,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterRelationCleanupRequest,
    AdapterReplayCoverageRequest,
    AdapterStableView,
    AdapterTable,
    AdapterView,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
    DESIRED_OBJECT_TYPE_TABLE,
    REPLAY_CURSOR_COLUMN_NAME,
    REPLAY_LANDED_AT_COLUMN_NAME,
    REPLAY_OFFSET_COLUMN_NAME,
    REPLAY_PARTITION_COLUMN_NAME,
    REPLAY_TIMESTAMP_COLUMN_NAME,
)
from sqltide.compiler.compile.models import (
    Column,
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompilerAdapterProfile,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    KafkaTableSpec,
    LogicalResourceKey,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
    ViewSpec,
)
from sqltide.compiler.compile.models import (
    KafkaSettings as CompiledKafkaSettings,
)
from sqltide.compiler.compile.types import DesiredObjectType
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaSettings,
    LoadedPipeline,
    Pipeline,
    ReplayBoundary,
    ReplayBoundaryColumns,
    ReplayOnChangePolicy,
    TransformStep,
)
from sqltide.compiler.discovery.types import (
    ReplayAnchorMode,
    ReplayBoundaryMode,
    ReplayLineageMode,
    SourceKind,
)
from sqltide.compiler.pipeline.models import CompileAnalysis, RealizedProject
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualState,
    ActualTable,
    DeploymentRecord,
    DeploymentWatermarkRecord,
    DirectPlan,
    DirectRelationOperation,
    DirectWarehouseSnapshot,
    ObjectStateRecord,
    PlannedObjectChange,
    PlanningWarehouseSnapshot,
    PreparedObjectMapping,
    PublishEventRecord,
)
from sqltide.compiler.planner.types import ActualObject, DesiredObject, DirectSelectionMode
from tests.unit.src.sqltide.compiler.compile.helpers import build_realization_analyzer

EXAMPLE_PIPELINE_DIRECTORY: Path = Path("tests/fixtures/basic_project/pipelines/pl__orders")


def compile_pipeline(loaded_pipeline: LoadedPipeline) -> CompiledPipeline:
    return compile_pipeline_impl(
        loaded_pipeline=loaded_pipeline,
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )


class SnapshotRecordingConnection(AdapterConnection):
    def __init__(
        self,
        *,
        catalog: CatalogSnapshot,
        metadata_result: AdapterQueryResult,
        virtual_environments: bool,
        direct_rebuild: bool = True,
    ) -> None:
        self._catalog: CatalogSnapshot = catalog
        self._metadata_result: AdapterQueryResult = metadata_result
        self._capabilities: AdapterCapabilities = AdapterCapabilities(
            virtual_environments=virtual_environments,
            managed_source_kinds=frozenset({"kafka"}),
            replay_boundary_modes=frozenset(AdapterReplayBoundaryMode),
            history_prefix_seed=True,
            stable_logical_bindings=True,
            per_relation_atomic_replace=True,
            graph_atomic_publish=False,
            set_difference_comparison=True,
            direct_rebuild=direct_rebuild,
        )
        self.catalog_load_count: int = 0
        self.query_count: int = 0

    @property
    def adapter_identity(self) -> AdapterIdentity:
        return self._catalog.identity.adapter

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities

    def load_catalog(self, database: str) -> CatalogSnapshot:
        del database
        self.catalog_load_count += 1
        return self._catalog

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        del database, table
        return frozenset()

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        del database
        return InspectedManagedTableState(active_bindings=(), physical_candidates=())

    def query(self, statement: str) -> AdapterQueryResult:
        del statement
        self.query_count += 1
        return self._metadata_result

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        del statement
        return AdapterMutationResult()

    def capture_warehouse_timestamp(self) -> str:
        return "2026-07-31 12:00:00.000"

    def render_ensure_database(self, database: str) -> str:
        return f"CREATE DATABASE IF NOT EXISTS {database};"

    def render_resource(
        self,
        *,
        resource: AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView,
        database: str,
        if_not_exists: bool = False,
    ) -> str:
        return ClickHouseAdapter().render_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        del database
        return ()

    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]:
        del database, state
        return ()

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        del database
        return AdapterDeploymentInventory(deployments=(), publish_events=())

    def render_replay_coverage_query(self, request: AdapterReplayCoverageRequest) -> str:
        del request
        return "SELECT '[]' AS value"

    def compare_readiness(
        self, request: AdapterReadinessRequest
    ) -> tuple[AdapterReadinessRootObservation, ...]:
        del request
        return ()

    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]:
        del request
        return ()

    def render_cleanup_relations(self, request: AdapterRelationCleanupRequest) -> tuple[str, ...]:
        del request
        return ()

    def close(self) -> None:
        return None


def build_snapshot_catalog() -> CatalogSnapshot:
    return CatalogSnapshot(
        identity=CatalogIdentity(
            adapter=AdapterIdentity(name="clickhouse"),
            database="analytics",
        ),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="tbl__orders",
                engine="MergeTree",
                columns=(CatalogColumn(name="order_id", type="String"),),
            ),
        ),
    )


def realize_compiled_pipelines(
    compiled_pipelines: tuple[CompiledPipeline, ...],
) -> RealizedProject:
    sources_by_name: dict[str, CompiledSource] = {
        cast(CompiledSource, pipeline.source).key.name: cast(CompiledSource, pipeline.source)
        for pipeline in compiled_pipelines
    }
    compiled_project: CompiledProject = CompiledProject(
        sources=tuple(sources_by_name.values()),
        models=tuple(chain.from_iterable(pipeline.models for pipeline in compiled_pipelines)),
        pipelines=compiled_pipelines,
        tests=(),
        test_cases=(),
        audits=(),
    )
    sql_analyzer: SqlModelAnalyzer = build_realization_analyzer(compiled_project)
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    return realize_project(
        project=compiled_project,
        adapter_profile=adapter_profile,
        sql_analyzer=sql_analyzer,
    )


def build_metadata_records() -> tuple[
    tuple[ObjectStateRecord, ...],
    tuple[DeploymentRecord, ...],
    tuple[DeploymentWatermarkRecord, ...],
    tuple[PublishEventRecord, ...],
]:
    root_key: ObjectKey = ObjectKey(
        database=None, object_type=DESIRED_OBJECT_TYPE_TABLE, name="raw__orders"
    )
    transform_key: ObjectKey = ObjectKey(
        database=None,
        object_type=DESIRED_OBJECT_TYPE_TABLE,
        name="tbl__orders_enriched",
    )
    mv_key: ObjectKey = ObjectKey(
        database=None,
        object_type=DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
        name="mv__orders_enriched",
    )
    object_states: tuple[ObjectStateRecord, ...] = (
        ObjectStateRecord(
            deployment_id="20260408T120000Z_ab12cd",
            key=transform_key,
            normalized_fingerprint="fingerprint_transform",
            normalized_query="SELECT * FROM raw__orders",
            recorded_at="2026-04-08T12:00:00Z",
        ),
        ObjectStateRecord(
            deployment_id="20260408T120000Z_ab12cd",
            key=mv_key,
            normalized_fingerprint="fingerprint_mv",
            normalized_query="SELECT * FROM raw__orders",
            recorded_at="2026-04-08T12:00:01Z",
        ),
    )
    deployments: tuple[DeploymentRecord, ...] = (
        DeploymentRecord(
            deployment_id="20260408T130000Z_cd34ef",
            created_at="2026-04-08T13:00:00Z",
            status="backfilling",
            replay_lineage_mode="offsets",
            selected_root_keys=(transform_key, root_key),
            warning_codes=("z_warning", "a_warning"),
            prepared_object_mappings=(
                PreparedObjectMapping(
                    logical_key=mv_key,
                    physical_name="mv__orders_enriched__20260408T130000Z_cd34ef",
                    logical_model_name="orders_enriched",
                ),
                PreparedObjectMapping(
                    logical_key=transform_key,
                    physical_name="tbl__orders_enriched__20260408T130000Z_cd34ef",
                    logical_model_name="orders_enriched",
                ),
            ),
        ),
        DeploymentRecord(
            deployment_id="20260408T120000Z_ab12cd",
            created_at="2026-04-08T12:00:00Z",
            status="published",
            replay_lineage_mode="timestamp",
            selected_root_keys=(root_key,),
            warning_codes=(),
            prepared_object_mappings=(),
        ),
    )
    watermarks: tuple[DeploymentWatermarkRecord, ...] = (
        DeploymentWatermarkRecord(
            deployment_id="20260408T130000Z_cd34ef",
            root_key=transform_key,
            anchor_key=root_key,
            boundary_key="partition:1",
            cutoff_value="54321",
        ),
        DeploymentWatermarkRecord(
            deployment_id="20260408T130000Z_cd34ef",
            root_key=transform_key,
            anchor_key=root_key,
            boundary_key="partition:0",
            cutoff_value="12345",
        ),
    )
    publish_events: tuple[PublishEventRecord, ...] = (
        PublishEventRecord(
            deployment_id="20260408T120000Z_ab12cd",
            published_at="2026-04-08T12:30:00Z",
            logical_view_names=("tbl__orders_enriched",),
        ),
    )
    return object_states, deployments, watermarks, publish_events


def build_example_desired_state() -> DesiredState:
    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(EXAMPLE_PIPELINE_DIRECTORY)
    return realize_compiled_pipelines((compile_pipeline(loaded_pipeline),)).desired_state


def build_view_only_desired_state() -> DesiredState:
    view_key: ObjectKey = ObjectKey(database=None, object_type="view", name="customer_orders")
    return DesiredState(
        objects=(
            DesiredView(
                key=view_key,
                deps=(ObjectKey(database=None, object_type="table", name="orders_rollup"),),
                spec=ViewSpec(
                    query="SELECT order_id FROM orders_rollup",
                    database_template=(
                        "SELECT order_id FROM __sqltide_target_database__.orders_rollup"
                    ),
                ),
                logical_model_name="customer_orders",
            ),
        ),
        replay_anchor_keys=frozenset(),
        mutable_ref_warning_keys=frozenset(),
    )


def build_single_transform_desired_state(
    *,
    query: str,
    replay_lineage_mode: ReplayLineageMode | str = ReplayLineageMode.OFFSETS,
    replay_anchor: ReplayAnchorMode | str = ReplayAnchorMode.AUTO,
    order_by: tuple[str, ...] = ("order_id",),
    supporting_transforms: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> DesiredState:
    resolved_replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(replay_lineage_mode)
    resolved_replay_anchor: ReplayAnchorMode = ReplayAnchorMode(replay_anchor)
    supporting_transform_steps: list[TransformStep] = [
        TransformStep(
            name=name,
            source="orders",
            engine="MergeTree()",
            order_by=list(transform_order_by),
            query=transform_query,
        )
        for name, transform_query, transform_order_by in supporting_transforms
    ]
    pipeline: Pipeline = Pipeline(
        name="tmp_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders",
                consumer_group="sqltide_tmp_pipeline_orders",
            ),
            replay_boundary=ReplayBoundary(
                mode=ReplayBoundaryMode(resolved_replay_lineage_mode),
                columns=ReplayBoundaryColumns(),
            ),
        ),
        transforms=[
            *supporting_transform_steps,
            TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=list(order_by),
                query=query,
                replay_anchor=resolved_replay_anchor,
            ),
        ],
    )
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=pipeline,
        file_path=EXAMPLE_PIPELINE_DIRECTORY,
    )
    return realize_compiled_pipelines((compile_pipeline(loaded_pipeline),)).desired_state


def build_preservation_matrix_compiled_pipeline(
    *, source_ownership: str, replay_lineage_mode: ReplayLineageMode | str
) -> CompiledPipeline:
    resolved_replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(replay_lineage_mode)
    builder: Callable[[ReplayLineageMode], CompiledPipeline] = PRESERVATION_PIPELINE_BUILDERS[
        source_ownership
    ]
    return builder(resolved_replay_lineage_mode)


def _build_managed_preservation_compiled_pipeline(
    replay_lineage_mode: ReplayLineageMode,
) -> CompiledPipeline:
    pipeline: Pipeline = Pipeline(
        name="preservation_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders",
            ),
            replay_boundary=ReplayBoundary(
                mode=ReplayBoundaryMode(replay_lineage_mode),
                columns=ReplayBoundaryColumns(),
            ),
        ),
        transforms=[_build_preservation_transform(replay_lineage_mode)],
    )
    return compile_pipeline(LoadedPipeline(pipeline=pipeline, file_path=EXAMPLE_PIPELINE_DIRECTORY))


def _build_adopted_preservation_compiled_pipeline(
    replay_lineage_mode: ReplayLineageMode,
) -> CompiledPipeline:
    replay_boundary_mode: ReplayBoundaryMode = ReplayBoundaryMode(replay_lineage_mode)
    pipeline: Pipeline = Pipeline(
        name="preservation_pipeline",
        source=ExternalTableSourceStep(
            name="orders",
            kind=PRESERVATION_EXTERNAL_SOURCE_KIND_BY_MODE[replay_lineage_mode],
            table_name="orders_existing",
            replay_boundary=ReplayBoundary(
                mode=replay_boundary_mode,
                columns=PRESERVATION_BOUNDARY_COLUMNS_BY_MODE[replay_lineage_mode],
            ),
        ),
        transforms=[_build_preservation_transform(replay_lineage_mode)],
    )
    return compile_pipeline(LoadedPipeline(pipeline=pipeline, file_path=EXAMPLE_PIPELINE_DIRECTORY))


def _build_preservation_transform(replay_lineage_mode: ReplayLineageMode) -> TransformStep:
    return TransformStep(
        name="orders_enriched",
        source="orders",
        engine="MergeTree()",
        order_by=["order_id"],
        query=(
            "SELECT CAST(order_id AS String) AS order_id, "
            + PRESERVATION_PROJECTION_BY_MODE[replay_lineage_mode]
            + ' FROM __ref("orders")'
        ),
        replay_anchor=ReplayAnchorMode.NEVER,
    )


PRESERVATION_PIPELINE_BUILDERS: dict[str, Callable[[ReplayLineageMode], CompiledPipeline]] = {
    "managed": _build_managed_preservation_compiled_pipeline,
    "adopted": _build_adopted_preservation_compiled_pipeline,
}
PRESERVATION_EXTERNAL_SOURCE_KIND_BY_MODE: dict[ReplayLineageMode, SourceKind] = {
    ReplayLineageMode.OFFSETS: SourceKind.KAFKA,
    ReplayLineageMode.TIMESTAMP: SourceKind.KAFKA,
    ReplayLineageMode.CURSOR: SourceKind.STREAM_TABLE,
}
PRESERVATION_BOUNDARY_COLUMNS_BY_MODE: dict[ReplayLineageMode, ReplayBoundaryColumns] = {
    ReplayLineageMode.OFFSETS: ReplayBoundaryColumns(
        partition="event_partition",
        offset="event_offset",
        timestamp="event_timestamp",
    ),
    ReplayLineageMode.TIMESTAMP: ReplayBoundaryColumns(timestamp="event_timestamp"),
    ReplayLineageMode.CURSOR: ReplayBoundaryColumns(
        timestamp="event_timestamp",
        cursor="event_cursor",
    ),
}
PRESERVATION_PROJECTION_BY_MODE: dict[ReplayLineageMode, str] = {
    ReplayLineageMode.OFFSETS: (
        "CAST(_replay_partition AS Int32) AS _replay_partition, "
        "CAST(_replay_offset AS Int64) AS _replay_offset"
    ),
    ReplayLineageMode.TIMESTAMP: ("CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp"),
    ReplayLineageMode.LANDED_AT: ("CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at"),
    ReplayLineageMode.CURSOR: "CAST(_replay_cursor AS UInt64) AS _replay_cursor",
}


def build_mutable_ref_desired_state() -> DesiredState:
    return build_single_transform_desired_state(
        query=(
            "SELECT CAST(order_id AS UInt64) AS order_id, "
            "CAST(_replay_partition AS UInt64) AS _replay_partition, "
            "CAST(_replay_offset AS UInt64) AS _replay_offset "
            'FROM __ref("orders") LEFT JOIN '
            '__ref("customers", ref_type="mutable") USING customer_id'
        ),
        supporting_transforms=(
            (
                "customers",
                'SELECT CAST(customer_id AS UInt64) AS customer_id FROM __ref("orders")',
                ("customer_id",),
            ),
        ),
    )


DIRECT_SCOPE_MODEL_SQL_BY_NAME: dict[str, str] = {
    "alpha": 'SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    "beta": 'SELECT order_id::UInt64 AS order_id FROM __ref("alpha")',
    "gamma": 'SELECT order_id::UInt64 AS order_id FROM __ref("beta")',
    "delta": (
        'SELECT a.order_id::UInt64 AS order_id FROM __ref("alpha") AS a '
        'LEFT JOIN __ref("gamma", ref_type="reference") AS g ON a.order_id = g.order_id'
    ),
}
DIRECT_SCOPE_MODEL_NAMES: tuple[str, ...] = ("alpha", "beta", "gamma", "delta")
DIRECT_SCOPE_SOURCE_RELATION_NAMES: tuple[str, ...] = (
    "kafka__orders",
    "raw__orders",
    "mv__orders",
)
_DIRECT_SCOPE_SETTINGS_BLOCK_BY_FLAG: dict[bool | None, str] = {
    None: "",
    True: '\n[defaults]\npipeline_mode = "virtual"\n',
    False: '\n[defaults]\npipeline_mode = "direct"\n',
}
_DIRECT_SCOPE_SOURCE_YML: str = (
    "sources:\n"
    "  - name: orders\n"
    "    kind: kafka\n"
    "    broker_list: kafka:9092\n"
    "    topic: source.orders\n"
    "    replay_boundary: {mode: offsets}\n"
)


def write_direct_scope_project(
    *, project_root: Path, virtual_environments: bool | None = None
) -> None:
    """Write the alpha/beta/gamma/delta scope project used by direct planning tests."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_scope"\n'
        'default_target = "test"\n'
        f"{_DIRECT_SCOPE_SETTINGS_BLOCK_BY_FLAG[virtual_environments]}"
        "\n[targets.test]\n"
        'database = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "orders.yml").write_text(_DIRECT_SCOPE_SOURCE_YML, encoding="utf-8")
    model_name: str
    model_sql: str
    for model_name, model_sql in DIRECT_SCOPE_MODEL_SQL_BY_NAME.items():
        (pipeline_root / f"{model_name}.sql").write_text(
            f'MODEL (order_by ["order_id"]);\n{model_sql}\n',
            encoding="utf-8",
        )


def write_direct_mutable_scope_project(*, project_root: Path) -> None:
    """Write the direct scope with delta using a mutable side reference."""

    write_direct_scope_project(project_root=project_root)
    delta_path: Path = project_root / "pipelines" / "pl__orders" / "delta.sql"
    delta_path.write_text(
        delta_path.read_text(encoding="utf-8").replace(
            'ref_type="reference"', 'ref_type="mutable"'
        ),
        encoding="utf-8",
    )


def write_direct_view_only_project(*, project_root: Path) -> None:
    """Write one source-less direct project containing an exact-named terminal view."""

    pipeline_root: Path = project_root / "pipelines" / "pl__consumer"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    _write_direct_project_config(project_root=project_root)
    (pipeline_root / "customer_orders.sql").write_text(
        "MODEL (kind view, relation_name customer_orders);\nSELECT 1::UInt64 AS order_id\n",
        encoding="utf-8",
    )


def write_direct_multi_upstream_view_project(*, project_root: Path) -> None:
    """Write a custom-named table and terminal view with source and model dependencies."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    _write_direct_project_config(project_root=project_root)
    (source_root / "orders.yml").write_text(_DIRECT_SCOPE_SOURCE_YML, encoding="utf-8")
    (pipeline_root / "orders_base.sql").write_text(
        "MODEL (relation_name orders_rollup, order_by [order_id]);\n"
        'SELECT kafka_value::UInt64 AS order_id FROM __source("orders")\n',
        encoding="utf-8",
    )
    (pipeline_root / "customer_orders.sql").write_text(
        "MODEL (kind view, relation_name customer_orders);\n"
        "SELECT base.order_id::UInt64 AS order_id\n"
        'FROM __ref("orders_base") AS base\n'
        'INNER JOIN __source("orders") AS raw\n'
        "ON base.order_id = raw.kafka_value::UInt64\n",
        encoding="utf-8",
    )


def _write_direct_project_config(*, project_root: Path) -> None:
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_view"\ndefault_target = "test"\n\n[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )


def analyze_direct_scope_project(*, project_root: Path) -> CompileAnalysis:
    """Analyze a written scope project exactly as the CLI does."""

    return analyze_project(
        pipelines_root=project_root / "pipelines",
        loaded_project=_native.load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )


def direct_model_keys(
    *, analysis: CompileAnalysis, names: tuple[str, ...]
) -> frozenset[LogicalResourceKey]:
    """Return the logical model keys for the named models."""

    key_by_name: dict[str, LogicalResourceKey] = {
        key.name: key for key in analysis.graph.ordered_keys
    }
    return frozenset(key_by_name[name] for name in names)


def build_direct_snapshot(
    *,
    relation_names: tuple[str, ...] = (),
    stable_binding_names: tuple[str, ...] = (),
) -> DirectWarehouseSnapshot:
    """Build one immutable direct snapshot from explicit catalog and deployment facts."""

    stable_binding_by_name: dict[str, str | None] = {
        relation_name: f"{relation_name}__binding" for relation_name in stable_binding_names
    }
    return DirectWarehouseSnapshot(
        catalog=CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=AdapterIdentity(name="clickhouse"),
                database="analytics",
            ),
            warehouse_timezone="UTC",
            relations=tuple(
                CatalogRelation(
                    name=relation_name,
                    engine="MergeTree",
                    columns=(
                        CatalogColumn(name="order_id", type="UInt64"),
                        CatalogColumn(name=REPLAY_PARTITION_COLUMN_NAME, type="Int64"),
                        CatalogColumn(name=REPLAY_OFFSET_COLUMN_NAME, type="Int64"),
                        CatalogColumn(name=REPLAY_TIMESTAMP_COLUMN_NAME, type="DateTime64(3)"),
                        CatalogColumn(name=REPLAY_LANDED_AT_COLUMN_NAME, type="DateTime64(3)"),
                        CatalogColumn(name=REPLAY_CURSOR_COLUMN_NAME, type="String"),
                    ),
                    stable_binding_name=stable_binding_by_name.get(relation_name),
                )
                for relation_name in relation_names
            ),
        ),
    )


def plan_direct_scope(
    *,
    analysis: CompileAnalysis,
    snapshot: DirectWarehouseSnapshot,
    selected_model_names: tuple[str, ...],
    selection_mode: DirectSelectionMode | None = None,
    include_missing_upstream: bool = False,
) -> DirectPlan:
    """Plan the direct closure of the scope project for the named selection."""

    return plan_direct_build(
        graph=analysis.graph,
        realized_project=analysis.realized_project,
        snapshot=snapshot,
        database="analytics",
        selected_model_keys=direct_model_keys(analysis=analysis, names=selected_model_names),
        selection_mode=selection_mode,
        include_missing_upstream=include_missing_upstream,
    )


def logical_key_names(keys: tuple[LogicalResourceKey, ...]) -> tuple[str, ...]:
    """Return the names of logical resource keys in their original order."""

    return tuple(key.name for key in keys)


def replay_root_summaries(*, plan: DirectPlan) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Summarize each replay root as model, driving relation, and propagated models."""

    return tuple(
        (
            root.model_key.name,
            root.driving_input_relation_name,
            logical_key_names(root.propagated_model_keys),
        )
        for root in plan.replay_roots
    )


def relation_operation_summaries(
    *, operations: tuple[DirectRelationOperation, ...]
) -> tuple[tuple[str, str], ...]:
    """Summarize relation operations as ordered action and relation-name pairs."""

    return tuple((str(operation.action), operation.relation_name) for operation in operations)


def build_settled_direct_snapshot() -> DirectWarehouseSnapshot:
    """Build the snapshot of a warehouse that direct mode already built end to end."""

    model_relation_names: tuple[str, ...] = direct_scope_relation_names(
        model_names=DIRECT_SCOPE_MODEL_NAMES
    )
    return build_direct_snapshot(
        relation_names=(*DIRECT_SCOPE_SOURCE_RELATION_NAMES, *model_relation_names),
    )


def build_direct_fingerprint_snapshot(
    *, analysis: CompileAnalysis, changed_model_names: tuple[str, ...] = ()
) -> AdapterDirectFingerprintSnapshot:
    """Build complete applied fingerprints with optional deterministic query drift."""

    changed_names: frozenset[str] = frozenset(changed_model_names)
    return AdapterDirectFingerprintSnapshot(
        status="available",
        baselines=tuple(
            AdapterDirectFingerprintRecord(
                fingerprint_id=f"{model.key.name}-baseline",
                logical_model_identity=f"analytics.{model.key.name}",
                definition_sql=model.query,
                definition_hash={
                    True: "outdated",
                    False: DirectModelFingerprint.query_hash(model.query),
                }[model.key.name in changed_names],
                identity_metadata=json.dumps(
                    DirectModelFingerprint.identity(
                        model=model,
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                workflow_id="previous",
                tool_version="0.32.0",
            )
            for model in analysis.compiled_project.models
        ),
    )


def without_catalog_columns(
    *,
    snapshot: DirectWarehouseSnapshot,
    relation_name: str,
    column_names: tuple[str, ...],
) -> DirectWarehouseSnapshot:
    """Return a snapshot with selected columns removed from one relation."""

    relation_by_name: dict[str, CatalogRelation] = {
        relation.name: relation for relation in snapshot.catalog.relations
    }
    target: CatalogRelation = relation_by_name[relation_name]
    retained_column_by_name: dict[str, CatalogColumn] = {
        column.name: column for column in target.columns
    }
    for column_name in column_names:
        retained_column_by_name.pop(column_name)
    relation_by_name[relation_name] = replace(
        target,
        columns=tuple(retained_column_by_name.values()),
    )
    relations: tuple[CatalogRelation, ...] = tuple(
        relation_by_name[relation.name] for relation in snapshot.catalog.relations
    )
    return replace(snapshot, catalog=replace(snapshot.catalog, relations=relations))


def direct_scope_relation_names(*, model_names: tuple[str, ...]) -> tuple[str, ...]:
    """Return the table and view relation names declared by the named models."""

    return tuple(
        chain.from_iterable(
            (f"tbl__{model_name}", f"mv__{model_name}") for model_name in model_names
        )
    )


def build_key(database: str | None, object_type: str, name: str) -> ObjectKey:
    return ObjectKey(database=database, object_type=DesiredObjectType(object_type), name=name)


def build_example_desired_state_with_replay_on_change(
    *, replay_on_change: ReplayOnChangePolicy | None
) -> DesiredState:
    """Build the example desired state with replay-on-change on every transform."""

    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(EXAMPLE_PIPELINE_DIRECTORY)
    pipeline_with_policy: Pipeline = replace(
        loaded_pipeline.pipeline,
        transforms=[
            replace(transform_step, replay_on_change=replay_on_change)
            for transform_step in loaded_pipeline.pipeline.transforms
        ],
    )
    return realize_compiled_pipelines(
        (compile_pipeline(replace(loaded_pipeline, pipeline=pipeline_with_policy)),)
    ).desired_state


def build_example_actual_state() -> ActualState:
    desired_state: DesiredState = build_example_desired_state()
    kafka_table: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView = (
        desired_state.objects[0]
    )
    landing_mv: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView = (
        desired_state.objects[1]
    )
    raw_table: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView = (
        desired_state.objects[3]
    )
    assert isinstance(kafka_table, DesiredKafkaTable)
    assert isinstance(landing_mv, DesiredMaterializedView)
    assert isinstance(raw_table, DesiredTable)

    return ActualState(
        objects=(
            ActualKafkaTable(
                key=kafka_table.key,
                spec=KafkaTableSpec(
                    columns=kafka_table.spec.columns,
                    kafka=kafka_table.spec.kafka,
                ),
            ),
            ActualMaterializedView(
                key=landing_mv.key,
                spec=MaterializedViewSpec(
                    source_table_name=landing_mv.spec.source_table_name,
                    target_table_name=landing_mv.spec.target_table_name,
                    query=landing_mv.spec.query,
                ),
            ),
            ActualTable(
                key=raw_table.key,
                spec=TableSpec(
                    columns=raw_table.spec.columns,
                    storage=TableStorage(
                        engine=raw_table.spec.storage.engine,
                        order_by=raw_table.spec.storage.order_by,
                        partition_by=raw_table.spec.storage.partition_by,
                        ttl=raw_table.spec.storage.ttl,
                        settings={"index_granularity": "4096"},
                    ),
                ),
            ),
        )
    )


def build_actual_state_matching_desired(desired_state: DesiredState) -> ActualState:
    actual_object_builders: dict[type[object], Callable[..., object]] = {
        DesiredKafkaTable: ActualKafkaTable,
        DesiredMaterializedView: ActualMaterializedView,
        DesiredTable: ActualTable,
    }
    actual_objects: tuple[ActualKafkaTable | ActualMaterializedView | ActualTable, ...] = tuple(
        cast(
            ActualKafkaTable | ActualMaterializedView | ActualTable,
            actual_object_builders[type(desired_object)](
                key=desired_object.key,
                spec=desired_object.spec,
            ),
        )
        for desired_object in desired_state.objects
    )
    return ActualState(objects=actual_objects)


def build_projection_characterization_inputs() -> tuple[DesiredState, PlanningWarehouseSnapshot]:
    kafka_key: ObjectKey = ObjectKey(database=None, object_type="kafka_table", name="kafka__orders")
    raw_key: ObjectKey = ObjectKey(database=None, object_type="table", name="raw__orders")
    landing_mv_key: ObjectKey = ObjectKey(
        database=None,
        object_type="materialized_view",
        name="mv__orders_landing",
    )
    transform_key: ObjectKey = ObjectKey(
        database=None, object_type="table", name="tbl__orders_enriched"
    )
    raw_storage: TableStorage = TableStorage(
        engine="MergeTree()",
        order_by=("order_id",),
        partition_by="toYYYYMM(created_at)",
        ttl="created_at + INTERVAL 30 DAY",
        settings={"index_granularity": "8192"},
    )
    desired_state: DesiredState = DesiredState(
        objects=(
            DesiredKafkaTable(
                key=kafka_key,
                deps=(),
                spec=KafkaTableSpec(
                    columns=(Column(name="message", type="String"),),
                    kafka=CompiledKafkaSettings(
                        broker_list="kafka:9092",
                        topic="source.orders.created",
                        consumer_group="sqltide_orders_orders",
                        format="JSONAsString",
                        settings={"kafka_num_consumers": "4"},
                    ),
                ),
            ),
            DesiredTable(
                key=raw_key,
                deps=(kafka_key,),
                spec=TableSpec(
                    columns=(Column(name="order_id", type="String"),),
                    storage=raw_storage,
                ),
            ),
            DesiredMaterializedView(
                key=landing_mv_key,
                deps=(kafka_key, raw_key),
                spec=MaterializedViewSpec(
                    source_table_name="kafka__orders",
                    target_table_name="raw__orders",
                    query="SELECT message AS order_id FROM kafka__orders",
                ),
            ),
            DesiredTable(
                key=transform_key,
                deps=(raw_key,),
                spec=TableSpec(
                    columns=(Column(name="order_id", type="String"),),
                    storage=raw_storage,
                ),
            ),
        ),
        replay_anchor_keys=frozenset({raw_key}),
        mutable_ref_warning_keys=frozenset(),
    )
    physical_name: str = "tbl__orders_enriched__20260131T120000Z_ab12"
    snapshot: PlanningWarehouseSnapshot = PlanningWarehouseSnapshot(
        catalog=CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=AdapterIdentity(name="clickhouse"), database="analytics"
            ),
            warehouse_timezone="UTC",
            relations=(
                CatalogRelation(name=kafka_key.name, engine="Kafka", columns=()),
                CatalogRelation(name=raw_key.name, engine="MergeTree", columns=()),
                CatalogRelation(
                    name=landing_mv_key.name,
                    engine="MaterializedView",
                    columns=(),
                    query_sql="SELECT 1",
                ),
                CatalogRelation(
                    name=transform_key.name,
                    engine="View",
                    columns=(),
                    query_sql=f"SELECT * FROM {physical_name}",
                    stable_binding_name=physical_name,
                ),
                CatalogRelation(
                    name=physical_name,
                    engine="MergeTree",
                    columns=(CatalogColumn(name="order_id", type="String"),),
                    order_by=("order_id",),
                    ttl="created_at + INTERVAL 30 DAY",
                    settings=(("index_granularity", "8192"),),
                ),
            ),
        ),
        object_state_records=(),
    )
    return desired_state, snapshot


KeyParts: type = tuple[str | None, str, str]


def key_parts(key: ObjectKey) -> KeyParts:
    """Return a key as a comparable tuple."""

    return (key.database, key.object_type, key.name)


def optional_key_parts(key: ObjectKey | None) -> KeyParts | None:
    """Return a key as a comparable tuple, preserving an absent key as None."""

    resolvers: dict[type[object], Callable[[], tuple[str | None, str, str] | None]] = {
        type(None): lambda: None,
        ObjectKey: lambda: key_parts(cast(ObjectKey, key)),
    }
    return resolvers[type(key)]()


def planned_change(
    *, desired_object: DesiredObject, actual_object: ActualObject
) -> PlannedObjectChange:
    """Plan one desired object against one actual object and return its classification."""

    return plan_deployment(
        desired_state=DesiredState(
            objects=(desired_object,),
            replay_anchor_keys=frozenset(),
            mutable_ref_warning_keys=frozenset(),
        ),
        actual_state=ActualState(objects=(actual_object,)),
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    ).object_changes[0]
