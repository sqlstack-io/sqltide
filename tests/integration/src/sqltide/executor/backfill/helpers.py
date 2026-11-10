from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import chain
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import (
    SqlModelAnalyzer,
    assemble_virtual_build_workflow,
    build_root_backfill_reports,
    build_virtual_bootstrap_workflow,
    build_virtual_execution_result,
    load_planning_warehouse_snapshot,
    plan_deployment,
    realize_project,
    resolve_unsupported_bounded_replay_behavior,
)
from sqltide._native import compile_pipeline as compile_pipeline_impl
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterConnectionConfig,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterTable,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.cli.workflow_artifacts.main._publish_build_workflow import publish_build_workflow
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompilerAdapterProfile,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaSettings,
    LoadedPipeline,
    Pipeline,
    ReplayBoundary,
    ReplayBoundaryColumns,
    TransformStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    ReplayAnchorMode,
    ReplayBoundaryMode,
    ReplayLineageMode,
    SourceKind,
)
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.pipeline.types import AdapterResource
from sqltide.compiler.planner.constants import REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED
from sqltide.compiler.planner.models import (
    ActualState,
    DeploymentPlan,
    PlanningWarehouseSnapshot,
)
from sqltide.compiler.planner.types import RebuildExecutionMode
from sqltide.executor.backfill.models import (
    BackfillBootstrapRequest,
    BackfillBootstrapResult,
    BackfillExecutionResult,
    RootBackfillReport,
)
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest
from sqltide.executor.workflow.main.execute_build_workflow import execute_build_workflow
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    PublishedBuildWorkflow,
    WorkflowExecutionResult,
)
from sqltide.executor.workflow.types import WorkflowEventEmitter
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    build_compiled_example_pipeline,
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill._test_types import (
    BoundedPreservationMatrixScenarioResult,
    ExecuteBoundedPreservationMatrixIntegrationTestCase,
    ExecuteStartTimeReplayIntegrationTestCase,
    ManagedSourceResources,
    ModelResources,
    StartTimeReplayScenarioResult,
)
from tests.unit.src.sqltide.compiler.compile.helpers import build_realization_analyzer
from tests.unit.src.sqltide.compiler.planner.helpers import EXAMPLE_PIPELINE_DIRECTORY

KAFKA_TOPIC_PROJECTION: dict[bool, str] = {
    True: "CAST(kafka_topic AS String) AS kafka_topic, ",
    False: "",
}
SCALAR_REPLAY_PROJECTION: dict[ReplayLineageMode, str] = {
    ReplayLineageMode.TIMESTAMP: "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp ",
    ReplayLineageMode.LANDED_AT: "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at ",
}
LANDED_AT_REPLAY_PROJECTION: str = SCALAR_REPLAY_PROJECTION[ReplayLineageMode.LANDED_AT]


def execute_backfill(
    *,
    request: BackfillBootstrapRequest,
    client: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
) -> BackfillExecutionResult:
    """Execute legacy preservation scenarios through published workflow authority."""

    confirmed_request: BackfillBootstrapRequest
    snapshot: PlanningWarehouseSnapshot
    root_reports: tuple[RootBackfillReport, ...]
    confirmed_request, snapshot, root_reports = _prepare_backfill_request(
        request=request,
        client=client,
    )
    workflow: BuildWorkflow = assemble_virtual_build_workflow(
        request=confirmed_request,
        client=client,
        plan_json="{}",
    )
    execution: WorkflowExecutionResult = _execute_test_workflow(
        client=client,
        workflow=workflow,
        emitter=emitter,
    )
    return build_virtual_execution_result(
        request=confirmed_request,
        root_reports=root_reports,
        existing_relation_names=snapshot.catalog.relation_names(),
        execution=execution,
    )


def execute_backfill_bootstrap(
    *, request: BackfillBootstrapRequest, client: AdapterConnection
) -> BackfillBootstrapResult:
    """Execute only candidate bootstrap statements through published authority."""

    confirmed_request: BackfillBootstrapRequest
    snapshot: PlanningWarehouseSnapshot
    root_reports: tuple[RootBackfillReport, ...]
    confirmed_request, snapshot, root_reports = _prepare_backfill_request(
        request=request,
        client=client,
    )
    workflow: BuildWorkflow = assemble_virtual_build_workflow(
        request=confirmed_request,
        client=client,
        plan_json="{}",
    )
    bootstrap_workflow: BuildWorkflow = build_virtual_bootstrap_workflow(workflow=workflow)
    _ = _execute_test_workflow(client=client, workflow=bootstrap_workflow)
    return BackfillBootstrapResult(
        deployment_id=cast(str, confirmed_request.deployment_id),
        created_at=cast(str, confirmed_request.created_at),
        deployment_plan=cast(DeploymentPlan, confirmed_request.confirmed_plan),
        root_reports=root_reports,
        existing_relation_names=snapshot.catalog.relation_names(),
    )


def _prepare_backfill_request(
    *, request: BackfillBootstrapRequest, client: AdapterConnection
) -> tuple[
    BackfillBootstrapRequest,
    PlanningWarehouseSnapshot,
    tuple[RootBackfillReport, ...],
]:
    snapshot: PlanningWarehouseSnapshot = load_planning_warehouse_snapshot(
        client=client,
        database=request.default_database,
    )
    actual_state: ActualState = _native.load_actual_state_from_snapshot(
        snapshot=snapshot,
        desired_state=request.desired_state,
        database=request.default_database,
    )
    deployment_plan: DeploymentPlan = request.confirmed_plan or plan_deployment(
        desired_state=request.desired_state,
        actual_state=actual_state,
        default_database=request.default_database,
        render_resource=client.render_resource,
        deployment_id=request.deployment_id,
        full_refresh_keys=request.full_refresh_keys,
        start_time_keys=request.start_time_keys,
        start_time=request.start_time,
    )
    deployment_plan = resolve_unsupported_bounded_replay_behavior(
        catalog=snapshot.catalog,
        deployment_plan=deployment_plan,
        desired_state=request.desired_state,
        default_database=request.default_database,
        replay_lineage_mode=ReplayLineageMode(request.replay_lineage_mode),
    )
    confirmed_request: BackfillBootstrapRequest = replace(
        request,
        confirmed_plan=deployment_plan,
        deployment_id=deployment_plan.deployment_id,
        confirmed_target_catalog=snapshot.catalog,
        confirmed_metadata_catalog=snapshot.catalog,
    )
    root_reports: tuple[RootBackfillReport, ...] = build_root_backfill_reports(
        catalog=snapshot.catalog,
        desired_state=request.desired_state,
    )
    return confirmed_request, snapshot, root_reports


def _execute_test_workflow(
    *,
    client: AdapterConnection,
    workflow: BuildWorkflow,
    emitter: WorkflowEventEmitter | None = None,
) -> WorkflowExecutionResult:
    with TemporaryDirectory() as temporary_directory:
        published: PublishedBuildWorkflow = publish_build_workflow(
            target_dir=Path(temporary_directory),
            workflow=workflow,
        )
        return execute_build_workflow(
            published_workflow=published,
            connection=client,
            emitter=emitter,
        )


def compile_pipeline(loaded_pipeline: LoadedPipeline) -> CompiledPipeline:
    return compile_pipeline_impl(
        loaded_pipeline=loaded_pipeline,
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )


def build_managed_replay_boundary(
    replay_lineage_mode: ReplayLineageMode | str,
) -> ReplayBoundary:
    return ReplayBoundary(
        mode=ReplayBoundaryMode(replay_lineage_mode),
        columns=ReplayBoundaryColumns(),
    )


def build_backfill_bootstrap_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
) -> BackfillBootstrapRequest:
    desired_state: DesiredState = build_desired_state(
        (compile_pipeline(_native.load_pipeline_directory(EXAMPLE_PIPELINE_DIRECTORY)),)
    )
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
    )


def build_compiled_pipeline() -> CompiledPipeline:
    return build_compiled_example_pipeline()


def build_scalar_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
    replay_lineage_mode: ReplayLineageMode | str,
) -> BackfillBootstrapRequest:
    resolved_replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(replay_lineage_mode)
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline(
        resolved_replay_lineage_mode
    )
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=resolved_replay_lineage_mode,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_scalar_replay_compiled_pipeline(
    replay_lineage_mode: ReplayLineageMode | str,
) -> CompiledPipeline:
    return build_named_scalar_replay_compiled_pipeline(
        replay_lineage_mode=ReplayLineageMode(replay_lineage_mode),
        pipeline_name="orders_pipeline",
        source_name="orders",
        transform_name="orders_enriched",
        topic="source.orders.created",
        include_kafka_topic=False,
    )


def build_named_scalar_replay_compiled_pipeline(
    *,
    replay_lineage_mode: ReplayLineageMode | str,
    pipeline_name: str,
    source_name: str,
    transform_name: str,
    topic: str,
    include_kafka_topic: bool = False,
) -> CompiledPipeline:
    resolved_replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(replay_lineage_mode)
    query: str = (
        "SELECT CAST(kafka_key AS String) AS order_id, "
        + KAFKA_TOPIC_PROJECTION[include_kafka_topic]
        + SCALAR_REPLAY_PROJECTION.get(resolved_replay_lineage_mode, LANDED_AT_REPLAY_PROJECTION)
        + f'FROM __ref("{source_name}")'
    )

    pipeline: Pipeline = Pipeline(
        name=pipeline_name,
        source=KafkaLandingStep(
            name=source_name,
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic=topic,
            ),
            replay_boundary=build_managed_replay_boundary(resolved_replay_lineage_mode),
        ),
        transforms=[
            TransformStep(
                name=transform_name,
                source=source_name,
                engine="MergeTree()",
                order_by=["order_id"],
                query=query,
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_offset_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_reference_join_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    desired_state: DesiredState = build_desired_state((build_reference_join_compiled_pipeline(),))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_reference_join_region_lookup_only_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    desired_state: DesiredState = build_desired_state(
        (build_reference_join_region_lookup_only_compiled_pipeline(),)
    )
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_external_source_offset_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    compiled_pipeline: CompiledPipeline = build_external_source_offset_replay_compiled_pipeline()
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_external_source_cursor_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    start_time: str | None = None,
) -> BackfillBootstrapRequest:
    compiled_pipeline: CompiledPipeline = build_external_source_cursor_replay_compiled_pipeline()
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    start_time_keys_by_presence: dict[bool, frozenset[ObjectKey]] = {
        True: frozenset({require_model_resources(compiled_pipeline).target_table.key}),
        False: frozenset(),
    }
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.CURSOR,
        deployment_id=deployment_id,
        start_time=start_time,
        start_time_keys=start_time_keys_by_presence[start_time is not None],
        created_at=created_at,
        stabilization_seconds=0.0,
    )


def build_aggregate_offset_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    compiled_pipeline: CompiledPipeline = build_aggregate_offset_replay_compiled_pipeline()
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_external_source_aggregate_offset_replay_request(
    *,
    database: str,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    compiled_pipeline: CompiledPipeline = (
        build_external_source_aggregate_offset_replay_compiled_pipeline()
    )
    desired_state: DesiredState = build_desired_state((compiled_pipeline,))
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def build_offset_replay_compiled_pipeline() -> CompiledPipeline:
    return build_named_offset_replay_compiled_pipeline(include_kafka_topic=False)


def build_reference_join_compiled_pipeline() -> CompiledPipeline:
    return _build_reference_join_compiled_pipeline(include_enriched_orders=True)


def build_reference_join_region_lookup_only_compiled_pipeline() -> CompiledPipeline:
    return _build_reference_join_compiled_pipeline(include_enriched_orders=False)


def _build_reference_join_compiled_pipeline(*, include_enriched_orders: bool) -> CompiledPipeline:
    region_lookup: TransformStep = TransformStep(
        name="region_lookup",
        source="orders",
        engine="MergeTree()",
        order_by=["region"],
        query=(
            "SELECT CAST(kafka_key AS String) AS region, "
            "CAST(upper(kafka_key) AS String) AS region_display, "
            "CAST(_replay_partition AS Int64) AS _replay_partition, "
            "CAST(_replay_offset AS Int64) AS _replay_offset "
            'FROM __ref("orders")'
        ),
        replay_anchor=ReplayAnchorMode.NEVER,
    )
    enriched_orders: TransformStep = TransformStep(
        name="enriched_orders",
        source="orders",
        engine="MergeTree()",
        order_by=["order_id", "_replay_partition", "_replay_offset"],
        query=(
            "SELECT CAST(o.kafka_key AS String) AS order_id, "
            "CAST(r.region_display AS String) AS region_display, "
            "CAST(o._replay_partition AS Int64) AS _replay_partition, "
            "CAST(o._replay_offset AS Int64) AS _replay_offset "
            'FROM __ref("orders") AS o '
            'LEFT JOIN __ref("region_lookup", ref_type="reference") AS r '
            "ON CAST(o.kafka_key AS String) = r.region"
        ),
        replay_anchor=ReplayAnchorMode.NEVER,
    )
    optional_transforms: dict[bool, tuple[TransformStep, ...]] = {
        True: (enriched_orders,),
        False: (),
    }
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
            ),
            replay_boundary=build_managed_replay_boundary(ReplayLineageMode.OFFSETS),
        ),
        transforms=[region_lookup, *optional_transforms[include_enriched_orders]],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_external_source_offset_replay_compiled_pipeline() -> CompiledPipeline:
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
                    landed_at="event_landed_at",
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
                    "SELECT CAST(order_id AS String) AS order_id, "
                    "CAST(event_partition AS Int64) AS _replay_partition, "
                    "CAST(event_offset AS Int64) AS _replay_offset "
                    'FROM __ref("orders")'
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_external_source_cursor_replay_compiled_pipeline() -> CompiledPipeline:
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=ExternalTableSourceStep(
            name="orders",
            kind=SourceKind.STREAM_TABLE,
            table_name="orders_existing",
            replay_boundary=ReplayBoundary(
                mode=ReplayBoundaryMode.CURSOR,
                columns=ReplayBoundaryColumns(
                    cursor="event_cursor",
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
                    "SELECT CAST(order_id AS String) AS order_id, "
                    "CAST(_replay_cursor AS UInt64) AS _replay_cursor "
                    'FROM __ref("orders")'
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_aggregate_offset_replay_compiled_pipeline() -> CompiledPipeline:
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
            ),
            replay_boundary=build_managed_replay_boundary(ReplayLineageMode.OFFSETS),
        ),
        transforms=[
            TransformStep(
                name="hourly_order_volume",
                source="orders",
                engine="MergeTree()",
                order_by=["event_hour"],
                query=(
                    "SELECT CAST(toStartOfHour(_replay_timestamp) AS DateTime64(3)) "
                    "AS event_hour, "
                    "CAST(count() AS UInt64) AS order_event_count "
                    'FROM __ref("orders") AS item_rows '
                    "GROUP BY event_hour"
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_external_source_aggregate_offset_replay_compiled_pipeline() -> CompiledPipeline:
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
                    landed_at="event_landed_at",
                ),
            ),
        ),
        transforms=[
            TransformStep(
                name="hourly_order_volume",
                source="orders",
                engine="MergeTree()",
                order_by=["event_hour"],
                query=(
                    "SELECT CAST(toStartOfHour(event_timestamp) AS DateTime64(3)) "
                    "AS event_hour, "
                    "CAST(count() AS UInt64) AS order_event_count "
                    'FROM __ref("orders") AS item_rows '
                    "GROUP BY event_hour"
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_changed_aggregate_offset_replay_compiled_pipeline(
    *,
    bounded_replay_fallback: BoundedReplayFallback | str = BoundedReplayFallback.FULL,
) -> CompiledPipeline:
    resolved_bounded_replay_fallback: BoundedReplayFallback = BoundedReplayFallback(
        bounded_replay_fallback
    )
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
            ),
            replay_boundary=build_managed_replay_boundary(ReplayLineageMode.OFFSETS),
        ),
        transforms=[
            TransformStep(
                name="hourly_order_volume",
                source="orders",
                engine="MergeTree()",
                order_by=["event_hour"],
                query=(
                    "SELECT CAST(toStartOfHour(_replay_timestamp) AS DateTime64(3)) "
                    "AS event_hour, "
                    "CAST(count() AS UInt64) AS order_event_count, "
                    "CAST('changed' AS String) AS replay_marker "
                    'FROM __ref("orders") AS item_rows '
                    "GROUP BY event_hour"
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
                bounded_replay_fallback=resolved_bounded_replay_fallback,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_named_offset_replay_compiled_pipeline(
    *, include_kafka_topic: bool = False
) -> CompiledPipeline:
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
            ),
            replay_boundary=build_managed_replay_boundary(ReplayLineageMode.OFFSETS),
        ),
        transforms=[
            TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=["order_id"],
                query=(
                    "SELECT CAST(kafka_key AS String) AS order_id, "
                    + KAFKA_TOPIC_PROJECTION[include_kafka_topic]
                    + "CAST(_replay_partition AS Int64) AS _replay_partition, "
                    "CAST(_replay_offset AS Int64) AS _replay_offset "
                    'FROM __ref("orders")'
                ),
                replay_anchor=ReplayAnchorMode.NEVER,
            )
        ],
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def build_changed_scalar_replay_compiled_pipeline(
    replay_lineage_mode: ReplayLineageMode | str,
) -> CompiledPipeline:
    return build_named_scalar_replay_compiled_pipeline(
        replay_lineage_mode=ReplayLineageMode(replay_lineage_mode),
        pipeline_name="orders_pipeline",
        source_name="orders",
        transform_name="orders_enriched",
        topic="source.orders.created",
        include_kafka_topic=True,
    )


def build_changed_offset_replay_compiled_pipeline() -> CompiledPipeline:
    return build_named_offset_replay_compiled_pipeline(include_kafka_topic=True)


def build_raw_orders_row(
    *,
    kafka_key: str,
    _replay_partition: int,
    _replay_offset: int,
    _replay_timestamp: str,
    _replay_landed_at: str,
) -> tuple[object, ...]:
    return (
        kafka_key,
        "{}",
        "source.orders.created",
        _replay_partition,
        _replay_offset,
        _replay_timestamp,
        [],
        [],
        _replay_landed_at,
    )


def build_external_source_orders_row(
    *,
    order_id: str,
    event_partition: int,
    event_offset: int,
    event_timestamp: str,
    event_landed_at: str,
) -> tuple[object, ...]:
    return (
        order_id,
        event_partition,
        event_offset,
        event_timestamp,
        event_landed_at,
    )


def build_external_source_cursor_orders_row(
    *,
    order_id: str,
    event_cursor: int,
    event_timestamp: str,
) -> tuple[object, ...]:
    return (order_id, event_cursor, event_timestamp)


def run_bounded_preservation_matrix_scenario(
    *,
    test_case: ExecuteBoundedPreservationMatrixIntegrationTestCase,
    connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> BoundedPreservationMatrixScenarioResult:
    initial_pipeline: CompiledPipeline = _build_matrix_compiled_pipeline(
        source_ownership=test_case.source_ownership,
        replay_lineage_mode=test_case.replay_lineage_mode,
        include_marker=False,
    )
    changed_pipeline: CompiledPipeline = _build_matrix_compiled_pipeline(
        source_ownership=test_case.source_ownership,
        replay_lineage_mode=test_case.replay_lineage_mode,
        include_marker=True,
    )
    _prepare_matrix_source(
        source_ownership=test_case.source_ownership,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=initial_pipeline,
    )
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=connection_settings.host,
            port=connection_settings.port,
            username=connection_settings.username,
            password=connection_settings.password,
            database=clickhouse_database,
        )
    )
    initial_desired_state: DesiredState = build_desired_state((initial_pipeline,))
    changed_desired_state: DesiredState = build_desired_state((changed_pipeline,))
    initial_deployment_id: str = "20260409T180000Z_matrix"
    changed_deployment_id: str = "20260409T180500Z_matrix"
    created_at: str = "2026-04-09 18:00:00.123"
    initial_boundary_time: str = "2026-04-09 18:00:00.000"
    changed_boundary_time: str = "2026-04-09 18:05:00.000"

    try:
        initial_result: BackfillExecutionResult = execute_backfill(
            request=_build_matrix_request(
                desired_state=initial_desired_state,
                database=clickhouse_database,
                replay_lineage_mode=test_case.replay_lineage_mode,
                deployment_id=initial_deployment_id,
                created_at=created_at,
                boundary_time=initial_boundary_time,
            ),
            client=connection,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        _insert_matrix_tail(
            source_ownership=test_case.source_ownership,
            clickhouse_client=clickhouse_client,
            clickhouse_database=clickhouse_database,
            compiled_pipeline=initial_pipeline,
        )
        runner: Callable[..., RebuildExecutionMode] = {
            RebuildExecutionMode.SEEDED_BOUNDED_REBUILD: _execute_seeded_matrix_replay,
            RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD: _execute_unseeded_matrix_replay,
        }[test_case.requested_execution_mode]
        execution_mode: RebuildExecutionMode = runner(
            client=connection,
            desired_state=changed_desired_state,
            database=clickhouse_database,
            replay_lineage_mode=test_case.replay_lineage_mode,
            deployment_id=changed_deployment_id,
            created_at=created_at,
            boundary_time=changed_boundary_time,
        )
    finally:
        connection.close()

    result_rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, replay_marker FROM "
        f"{clickhouse_database}.tbl__orders_enriched__{changed_deployment_id} "
        "ORDER BY order_id"
    ).result_rows
    return BoundedPreservationMatrixScenarioResult(
        execution_mode=execution_mode,
        shadow_rows=tuple((str(row[0]), str(row[1])) for row in result_rows),
    )


def _build_matrix_compiled_pipeline(
    *,
    source_ownership: str,
    replay_lineage_mode: ReplayLineageMode,
    include_marker: bool,
) -> CompiledPipeline:
    projection: str = {
        ("managed", ReplayLineageMode.OFFSETS): (
            "CAST(_replay_partition AS Int64) AS _replay_partition, "
            "CAST(_replay_offset AS Int64) AS _replay_offset"
        ),
        ("managed", ReplayLineageMode.TIMESTAMP): (
            "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp"
        ),
        ("managed", ReplayLineageMode.LANDED_AT): (
            "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at"
        ),
        ("adopted", ReplayLineageMode.OFFSETS): (
            "CAST(event_partition AS Int64) AS _replay_partition, "
            "CAST(event_offset AS Int64) AS _replay_offset"
        ),
        ("adopted", ReplayLineageMode.TIMESTAMP): (
            "CAST(event_timestamp AS DateTime64(3)) AS _replay_timestamp"
        ),
        ("adopted", ReplayLineageMode.CURSOR): ("CAST(event_cursor AS UInt64) AS _replay_cursor"),
    }[(source_ownership, replay_lineage_mode)]
    order_id_expression: str = {
        "managed": "CAST(kafka_key AS String)",
        "adopted": "CAST(order_id AS String)",
    }[source_ownership]
    marker_projection: str = {
        False: "",
        True: ", CAST('changed' AS String) AS replay_marker",
    }[include_marker]
    transform: TransformStep = TransformStep(
        name="orders_enriched",
        source="orders",
        engine="MergeTree()",
        order_by=["order_id"],
        query=(
            f"SELECT {order_id_expression} AS order_id, {projection}{marker_projection} "
            'FROM __ref("orders")'
        ),
        replay_anchor=ReplayAnchorMode.NEVER,
    )
    pipeline_builder: Callable[..., Pipeline] = {
        "managed": _build_managed_matrix_pipeline,
        "adopted": _build_adopted_matrix_pipeline,
    }[source_ownership]
    pipeline: Pipeline = pipeline_builder(
        replay_lineage_mode=replay_lineage_mode,
        transform=transform,
    )
    return compile_pipeline(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=EXAMPLE_PIPELINE_DIRECTORY,
            project=None,
        )
    )


def _build_managed_matrix_pipeline(
    *, replay_lineage_mode: ReplayLineageMode, transform: TransformStep
) -> Pipeline:
    return Pipeline(
        name="preservation_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
            ),
            replay_boundary=build_managed_replay_boundary(replay_lineage_mode),
        ),
        transforms=[transform],
    )


def _build_adopted_matrix_pipeline(
    *, replay_lineage_mode: ReplayLineageMode, transform: TransformStep
) -> Pipeline:
    source_builder: Callable[[], ExternalTableSourceStep] = {
        ReplayLineageMode.OFFSETS: _build_adopted_offset_matrix_source,
        ReplayLineageMode.TIMESTAMP: _build_adopted_timestamp_matrix_source,
        ReplayLineageMode.CURSOR: _build_adopted_cursor_matrix_source,
    }[replay_lineage_mode]
    return Pipeline(
        name="preservation_pipeline",
        source=source_builder(),
        transforms=[transform],
    )


def _build_adopted_offset_matrix_source() -> ExternalTableSourceStep:
    return ExternalTableSourceStep(
        name="orders",
        kind=SourceKind.KAFKA,
        table_name="orders_existing",
        replay_boundary=ReplayBoundary(
            mode=ReplayBoundaryMode.OFFSETS,
            columns=ReplayBoundaryColumns(
                partition="event_partition",
                offset="event_offset",
                timestamp="event_timestamp",
                landed_at="event_landed_at",
            ),
        ),
    )


def _build_adopted_timestamp_matrix_source() -> ExternalTableSourceStep:
    return ExternalTableSourceStep(
        name="orders",
        kind=SourceKind.KAFKA,
        table_name="orders_existing",
        replay_boundary=ReplayBoundary(
            mode=ReplayBoundaryMode.TIMESTAMP,
            columns=ReplayBoundaryColumns(timestamp="event_timestamp"),
        ),
    )


def _build_adopted_cursor_matrix_source() -> ExternalTableSourceStep:
    return ExternalTableSourceStep(
        name="orders",
        kind=SourceKind.STREAM_TABLE,
        table_name="orders_existing",
        replay_boundary=ReplayBoundary(
            mode=ReplayBoundaryMode.CURSOR,
            columns=ReplayBoundaryColumns(
                timestamp="event_timestamp",
                cursor="event_cursor",
            ),
        ),
    )


def _build_matrix_request(
    *,
    desired_state: DesiredState,
    database: str,
    replay_lineage_mode: ReplayLineageMode,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> BackfillBootstrapRequest:
    return BackfillBootstrapRequest(
        desired_state=desired_state,
        default_database=database,
        metadata_database=database,
        replay_lineage_mode=replay_lineage_mode,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
        stabilization_seconds=0.0,
    )


def _prepare_matrix_source(
    *,
    source_ownership: str,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    preparer: Callable[..., None] = {
        "managed": _prepare_managed_matrix_source,
        "adopted": _prepare_adopted_matrix_source,
    }[source_ownership]
    preparer(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
    )


def _prepare_managed_matrix_source(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    _create_live_landing_objects(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            _build_managed_matrix_row(order_id="historical-order", offset=1, second="58"),
            _build_managed_matrix_row(order_id="frontier-order", offset=2, second="59"),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )


def _prepare_adopted_matrix_source(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    del compiled_pipeline
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.orders_existing ("
        "order_id String, event_partition Int64, event_offset Int64, "
        "event_timestamp DateTime64(3), event_landed_at DateTime64(3), "
        "event_cursor UInt64) ENGINE = MergeTree() ORDER BY (order_id)"
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.orders_existing",
        data=[
            _build_adopted_matrix_row(order_id="historical-order", offset=1, second="58"),
            _build_adopted_matrix_row(order_id="frontier-order", offset=2, second="59"),
        ],
        column_names=[
            "order_id",
            "event_partition",
            "event_offset",
            "event_timestamp",
            "event_landed_at",
            "event_cursor",
        ],
    )


def _insert_matrix_tail(
    *,
    source_ownership: str,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    inserter: Callable[..., None] = {
        "managed": _insert_managed_matrix_tail,
        "adopted": _insert_adopted_matrix_tail,
    }[source_ownership]
    inserter(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
    )


def _insert_managed_matrix_tail(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[_build_managed_matrix_row(order_id="tail-order", offset=3, second="01")],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )


def _insert_adopted_matrix_tail(
    *,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    del compiled_pipeline
    clickhouse_client.insert(
        table=f"{clickhouse_database}.orders_existing",
        data=[_build_adopted_matrix_row(order_id="tail-order", offset=3, second="01")],
        column_names=[
            "order_id",
            "event_partition",
            "event_offset",
            "event_timestamp",
            "event_landed_at",
            "event_cursor",
        ],
    )


def _build_managed_matrix_row(*, order_id: str, offset: int, second: str) -> tuple[object, ...]:
    timestamp: str = {
        "58": "2026-04-09 17:59:58.000",
        "59": "2026-04-09 17:59:59.000",
        "01": "2026-04-09 18:00:01.000",
    }[second]
    return build_raw_orders_row(
        kafka_key=order_id,
        _replay_partition=0,
        _replay_offset=offset,
        _replay_timestamp=timestamp,
        _replay_landed_at=timestamp,
    )


def _build_adopted_matrix_row(*, order_id: str, offset: int, second: str) -> tuple[object, ...]:
    timestamp: str = {
        "58": "2026-04-09 17:59:58.000",
        "59": "2026-04-09 17:59:59.000",
        "01": "2026-04-09 18:00:01.000",
    }[second]
    return (order_id, 0, offset, timestamp, timestamp, offset)


def _execute_seeded_matrix_replay(
    *,
    client: AdapterConnection,
    desired_state: DesiredState,
    database: str,
    replay_lineage_mode: ReplayLineageMode,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> RebuildExecutionMode:
    result: BackfillExecutionResult = execute_backfill(
        request=_build_matrix_request(
            desired_state=desired_state,
            database=database,
            replay_lineage_mode=replay_lineage_mode,
            deployment_id=deployment_id,
            created_at=created_at,
            boundary_time=boundary_time,
        ),
        client=client,
    )
    return RebuildExecutionMode(result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode)


def _execute_unseeded_matrix_replay(
    *,
    client: AdapterConnection,
    desired_state: DesiredState,
    database: str,
    replay_lineage_mode: ReplayLineageMode,
    deployment_id: str,
    created_at: str,
    boundary_time: str,
) -> RebuildExecutionMode:
    request: BackfillBootstrapRequest = _build_matrix_request(
        desired_state=desired_state,
        database=database,
        replay_lineage_mode=replay_lineage_mode,
        deployment_id=deployment_id,
        created_at=created_at,
        boundary_time=boundary_time,
    )
    confirmed_request: BackfillBootstrapRequest
    confirmed_request, _, _ = _prepare_backfill_request(
        request=request,
        client=client,
    )
    confirmed_plan: DeploymentPlan = cast(DeploymentPlan, confirmed_request.confirmed_plan)
    deployment_plan: DeploymentPlan = replace(
        confirmed_plan,
        rebuild_subtrees=tuple(
            replace(subtree, execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED)
            for subtree in confirmed_plan.rebuild_subtrees
        ),
    )
    result: BackfillExecutionResult = execute_backfill(
        request=replace(confirmed_request, confirmed_plan=deployment_plan),
        client=client,
    )
    return RebuildExecutionMode(result.bootstrap.deployment_plan.rebuild_subtrees[0].execution_mode)


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
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    return realize_project(
        project=compiled_project,
        adapter_profile=adapter_profile,
        sql_analyzer=build_realization_analyzer(compiled_project),
    )


def build_desired_state(compiled_pipelines: tuple[CompiledPipeline, ...]) -> DesiredState:
    return realize_compiled_pipelines(compiled_pipelines).desired_state


def require_managed_source(compiled_pipeline: CompiledPipeline) -> ManagedSourceResources:
    realized_project: RealizedProject = realize_compiled_pipelines((compiled_pipeline,))
    compiled_source: CompiledSource = cast(CompiledSource, compiled_pipeline.source)
    source_resources: tuple[AdapterResource, ...] = realized_project.resources_by_logical_key[
        compiled_source.key
    ]
    managed_source: AdapterManagedSource = cast(AdapterManagedSource, source_resources[0])
    landing_table: AdapterTable = cast(AdapterTable, source_resources[1])
    landing_view: AdapterMaterializedView = cast(AdapterMaterializedView, source_resources[2])
    objects_by_name: dict[str, object] = {
        item.name: item for item in realized_project.desired_state.objects
    }
    return ManagedSourceResources(
        kafka_table=cast(DesiredKafkaTable, objects_by_name[managed_source.name]),
        raw_table=cast(DesiredTable, objects_by_name[landing_table.name]),
        materialized_view=cast(
            DesiredMaterializedView,
            objects_by_name[landing_view.name],
        ),
    )


def require_model_resources(compiled_pipeline: CompiledPipeline) -> ModelResources:
    realized_project: RealizedProject = realize_compiled_pipelines((compiled_pipeline,))
    model_resources: tuple[AdapterResource, ...] = realized_project.resources_by_logical_key[
        compiled_pipeline.models[0].key
    ]
    adapter_table: AdapterTable = cast(AdapterTable, model_resources[0])
    adapter_view: AdapterMaterializedView = cast(AdapterMaterializedView, model_resources[1])
    objects_by_name: dict[str, object] = {
        item.name: item for item in realized_project.desired_state.objects
    }
    return ModelResources(
        target_table=cast(DesiredTable, objects_by_name[adapter_table.name]),
        materialized_view=cast(
            DesiredMaterializedView,
            objects_by_name[adapter_view.name],
        ),
    )


def build_scalar_target_insert_select_sql(
    *,
    replay_lineage_mode: ReplayLineageMode | str,
    database: str,
    source_table_name: str,
) -> str:
    resolved_replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(replay_lineage_mode)
    return (
        "SELECT CAST(kafka_key AS String) AS order_id, "
        + SCALAR_REPLAY_PROJECTION.get(resolved_replay_lineage_mode, LANDED_AT_REPLAY_PROJECTION)
        + f"FROM {database}.{source_table_name}"
    )


def build_offset_target_insert_select_sql(*, database: str, source_table_name: str) -> str:
    return (
        "SELECT CAST(kafka_key AS String) AS order_id, "
        "CAST(_replay_partition AS Int64) AS _replay_partition, "
        "CAST(_replay_offset AS Int64) AS _replay_offset "
        f"FROM {database}.{source_table_name}"
    )


def build_replay_compiled_pipeline(*, replay_lineage_mode: str) -> CompiledPipeline:
    """Build the compiled pipeline matching a replay lineage mode.

    Offset lineage needs its own fixture; every scalar mode shares one builder
    parameterised by the mode, so callers do not branch on the mode themselves.
    """

    builders: dict[bool, Callable[[], CompiledPipeline]] = {
        True: build_offset_replay_compiled_pipeline,
        False: lambda: build_scalar_replay_compiled_pipeline(replay_lineage_mode),
    }
    return builders[replay_lineage_mode == ReplayLineageMode.OFFSETS]()


def build_target_insert_select_sql(
    *, replay_lineage_mode: ReplayLineageMode | str, database: str, source_table_name: str
) -> str:
    """Build the target insert SELECT for a replay lineage mode.

    Offset lineage projects partition and offset columns; every scalar mode
    projects a single boundary column, so callers do not dispatch themselves.
    """

    builders: dict[bool, Callable[[], str]] = {
        True: lambda: build_offset_target_insert_select_sql(
            database=database, source_table_name=source_table_name
        ),
        False: lambda: build_scalar_target_insert_select_sql(
            replay_lineage_mode=replay_lineage_mode,
            database=database,
            source_table_name=source_table_name,
        ),
    }
    return builders[ReplayLineageMode(replay_lineage_mode) == ReplayLineageMode.OFFSETS]()


STAGED_ROW_FILTERS: dict[tuple[bool, bool], str] = {
    (True, True): " WHERE 0",
    (True, False): " WHERE 0",
    (False, True): "",
    (False, False): " WHERE kafka_key = 'historical-order'",
}


def _create_live_landing_objects(
    *, clickhouse_client: Client, clickhouse_database: str, compiled_pipeline: CompiledPipeline
) -> None:
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )


def _leave_live_landing_objects_absent(
    *, clickhouse_client: Client, clickhouse_database: str, compiled_pipeline: CompiledPipeline
) -> None:
    del clickhouse_client, clickhouse_database, compiled_pipeline


LIVE_LANDING_SETUP_BY_PRECREATED: dict[bool, Callable[..., None]] = {
    True: _create_live_landing_objects,
    False: _leave_live_landing_objects_absent,
}


def prepare_live_landing_objects(
    *,
    precreate: bool,
    clickhouse_client: Client,
    clickhouse_database: str,
    compiled_pipeline: CompiledPipeline,
) -> None:
    LIVE_LANDING_SETUP_BY_PRECREATED[precreate](
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
    )


def _assert_cursor_start_time_boundary(
    *, clickhouse_client: Client, clickhouse_database: str, start_time: str | None
) -> None:
    assert clickhouse_client.query(
        "SELECT min(event_cursor) FROM "
        f"{clickhouse_database}.orders_existing "
        f"WHERE event_timestamp >= toDateTime64('{start_time}', 3)"
    ).result_rows == [(2,)]


def _skip_cursor_start_time_boundary_assertion(
    *, clickhouse_client: Client, clickhouse_database: str, start_time: str | None
) -> None:
    del clickhouse_client, clickhouse_database, start_time


CURSOR_BOUNDARY_ASSERTION_BY_START_TIME: dict[bool, Callable[..., None]] = {
    True: _assert_cursor_start_time_boundary,
    False: _skip_cursor_start_time_boundary_assertion,
}


def assert_external_cursor_start_time_boundary(
    *, clickhouse_client: Client, clickhouse_database: str, start_time: str | None
) -> None:
    CURSOR_BOUNDARY_ASSERTION_BY_START_TIME[start_time is not None](
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        start_time=start_time,
    )


START_TIME_PIPELINE_BUILDERS: dict[
    ReplayLineageMode, Callable[[ReplayLineageMode], CompiledPipeline]
] = {
    ReplayLineageMode.OFFSETS: lambda _: build_offset_replay_compiled_pipeline(),
    ReplayLineageMode.TIMESTAMP: build_scalar_replay_compiled_pipeline,
    ReplayLineageMode.LANDED_AT: build_scalar_replay_compiled_pipeline,
}
CHANGED_START_TIME_PIPELINE_BUILDERS: dict[
    ReplayLineageMode, Callable[[ReplayLineageMode], CompiledPipeline]
] = {
    ReplayLineageMode.OFFSETS: lambda _: build_changed_offset_replay_compiled_pipeline(),
    ReplayLineageMode.TIMESTAMP: build_changed_scalar_replay_compiled_pipeline,
    ReplayLineageMode.LANDED_AT: build_changed_scalar_replay_compiled_pipeline,
}
START_TIME_QUERY_COLUMN_BY_MODE: dict[ReplayLineageMode, str] = {
    ReplayLineageMode.OFFSETS: "_replay_landed_at",
    ReplayLineageMode.TIMESTAMP: "_replay_timestamp",
    ReplayLineageMode.LANDED_AT: "_replay_timestamp",
}
START_TIME_ROW_VALUES_BY_MODE: dict[ReplayLineageMode, tuple[tuple[int, str], ...]] = {
    ReplayLineageMode.OFFSETS: (
        (10, "2026-04-09 17:09:58.000"),
        (11, "2026-04-09 17:09:59.000"),
        (12, "2026-04-09 17:15:01.000"),
    ),
    ReplayLineageMode.TIMESTAMP: (
        (1, "2026-04-09 15:59:58.000"),
        (2, "2026-04-09 15:59:59.000"),
        (3, "2026-04-09 17:05:01.000"),
    ),
    ReplayLineageMode.LANDED_AT: (
        (1, "2026-04-09 15:59:58.000"),
        (2, "2026-04-09 15:59:59.000"),
        (3, "2026-04-09 17:05:01.000"),
    ),
}


def run_start_time_replay_scenario(
    *,
    test_case: ExecuteStartTimeReplayIntegrationTestCase,
    connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> StartTimeReplayScenarioResult:
    replay_lineage_mode: ReplayLineageMode = ReplayLineageMode(test_case.replay_lineage_mode)
    compiled_pipeline: CompiledPipeline = START_TIME_PIPELINE_BUILDERS[replay_lineage_mode](
        replay_lineage_mode
    )
    changed_pipeline: CompiledPipeline = CHANGED_START_TIME_PIPELINE_BUILDERS[replay_lineage_mode](
        replay_lineage_mode
    )
    changed_desired_state: DesiredState = build_desired_state((changed_pipeline,))
    timestamp_query_sql: str = (
        f"SELECT max({START_TIME_QUERY_COLUMN_BY_MODE[replay_lineage_mode]}) FROM "
        f"{clickhouse_database}.{{raw_table_name}} "
        f"WHERE kafka_key = '{test_case.lower_bound_source_order_id}'"
    )
    historical_values, frontier_values, live_values = START_TIME_ROW_VALUES_BY_MODE[
        replay_lineage_mode
    ]

    _create_live_landing_objects(
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=historical_values[0],
                _replay_timestamp=historical_values[1],
                _replay_landed_at=historical_values[1],
            ),
            build_raw_orders_row(
                kafka_key="frontier-order",
                _replay_partition=0,
                _replay_offset=frontier_values[0],
                _replay_timestamp=frontier_values[1],
                _replay_landed_at=frontier_values[1],
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=connection_settings.host,
            port=connection_settings.port,
            username=connection_settings.username,
            password=connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        initial_result: BackfillExecutionResult = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=build_desired_state((compiled_pipeline,)),
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode=replay_lineage_mode,
                deployment_id=test_case.initial_deployment_id,
                created_at=test_case.created_at,
                boundary_time=test_case.initial_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_result.bootstrap.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
        frontier_timestamp: datetime = clickhouse_client.query(
            timestamp_query_sql.format(
                raw_table_name=require_managed_source(compiled_pipeline).raw_table.name
            )
        ).result_rows[0][0]
        converted_start_time: str = (
            frontier_timestamp - timedelta(milliseconds=test_case.lower_bound_offset_millis)
        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        start_time_result: BackfillExecutionResult = execute_backfill(
            request=BackfillBootstrapRequest(
                desired_state=changed_desired_state,
                default_database=clickhouse_database,
                metadata_database=clickhouse_database,
                replay_lineage_mode=replay_lineage_mode,
                deployment_id=test_case.changed_deployment_id,
                created_at=test_case.created_at,
                start_time_keys=frozenset(
                    {require_model_resources(compiled_pipeline).target_table.key}
                ),
                start_time=converted_start_time,
                boundary_time=test_case.changed_boundary_time,
                stabilization_seconds=0.0,
            ),
            client=managed_client,
        )
        clickhouse_client.insert(
            table=(
                f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}"
            ),
            data=[
                build_raw_orders_row(
                    kafka_key="live-order",
                    _replay_partition=0,
                    _replay_offset=live_values[0],
                    _replay_timestamp=live_values[1],
                    _replay_landed_at=live_values[1],
                )
            ],
            column_names=[
                "kafka_key",
                "kafka_value",
                "kafka_topic",
                "_replay_partition",
                "_replay_offset",
                "_replay_timestamp",
                "kafka_header_keys",
                "kafka_header_values",
                "_replay_landed_at",
            ],
        )
    finally:
        managed_client.close()

    shadow_rows_result: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT order_id, max(kafka_topic) FROM "
        f"{clickhouse_database}.{test_case.expected_shadow_table_name} "
        "GROUP BY order_id ORDER BY order_id"
    ).result_rows
    return StartTimeReplayScenarioResult(
        connection_settings=connection_settings,
        database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
        start_time_result=start_time_result,
        converted_start_time=converted_start_time,
        shadow_rows=tuple((str(row[0]), str(row[1])) for row in shadow_rows_result),
    )
