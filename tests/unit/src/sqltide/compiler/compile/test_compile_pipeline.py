from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import sqltide._native as _native
from sqltide._native import build_compile_inputs, build_compiler_adapter_profile
from sqltide.adapter.models import AdapterManagedSource, AdapterMaterializedView, AdapterTable
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.exceptions import (
    PipelineCompileError,
    TransformOrderByUnknownColumnError,
    TransformPartitionByUnknownColumnError,
    TransformSqlTopLevelSetOperationError,
    TransformTtlUnknownColumnError,
)
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompiledTableModel,
    CompileProjectInputs,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
)
from sqltide.compiler.discovery.models import (
    DiscoveredProjectFile,
    DiscoveredProjectInputs,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaSettings,
    LoadedPipeline,
    LoadedProject,
    Pipeline,
    Project,
    ProjectDependencies,
    ReplayBoundary,
    ReplayBoundaryColumns,
    ReplayOnChangePolicy,
    ReplayOnChangeRule,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    ModelReferenceScope,
    ReplayAnchorMode,
    ReplayBoundaryMode,
    ReplayLineageMode,
    ReplayOnChangeMode,
    SourceKind,
)
from sqltide.compiler.pipeline.models import RealizedProject
from tests.unit.src.sqltide.compiler.compile._test_types import (
    CompileAdoptedSourceSharingTestCase,
    CompileModelNamingTestCase,
    CompileModelRetentionTestCase,
    CompileModelTtlDefaultTestCase,
    CompilePipelineAdditionalRefDependencyTestCase,
    CompilePipelineAdoptedSourceTestCase,
    CompilePipelineInlineRefsTestCase,
    CompilePipelineInlineSqlSuccessTestCase,
    CompilePipelineInvalidOrderByTestCase,
    CompilePipelineInvalidPartitionByTestCase,
    CompilePipelineInvalidTransformSqlTestCase,
    CompilePipelineInvalidTtlTestCase,
    CompilePipelineMissingRefTypeTestCase,
    CompilePipelineMissingSourceRefTestCase,
    CompilePipelineRepeatedSourceRefTestCase,
    CompilePipelineReplayAnchorEligibilityTestCase,
    CompilePipelineReplayLineageModeResolutionTestCase,
    CompilePipelineReplayLineageModeTestCase,
    CompilePipelineReplayPolicyPrecedenceTestCase,
    CompilePipelineReplaySurfaceTestCase,
    CompilePipelineSqlFileTestCase,
    CompilePipelineSqlModelDefaultOrderByTestCase,
    CompilePipelineUnsupportedReplayBehaviorTestCase,
    CompileRelationCollisionTestCase,
    CompileRelationNameErrorTestCase,
    CompileRetentionErrorTestCase,
    ProgrammaticDependencyScopeTestCase,
)
from tests.unit.src.sqltide.compiler.compile.helpers import (
    build_inline_sql_pipeline,
    build_invalid_order_by_pipeline,
    build_invalid_storage_expression_pipeline,
    build_missing_source_ref_pipeline,
    build_sql_file_pipeline,
    compile_and_realize_pipeline,
    compile_logical_project,
    compile_test_pipeline,
    realized_managed_source,
    realized_model_table,
    realized_model_view,
    realized_source_table,
    realized_source_view,
    write_registry_pipeline_project,
)
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import (
    write_project_toml,
    write_source_yml,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_single_transform_desired_state,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInlineRefsTestCase(
            description="resolves inline refs for example pipeline",
            pipeline_dir="tests/fixtures/basic_project/pipelines/pl__orders",
            expected_relation_names={
                "orders": "raw__orders",
                "orders_enriched": "tbl__orders_enriched",
            },
            expected_kafka_table_name="kafka__orders",
            expected_raw_table_name="raw__orders",
            expected_landing_mv_name="mv__orders",
            expected_landing_query_fragments=(
                "_key AS kafka_key",
                "message AS kafka_value",
                "_topic AS kafka_topic",
                "_partition AS kafka_partition",
                "_offset AS kafka_offset",
                "_timestamp_ms AS kafka_timestamp",
                "_partition AS _replay_partition",
                "_offset AS _replay_offset",
                "_timestamp_ms AS _replay_timestamp",
                "_headers.name AS kafka_header_keys",
                "_headers.value AS kafka_header_values",
                "now64(3) AS _replay_landed_at",
                "now64(3) AS _replay_landed_at",
                "FROM kafka__orders",
            ),
            expected_refs=("orders",),
            expected_query_fragment="FROM raw__orders",
            expected_target_table_name="tbl__orders_enriched",
            expected_transform_mv_name="mv__orders_enriched",
            expected_desired_state_ordered_keys=(
                (None, "kafka_table", "kafka__orders"),
                (None, "materialized_view", "mv__orders"),
                (None, "materialized_view", "mv__orders_enriched"),
                (None, "table", "raw__orders"),
                (None, "table", "tbl__orders_enriched"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_example_pipeline_when_compiling_then_resolves_inline_refs(
    test_case: CompilePipelineInlineRefsTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(Path(test_case.pipeline_dir))
    compiled_pipeline: CompiledPipeline
    realized_project: RealizedProject
    compiled_pipeline, realized_project = compile_and_realize_pipeline(loaded_pipeline)
    managed_source: AdapterManagedSource = realized_managed_source(realized_project)
    landing_table: AdapterTable = realized_source_table(realized_project)
    landing_view: AdapterMaterializedView = realized_source_view(realized_project)
    model: CompiledModel = compiled_pipeline.models[0]
    model_table: AdapterTable = realized_model_table(realized_project, model.key.name)
    model_view: AdapterMaterializedView = realized_model_view(realized_project, model.key.name)

    assert {
        key.name: name for key, name in realized_project.relation_name_by_logical_key.items()
    } == test_case.expected_relation_names
    assert managed_source.name == test_case.expected_kafka_table_name
    assert landing_table.name == test_case.expected_raw_table_name
    assert landing_view.name == test_case.expected_landing_mv_name
    assert landing_view.source_relation_name == test_case.expected_kafka_table_name
    assert landing_view.target_relation_name == test_case.expected_raw_table_name
    for expected_fragment in test_case.expected_landing_query_fragments:
        assert expected_fragment in landing_view.query
    assert model.refs == test_case.expected_refs
    assert (
        test_case.expected_query_fragment in realized_project.resolved_query_by_model_key[model.key]
    )
    assert model_table.name == test_case.expected_target_table_name
    assert model_view.name == test_case.expected_transform_mv_name
    assert model_view.source_relation_name == test_case.expected_raw_table_name
    assert model_view.target_relation_name == test_case.expected_target_table_name
    desired_state: DesiredState = realized_project.desired_state
    desired_by_name: dict[str, object] = {item.name: item for item in desired_state.objects}
    desired_table: DesiredTable = cast(DesiredTable, desired_by_name[model_table.name])
    desired_view: DesiredMaterializedView = cast(
        DesiredMaterializedView,
        desired_by_name[model_view.name],
    )
    assert tuple(
        (dependency.database, dependency.object_type, dependency.name)
        for dependency in desired_table.deps
    ) == ((None, "table", "raw__orders"),)
    assert tuple(
        (dependency.database, dependency.object_type, dependency.name)
        for dependency in desired_view.deps
    ) == (
        (None, "table", "raw__orders"),
        (None, "table", "tbl__orders_enriched"),
    )
    assert tuple(column.name for column in model.output_columns) == (
        "order_id",
        "customer_id",
        "order_total",
        "created_at",
        "updated_at",
        "_replay_landed_at",
    )

    assert (
        tuple(
            (object_.key.database, object_.key.object_type, object_.key.name)
            for object_ in desired_state.objects
        )
        == test_case.expected_desired_state_ordered_keys
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineAdoptedSourceTestCase(
            description="compiles adopted source against its existing table name",
            pipeline_file_contents="""
source:
  kind: stream_table
  name: orders
  table_name: orders_existing
  replay_boundary:
    mode: offsets
    columns:
      _replay_partition: event_partition
      _replay_offset: event_offset
      _replay_timestamp: event_timestamp
""",
            sql_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);

SELECT
  CAST(order_id AS UInt64) AS order_id,
  CAST(event_partition AS UInt64) AS _replay_partition,
  CAST(event_offset AS UInt64) AS _replay_offset
FROM __ref("orders")
""",
            expected_source_relation_name="orders_existing",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adopted_source_when_compiling_then_it_uses_existing_table_relation_name(
    test_case: CompilePipelineAdoptedSourceTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_pipeline_project(
        project_dir=tmp_path,
        source_contents=test_case.pipeline_file_contents,
        model_contents=test_case.sql_contents,
    )

    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    compiled_pipeline: CompiledPipeline
    realized_project: RealizedProject
    compiled_pipeline, realized_project = compile_and_realize_pipeline(loaded_pipeline)
    model: CompiledModel = compiled_pipeline.models[0]

    compiled_source: CompiledSource = cast(CompiledSource, compiled_pipeline.source)
    assert isinstance(compiled_source.source, ExternalTableSourceStep)
    assert {
        key.name: name for key, name in realized_project.relation_name_by_logical_key.items()
    } == {
        "orders": test_case.expected_source_relation_name,
        "orders_enriched": "tbl__orders_enriched",
    }
    assert (
        f"FROM {test_case.expected_source_relation_name}"
        in realized_project.resolved_query_by_model_key[model.key]
    )

    desired_state: DesiredState = realized_project.desired_state

    assert tuple(object_.key.name for object_ in desired_state.objects) == (
        "mv__orders_enriched",
        "tbl__orders_enriched",
    )
    assert tuple(config.table_name for config in desired_state.external_source_replay_configs) == (
        test_case.expected_source_relation_name,
    )
    assert any(
        key.name == test_case.expected_source_relation_name
        for key in desired_state.replay_anchor_keys
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInlineSqlSuccessTestCase(
            description="compiles clickhouse double-colon casts in transform sql",
            transform_query='SELECT order_id::UInt64 AS order_id FROM __ref("orders")',
            expected_output_columns=(("order_id", "UInt64"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_inline_transform_sql_when_compiling_then_it_derives_expected_columns(
    test_case: CompilePipelineInlineSqlSuccessTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(test_case.transform_query)

    compiled_pipeline: CompiledPipeline = compile_test_pipeline(loaded_pipeline)

    assert (
        tuple((column.name, column.type) for column in compiled_pipeline.models[0].output_columns)
        == test_case.expected_output_columns
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineSqlFileTestCase(
            description="loads sql file relative to pipeline file",
            sql_relative_path="sql/prices.sql",
            sql_contents='SELECT CAST(kafka_value AS UInt64) AS order_id FROM __ref("orders")',
            expected_resolved_query=(
                "SELECT CAST(kafka_value AS UInt64) AS order_id FROM raw__orders"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transform_sql_file_when_compiling_then_loads_sql_relative_to_pipeline_file(
    test_case: CompilePipelineSqlFileTestCase,
    tmp_path: Path,
) -> None:
    loaded_pipeline: LoadedPipeline = build_sql_file_pipeline(
        tmp_path,
        test_case.sql_relative_path,
        test_case.sql_contents,
    )

    compiled_pipeline: CompiledPipeline
    realized_project: RealizedProject
    compiled_pipeline, realized_project = compile_and_realize_pipeline(loaded_pipeline)

    assert (
        realized_project.resolved_query_by_model_key[compiled_pipeline.models[0].key]
        == test_case.expected_resolved_query
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineReplayLineageModeTestCase(
            description="derives timestamp lineage from the managed source boundary",
            replay_boundary_mode=ReplayBoundaryMode.TIMESTAMP,
            expected_effective_replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
        ),
        CompilePipelineReplayLineageModeTestCase(
            description="derives landed-at lineage from the managed source boundary",
            replay_boundary_mode=ReplayBoundaryMode.LANDED_AT,
            expected_effective_replay_lineage_mode=ReplayLineageMode.LANDED_AT,
        ),
        CompilePipelineReplayLineageModeTestCase(
            description="defaults a programmatic managed source without a boundary to offsets",
            replay_boundary_mode=None,
            expected_effective_replay_lineage_mode=ReplayLineageMode.OFFSETS,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_loaded_pipeline_when_compiling_then_it_resolves_effective_replay_lineage_mode(
    test_case: CompilePipelineReplayLineageModeTestCase,
) -> None:
    base_loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(
        transform_query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")'
    )
    source: KafkaLandingStep = cast(KafkaLandingStep, base_loaded_pipeline.pipeline.source)
    boundary_by_mode: dict[ReplayBoundaryMode | str | None, ReplayBoundary | None] = {
        None: None,
        ReplayBoundaryMode.TIMESTAMP: ReplayBoundary(
            mode=ReplayBoundaryMode.TIMESTAMP,
            columns=ReplayBoundaryColumns(),
        ),
        ReplayBoundaryMode.LANDED_AT: ReplayBoundary(
            mode=ReplayBoundaryMode.LANDED_AT,
            columns=ReplayBoundaryColumns(),
        ),
    }
    pipeline: Pipeline = replace(
        base_loaded_pipeline.pipeline,
        source=replace(source, replay_boundary=boundary_by_mode[test_case.replay_boundary_mode]),
    )
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=pipeline,
        file_path=base_loaded_pipeline.file_path,
        project=None,
    )

    compiled_pipeline: CompiledPipeline = compile_test_pipeline(loaded_pipeline)

    assert (
        compiled_pipeline.effective_replay_lineage_mode
        == test_case.expected_effective_replay_lineage_mode
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineReplayPolicyPrecedenceTestCase(
            description="uses project replay policy when narrower scopes omit it",
            model_policy=None,
            pipeline_policy=None,
            project_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.FULL)
            ),
            expected_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.FULL)
            ),
        ),
        CompilePipelineReplayPolicyPrecedenceTestCase(
            description="pipeline replay policy overrides project default",
            model_policy=None,
            pipeline_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.BOUNDED,
                    lookback_seconds=3600,
                )
            ),
            project_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.FULL)
            ),
            expected_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.BOUNDED,
                    lookback_seconds=3600,
                )
            ),
        ),
        CompilePipelineReplayPolicyPrecedenceTestCase(
            description="model replay policy overrides pipeline and project defaults",
            model_policy=ReplayOnChangePolicy(
                non_breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.BOUNDED,
                    lookback_seconds=300,
                )
            ),
            pipeline_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.BOUNDED,
                    lookback_seconds=3600,
                )
            ),
            project_policy=ReplayOnChangePolicy(
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.FULL)
            ),
            expected_policy=ReplayOnChangePolicy(
                non_breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.BOUNDED,
                    lookback_seconds=300,
                )
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_replay_policy_scopes_when_compiling_then_model_pipeline_project_precedence_applies(
    test_case: CompilePipelineReplayPolicyPrecedenceTestCase,
) -> None:
    base_loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(
        transform_query=(
            "SELECT order_id::UInt64 AS order_id, "
            "_replay_partition::Int32 AS _replay_partition, "
            "_replay_offset::Int64 AS _replay_offset "
            'FROM __ref("orders")'
        )
    )
    pipeline: Pipeline = replace(
        base_loaded_pipeline.pipeline,
        transforms=(
            replace(
                base_loaded_pipeline.pipeline.transforms[0],
                replay_on_change=test_case.model_policy,
            ),
        ),
        replay_on_change=test_case.pipeline_policy,
    )
    compiled_pipeline: CompiledPipeline = compile_test_pipeline(
        replace(
            base_loaded_pipeline,
            pipeline=pipeline,
            project=Project(replay_on_change=test_case.project_policy),
        )
    )

    compiled_model: CompiledTableModel = cast(CompiledTableModel, compiled_pipeline.models[0])
    assert compiled_model.replay_on_change == test_case.expected_policy


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineUnsupportedReplayBehaviorTestCase(
            description=(
                "uses project unsupported replay behavior when pipeline does not override it"
            ),
            transform_unsupported_replay_behavior=None,
            pipeline_unsupported_replay_behavior=None,
            project_unsupported_replay_behavior=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
            expected_effective_unsupported_replay_behavior=(
                BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY
            ),
        ),
        CompilePipelineUnsupportedReplayBehaviorTestCase(
            description="pipeline unsupported replay behavior overrides project default",
            transform_unsupported_replay_behavior=None,
            pipeline_unsupported_replay_behavior=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
            project_unsupported_replay_behavior=BoundedReplayFallback.FULL,
            expected_effective_unsupported_replay_behavior=(
                BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY
            ),
        ),
        CompilePipelineUnsupportedReplayBehaviorTestCase(
            description=(
                "transform unsupported replay behavior overrides pipeline and project defaults"
            ),
            transform_unsupported_replay_behavior=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
            pipeline_unsupported_replay_behavior=BoundedReplayFallback.FULL,
            project_unsupported_replay_behavior=BoundedReplayFallback.FULL,
            expected_effective_unsupported_replay_behavior=(
                BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_loaded_pipeline_when_compiling_then_it_resolves_effective_unsupported_behavior(
    test_case: CompilePipelineUnsupportedReplayBehaviorTestCase,
) -> None:
    pipeline: Pipeline = Pipeline(
        name="orders_pipeline",
        source=KafkaLandingStep(
            name="orders",
            kafka=KafkaSettings(broker_list="kafka:9092", topic="source.orders.created"),
        ),
        transforms=[
            TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=["order_id"],
                query='SELECT CAST(kafka_key AS String) AS order_id FROM __ref("orders")',
                bounded_replay_fallback=test_case.transform_unsupported_replay_behavior,
            )
        ],
        bounded_replay_fallback=test_case.pipeline_unsupported_replay_behavior,
    )
    project_by_unsupported_replay_behavior: dict[
        BoundedReplayFallback | str | None, Project | None
    ] = {
        None: None,
        BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY: Project(
            bounded_replay_fallback=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY
        ),
        BoundedReplayFallback.FULL: Project(bounded_replay_fallback=BoundedReplayFallback.FULL),
    }
    project: Project | None = project_by_unsupported_replay_behavior[
        test_case.project_unsupported_replay_behavior
    ]

    compiled_pipeline: CompiledPipeline = compile_test_pipeline(
        LoadedPipeline(pipeline=pipeline, file_path=Path("pipeline"), project=project)
    )

    compiled_model: CompiledTableModel = cast(CompiledTableModel, compiled_pipeline.models[0])
    assert (
        compiled_model.effective_bounded_replay_fallback
        == test_case.expected_effective_unsupported_replay_behavior
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineReplayAnchorEligibilityTestCase(
            description=(
                "marks event-like transform with required offset lineage as replay-anchor-eligible"
            ),
            transform_query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders")'
            ),
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            expected_preserves_required_lineage=True,
            expected_replay_anchor_eligible=True,
        ),
        CompilePipelineReplayAnchorEligibilityTestCase(
            description="marks mutable-ref transform as non-anchor-eligible",
            transform_query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders") LEFT JOIN '
                '__ref("customers", ref_type="mutable") USING customer_id'
            ),
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            supporting_transforms=(
                (
                    "customers",
                    'SELECT CAST(customer_id AS UInt64) AS customer_id FROM __ref("orders")',
                ),
            ),
            expected_has_mutable_refs=True,
            expected_preserves_required_lineage=True,
            expected_replay_anchor_eligible=False,
        ),
        CompilePipelineReplayAnchorEligibilityTestCase(
            description="marks grouped transform as non-anchor-eligible",
            transform_query=(
                "SELECT CAST(customer_id AS UInt64) AS customer_id, "
                "CAST(count() AS UInt64) AS order_count, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders") GROUP BY customer_id, _replay_partition, _replay_offset'
            ),
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            order_by=("customer_id",),
            expected_has_aggregate_semantics=True,
            expected_preserves_required_lineage=True,
            expected_replay_anchor_eligible=False,
        ),
        CompilePipelineReplayAnchorEligibilityTestCase(
            description="marks ClickHouse aggregate-state combinator as non-anchor-eligible",
            transform_query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(sumIfState(order_id, 1) AS AggregateFunction(sum, UInt64)) AS total_state, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders")'
            ),
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            expected_has_aggregate_semantics=True,
            expected_preserves_required_lineage=True,
            expected_replay_anchor_eligible=False,
        ),
        CompilePipelineReplayAnchorEligibilityTestCase(
            description="marks transform missing required lineage as non-anchor-eligible",
            transform_query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")',
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            expected_preserves_required_lineage=False,
            expected_replay_anchor_eligible=False,
        ),
        CompilePipelineReplayAnchorEligibilityTestCase(
            description="respects explicit replay-anchor veto",
            transform_query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders")'
            ),
            replay_lineage_mode=ReplayLineageMode.OFFSETS,
            replay_anchor=ReplayAnchorMode.NEVER,
            expected_preserves_required_lineage=True,
            expected_replay_anchor_eligible=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_transform_when_compiling_then_it_sets_replay_anchor_inference_flags(
    test_case: CompilePipelineReplayAnchorEligibilityTestCase,
) -> None:
    source: KafkaLandingStep = KafkaLandingStep(
        name="orders",
        kafka=KafkaSettings(
            broker_list="kafka:9092",
            topic="source.orders",
            consumer_group="sqltide_tmp_pipeline_orders",
        ),
        replay_boundary=ReplayBoundary(
            mode=ReplayBoundaryMode(test_case.replay_lineage_mode),
            columns=ReplayBoundaryColumns(),
        ),
    )
    supporting_transforms: list[TransformStep] = [
        TransformStep(
            name=name,
            source="orders",
            engine="MergeTree()",
            order_by=["customer_id"],
            query=query,
        )
        for name, query in test_case.supporting_transforms
    ]
    transform: TransformStep = TransformStep(
        name="orders_enriched",
        source="orders",
        engine=test_case.engine,
        order_by=list(test_case.order_by),
        query=test_case.transform_query,
        replay_anchor=test_case.replay_anchor,
    )
    pipeline: Pipeline = Pipeline(
        name="tmp_pipeline",
        source=source,
        transforms=[*supporting_transforms, transform],
    )
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=pipeline,
        file_path=Path("tests/fixtures/basic_project/pipelines/pl__orders"),
        project=None,
    )

    compiled_pipeline: CompiledPipeline = compile_test_pipeline(loaded_pipeline)
    compiled_model: CompiledTableModel = cast(CompiledTableModel, compiled_pipeline.models[-1])

    assert compiled_model.has_mutable_refs is test_case.expected_has_mutable_refs
    assert compiled_model.has_aggregate_semantics is test_case.expected_has_aggregate_semantics
    assert (
        compiled_model.preserves_required_lineage is test_case.expected_preserves_required_lineage
    )
    assert compiled_model.replay_anchor_eligible is test_case.expected_replay_anchor_eligible


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineReplaySurfaceTestCase(
            description="managed kafka landing exposes replay lineage columns to transforms",
            pipeline=Pipeline(
                name="tmp_pipeline",
                source=KafkaLandingStep(
                    name="orders",
                    kafka=KafkaSettings(
                        broker_list="kafka:9092",
                        topic="source.orders",
                        consumer_group="sqltide_tmp_pipeline_orders",
                    ),
                    replay_boundary=ReplayBoundary(
                        mode=ReplayBoundaryMode.LANDED_AT,
                        columns=ReplayBoundaryColumns(),
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
                            "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at "
                            'FROM __ref("orders")'
                        ),
                    )
                ],
            ),
            expected_query_fragments=("_replay_landed_at",),
            expected_output_column_names=("order_id", "_replay_landed_at"),
        ),
        CompilePipelineReplaySurfaceTestCase(
            description="adopted external sources expose replay lineage columns to transforms",
            pipeline=Pipeline(
                name="tmp_pipeline",
                source=ExternalTableSourceStep(
                    name="orders",
                    kind=SourceKind.KAFKA,
                    table_name="orders_existing",
                    replay_boundary=ReplayBoundary(
                        mode=ReplayBoundaryMode.TIMESTAMP,
                        columns=ReplayBoundaryColumns(timestamp="event_timestamp"),
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
                            "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp "
                            'FROM __ref("orders")'
                        ),
                    )
                ],
            ),
            expected_query_fragments=("_replay_timestamp", "event_timestamp"),
            expected_output_column_names=("order_id", "_replay_timestamp"),
        ),
        CompilePipelineReplaySurfaceTestCase(
            description="adopted cursor sources expose replay cursor columns to transforms",
            pipeline=Pipeline(
                name="tmp_pipeline",
                source=ExternalTableSourceStep(
                    name="orders",
                    kind=SourceKind.STREAM_TABLE,
                    table_name="orders_existing",
                    replay_boundary=ReplayBoundary(
                        mode=ReplayBoundaryMode.CURSOR,
                        columns=ReplayBoundaryColumns(cursor="event_cursor"),
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
                            "CAST(_replay_cursor AS UInt64) AS _replay_cursor "
                            'FROM __ref("orders")'
                        ),
                    )
                ],
            ),
            expected_query_fragments=("_replay_cursor", "event_cursor"),
            expected_output_column_names=("order_id", "_replay_cursor"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_when_compiling_then_it_exposes_the_replay_surface(
    test_case: CompilePipelineReplaySurfaceTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=test_case.pipeline,
        file_path=Path("tests/fixtures/basic_project/pipelines/pl__orders"),
        project=None,
    )
    compiled_pipeline: CompiledPipeline
    realized_project: RealizedProject
    compiled_pipeline, realized_project = compile_and_realize_pipeline(loaded_pipeline)
    model: CompiledModel = compiled_pipeline.models[0]

    expected_query_fragment: str
    for expected_query_fragment in test_case.expected_query_fragments:
        assert expected_query_fragment in realized_project.resolved_query_by_model_key[model.key]
    assert (
        tuple(column.name for column in model.output_columns)
        == test_case.expected_output_column_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineReplayLineageModeResolutionTestCase(
            description="cursor external source resolves cursor replay lineage mode",
            pipeline=Pipeline(
                name="tmp_pipeline",
                source=ExternalTableSourceStep(
                    name="orders",
                    kind=SourceKind.STREAM_TABLE,
                    table_name="orders_existing",
                    replay_boundary=ReplayBoundary(
                        mode=ReplayBoundaryMode.CURSOR,
                        columns=ReplayBoundaryColumns(cursor="event_cursor"),
                    ),
                ),
                transforms=[
                    TransformStep(
                        name="orders_enriched",
                        source="orders",
                        engine="MergeTree()",
                        order_by=["order_id"],
                        query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")',
                    )
                ],
            ),
            expected_replay_lineage_mode=ReplayLineageMode.CURSOR,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_when_compiling_then_it_resolves_expected_replay_lineage_mode(
    test_case: CompilePipelineReplayLineageModeResolutionTestCase,
) -> None:
    compiled_pipeline: CompiledPipeline = compile_test_pipeline(
        LoadedPipeline(
            pipeline=test_case.pipeline,
            file_path=Path("tests/fixtures/basic_project/pipelines/pl__orders"),
            project=None,
        )
    )

    assert compiled_pipeline.effective_replay_lineage_mode == test_case.expected_replay_lineage_mode


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineMissingSourceRefTestCase(
            description="raises value error when transform sql omits source ref",
            transform_query="SELECT CAST(order_id AS String) AS order_id FROM tbl__other",
            expected_error_type=ValueError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transform_without_source_ref_when_compiling_then_raises_value_error(
    test_case: CompilePipelineMissingSourceRefTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_missing_source_ref_pipeline(test_case.transform_query)

    with pytest.raises(test_case.expected_error_type):
        compile_test_pipeline(loaded_pipeline)


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInvalidTransformSqlTestCase(
            description="rejects prewhere because generated materialized views do not support it",
            transform_query=(
                'SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders") '
                "PREWHERE order_id > 0"
            ),
            expected_error_type=PipelineCompileError,
            expected_message_fragments=(
                "uses PREWHERE, which ClickHouse does not allow in materialized views; "
                "use WHERE instead",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transform_with_prewhere_when_compiling_then_it_raises_a_clear_error(
    test_case: CompilePipelineInvalidTransformSqlTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(test_case.transform_query)

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    for expected_message_fragment in test_case.expected_message_fragments:
        assert expected_message_fragment in str(error_info.value)


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineMissingRefTypeTestCase(
            description="raises value error when additional ref omits ref_type",
            transform_query=(
                'SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders") '
                'LEFT JOIN __ref("customers") USING customer_id'
            ),
            expected_error_type=ValueError,
            expected_error_fragment="must declare ref_type for additional ref 'customers'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_additional_ref_without_ref_type_when_compiling_then_it_raises_value_error(
    test_case: CompilePipelineMissingRefTypeTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(test_case.transform_query)

    with pytest.raises(test_case.expected_error_type, match=test_case.expected_error_fragment):
        compile_test_pipeline(loaded_pipeline)


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineRepeatedSourceRefTestCase(
            description="compiles repeated driving source refs without ref_type annotations",
            transform_query=(
                "SELECT left_side.order_id::UInt64 AS order_id "
                'FROM __ref("orders") AS left_side '
                'INNER JOIN __ref("orders") AS right_side USING order_id'
            ),
            expected_target_name="orders_enriched",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_repeated_driving_source_refs_when_compiling_then_it_treats_them_as_one_source(
    test_case: CompilePipelineRepeatedSourceRefTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(test_case.transform_query)

    compiled_pipeline: CompiledPipeline = compile_test_pipeline(loaded_pipeline)

    compiled_model: CompiledTableModel = cast(CompiledTableModel, compiled_pipeline.models[0])
    assert compiled_model.transform.name == test_case.expected_target_name


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInvalidTransformSqlTestCase(
            description="raises a clear custom error for top level union output",
            transform_query="""
                SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
                UNION ALL
                SELECT CAST(order_id AS UInt64) AS order_id FROM replay_orders
            """,
            expected_error_type=TransformSqlTopLevelSetOperationError,
            expected_message_fragments=(
                "outermost SELECT",
                "UNION or UNION ALL",
                "expr::Type AS name",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_inline_transform_sql_when_compiling_then_it_raises_a_clear_contract_error(
    test_case: CompilePipelineInvalidTransformSqlTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_inline_sql_pipeline(test_case.transform_query)

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    error_message: str = str(error_info.value)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInvalidOrderByTestCase(
            description=(
                "raises a clear custom error for order by expressions "
                "that reference missing columns"
            ),
            transform_query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")',
            order_by=("toYYYYMM(created_at)",),
            expected_error_type=TransformOrderByUnknownColumnError,
            expected_message_fragments=(
                "invalid ORDER BY expression",
                "created_at",
                "Available columns: order_id",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_transform_order_by_when_compiling_then_it_raises_a_clear_contract_error(
    test_case: CompilePipelineInvalidOrderByTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_invalid_order_by_pipeline(
        test_case.transform_query,
        list(test_case.order_by),
    )

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    error_message: str = str(error_info.value)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineAdditionalRefDependencyTestCase(
            description="tracks additional reference tables as materialized view dependencies",
            query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders") LEFT JOIN __ref("customers", ref_type="reference") '
                "USING customer_id"
            ),
            expected_materialized_view_deps=(
                "raw__orders",
                "tbl__customers",
                "tbl__orders_enriched",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_additional_ref_when_compiling_then_materialized_view_depends_on_the_reference_table(
    test_case: CompilePipelineAdditionalRefDependencyTestCase,
) -> None:
    desired_state: DesiredState = build_single_transform_desired_state(
        query=test_case.query,
        supporting_transforms=(
            (
                "customers",
                'SELECT CAST(customer_id AS UInt64) AS customer_id FROM __ref("orders")',
                ("customer_id",),
            ),
        ),
    )
    desired_objects_by_name: dict[str, object] = {
        object_.name: object_ for object_ in desired_state.objects
    }
    orders_enriched_mv: DesiredMaterializedView = cast(
        DesiredMaterializedView,
        desired_objects_by_name["mv__orders_enriched"],
    )

    assert tuple(dependency.name for dependency in orders_enriched_mv.deps) == (
        test_case.expected_materialized_view_deps
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineSqlModelDefaultOrderByTestCase(
            description=(
                "raises a clear order by error when the default replay timestamp key is missing"
            ),
            sql_contents="""
            MODEL ();

            SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
            """,
            expected_error_type=TransformOrderByUnknownColumnError,
            expected_message_fragments=(
                "invalid ORDER BY expression",
                "_replay_timestamp",
                "Available columns: order_id",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sql_model_without_replay_timestamp_when_compiling_then_it_raises_order_by_error(
    test_case: CompilePipelineSqlModelDefaultOrderByTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_pipeline_project(
        project_dir=tmp_path,
        source_contents="""
        source:
          name: orders
          kind: kafka
          broker_list: kafka:9092
          topic: source.orders
          replay_boundary:
            mode: offsets
        """,
        model_contents=test_case.sql_contents,
    )
    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    error_message: str = str(error_info.value)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInvalidPartitionByTestCase(
            description=(
                "raises a clear custom error for partition by expressions "
                "that reference missing columns"
            ),
            transform_query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")',
            partition_by="toYYYYMM(created_at)",
            expected_error_type=TransformPartitionByUnknownColumnError,
            expected_message_fragments=(
                "invalid PARTITION BY expression",
                "created_at",
                "Available columns: order_id",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_transform_partition_by_when_compiling_then_it_raises_a_clear_contract_error(
    test_case: CompilePipelineInvalidPartitionByTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_invalid_storage_expression_pipeline(
        transform_query=test_case.transform_query,
        order_by=["order_id"],
        partition_by=test_case.partition_by,
    )

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    error_message: str = str(error_info.value)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message


@pytest.mark.parametrize(
    "test_case",
    [
        CompilePipelineInvalidTtlTestCase(
            description=(
                "raises a clear custom error for ttl expressions that reference missing columns"
            ),
            transform_query='SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")',
            ttl="toDateTime(created_at) + INTERVAL 30 DAY",
            expected_error_type=TransformTtlUnknownColumnError,
            expected_message_fragments=(
                "invalid TTL expression",
                "created_at",
                "Available columns: order_id",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_transform_ttl_when_compiling_then_it_raises_a_clear_contract_error(
    test_case: CompilePipelineInvalidTtlTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = build_invalid_storage_expression_pipeline(
        transform_query=test_case.transform_query,
        order_by=["order_id"],
        ttl=test_case.ttl,
    )

    with pytest.raises(test_case.expected_error_type) as error_info:
        compile_test_pipeline(loaded_pipeline)

    error_message: str = str(error_info.value)
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in error_message


@pytest.mark.parametrize(
    "test_case",
    [
        CompileModelNamingTestCase(
            description="resolves exact pipeline project and built-in kind naming precedence",
            project_contents="""
            name = "naming"
            default_target = "test"

            [naming]
            table_prefix = "project_tbl__"
            view_prefix = "project_view__"

            [targets.test]
            """,
            pipeline_contents="""
            [naming]
            table_prefix = "pipeline_tbl__"
            """,
            model_files={
                "first.sql": (
                    "MODEL (relation_name exact_table, order_by [order_id]); "
                    "SELECT order_id::UInt64 AS order_id "
                    'FROM __source("orders")'
                ),
                "second.sql": (
                    "MODEL (order_by [order_id]); "
                    'SELECT order_id::UInt64 AS order_id FROM __ref("first")'
                ),
                "exact_view.sql": (
                    "MODEL (kind view, relation_name exact_view); "
                    'SELECT order_id::UInt64 AS order_id FROM __ref("second")'
                ),
                "summary.sql": (
                    "MODEL (kind view); SELECT payment_id::UInt64 AS payment_id FROM "
                    '__source("payments") JOIN __ref("second") ON 1 = 1'
                ),
            },
            expected_relation_names={
                "exact_view": "exact_view",
                "first": "exact_table",
                "second": "pipeline_tbl__second",
                "summary": "project_view__summary",
            },
            expected_model_kinds={
                "exact_view": "view",
                "first": "table",
                "second": "table",
                "summary": "view",
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_model_relation_naming_layers_when_compiling_then_resolves_locked_precedence(
    test_case: CompileModelNamingTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(project_dir=tmp_path, contents=test_case.project_contents)
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents="""
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
          - name: payments
            kind: kafka
            broker_list: kafka:9092
            topic: source.payments
            replay_boundary: {mode: offsets}
        """,
    )
    pipeline_dir: Path = tmp_path / "pipelines" / "pl__models"
    write_pipeline_file(pipeline_dir / "pipeline.toml", test_case.pipeline_contents)
    file_name: str
    contents: str
    for file_name, contents in test_case.model_files.items():
        write_pipeline_file(pipeline_dir / file_name, contents)

    project: CompiledProject = compile_logical_project(tmp_path)

    assert {model.key.name: model.relation_name for model in project.models} == (
        test_case.expected_relation_names
    )
    assert {model.key.name: model.kind for model in project.models} == (
        test_case.expected_model_kinds
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileModelTtlDefaultTestCase(
            description="project model TTL supplies an omitted model value",
            model_ttl_header="",
            expected_ttl="event_at + INTERVAL 7 DAY",
        ),
        CompileModelTtlDefaultTestCase(
            description="model TTL overrides the project default",
            model_ttl_header=', ttl "event_at + INTERVAL 1 DAY"',
            expected_ttl="event_at + INTERVAL 1 DAY",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_project_model_ttl_when_compiling_then_applies_model_precedence(
    test_case: CompileModelTtlDefaultTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "ttl_defaults"
        default_target = "test"

        [defaults]
        model_ttl = "event_at + INTERVAL 7 DAY"

        [targets.test]
        """,
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - name: events
            kind: kafka
            broker_list: kafka:9092
            topic: source.events
            replay_boundary: {mode: offsets}
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__events" / "events_table.sql",
        (
            f"MODEL (order_by [event_at]{test_case.model_ttl_header}); "
            "SELECT CAST(_replay_timestamp AS DateTime64(3)) AS event_at, "
            "CAST(_replay_partition AS Int32) AS _replay_partition, "
            "CAST(_replay_offset AS Int64) AS _replay_offset, "
            "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp, "
            "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at "
            'FROM __source("events")'
        ),
    )

    project: CompiledProject = compile_logical_project(tmp_path)
    model: CompiledTableModel = cast(CompiledTableModel, project.models[0])

    assert model.transform.ttl == test_case.expected_ttl


@pytest.mark.parametrize(
    "test_case",
    [
        CompileModelRetentionTestCase(
            description="project typed retention applies to compatible model columns",
            pipeline_defaults="",
            model_retention_header="",
            timestamp_projection=("CAST(_replay_timestamp AS DateTime64(3)) AS kafka_timestamp, "),
            when_missing="error",
            expected_ttl="least(kafka_timestamp, ingested_at) + INTERVAL 7 DAY",
            expected_origin="project",
            expected_applied=True,
        ),
        CompileModelRetentionTestCase(
            description="pipeline typed retention overrides the project policy",
            pipeline_defaults=(
                '[defaults.models.retention]\nduration = "30d"\n'
                'timestamp_column = "kafka_timestamp"\ncap_at_column = "ingested_at"\n'
            ),
            model_retention_header="",
            timestamp_projection=("CAST(_replay_timestamp AS DateTime64(3)) AS kafka_timestamp, "),
            when_missing="error",
            expected_ttl="least(kafka_timestamp, ingested_at) + INTERVAL 30 DAY",
            expected_origin="pipeline",
            expected_applied=True,
        ),
        CompileModelRetentionTestCase(
            description="model typed retention overrides pipeline and project policies",
            pipeline_defaults=(
                '[defaults.models.retention]\nduration = "30d"\n'
                'timestamp_column = "kafka_timestamp"\ncap_at_column = "ingested_at"\n'
            ),
            model_retention_header=(
                ', retention (duration "2d", timestamp_column kafka_timestamp, '
                "cap_at_column ingested_at)"
            ),
            timestamp_projection=("CAST(_replay_timestamp AS DateTime64(3)) AS kafka_timestamp, "),
            when_missing="error",
            expected_ttl="least(kafka_timestamp, ingested_at) + INTERVAL 2 DAY",
            expected_origin="model",
            expected_applied=True,
        ),
        CompileModelRetentionTestCase(
            description="explicit disabled retention blocks inherited policies",
            pipeline_defaults="",
            model_retention_header=", retention false",
            timestamp_projection=("CAST(_replay_timestamp AS DateTime64(3)) AS kafka_timestamp, "),
            when_missing="error",
            expected_ttl=None,
            expected_origin="model",
            expected_applied=False,
        ),
        CompileModelRetentionTestCase(
            description="skip policy leaves an incompatible model without retention",
            pipeline_defaults="",
            model_retention_header="",
            timestamp_projection="",
            when_missing="skip",
            expected_ttl=None,
            expected_origin="project",
            expected_applied=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_typed_retention_when_compiling_then_resolves_schema_aware_precedence(
    test_case: CompileModelRetentionTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "retention_defaults"\ndefault_target = "test"\n\n'
            '[defaults.models.retention]\nduration = "7d"\n'
            'timestamp_column = "kafka_timestamp"\ncap_at_column = "ingested_at"\n'
            f'when_missing = "{test_case.when_missing}"\n\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents=(
            "sources:\n  - name: events\n    kind: kafka\n"
            "    broker_list: kafka:9092\n    topic: source.events\n"
            "    replay_boundary: {mode: offsets}\n"
        ),
    )
    pipeline_dir: Path = tmp_path / "pipelines" / "pl__events"
    write_pipeline_file(pipeline_dir / "pipeline.toml", test_case.pipeline_defaults)
    write_pipeline_file(
        pipeline_dir / "events_table.sql",
        (
            f"MODEL (order_by [_replay_timestamp]{test_case.model_retention_header}); "
            f"SELECT {test_case.timestamp_projection}"
            "CAST(_replay_landed_at AS DateTime64(3)) AS ingested_at, "
            "CAST(_replay_partition AS Int32) AS _replay_partition, "
            "CAST(_replay_offset AS Int64) AS _replay_offset, "
            "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp, "
            "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at "
            'FROM __source("events")'
        ),
    )

    project: CompiledProject = compile_logical_project(tmp_path)
    model: CompiledTableModel = cast(CompiledTableModel, project.models[0])

    assert model.transform.ttl == test_case.expected_ttl
    assert str(model.retention.origin) == test_case.expected_origin
    assert model.retention_applied is test_case.expected_applied


@pytest.mark.parametrize(
    "test_case",
    [
        CompileRetentionErrorTestCase(
            description="required project retention timestamp is absent",
            timestamp_projection="",
            expected_error_fragment=(
                "Model 'events_table' retention requires missing output columns: kafka_timestamp"
            ),
        ),
        CompileRetentionErrorTestCase(
            description="project retention timestamp has an incompatible type",
            timestamp_projection="CAST('bad' AS String) AS kafka_timestamp, ",
            expected_error_fragment=(
                "Model 'events_table' retention columns must be non-null Date or DateTime "
                r"values: kafka_timestamp \(String\)"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_required_retention_column_missing_when_compiling_then_fails_with_model_context(
    test_case: CompileRetentionErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "retention_error"\ndefault_target = "test"\n\n'
            '[defaults.models.retention]\nduration = "7d"\n'
            'timestamp_column = "kafka_timestamp"\nwhen_missing = "error"\n\n'
            "[targets.test]\n"
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents=(
            "sources:\n  - name: events\n    kind: kafka\n"
            "    broker_list: kafka:9092\n    topic: source.events\n"
            "    replay_boundary: {mode: offsets}\n"
        ),
    )
    pipeline_dir: Path = tmp_path / "pipelines" / "pl__events"
    write_pipeline_file(
        pipeline_dir / "events_table.sql",
        (
            "MODEL (order_by [_replay_timestamp]); SELECT "
            f"{test_case.timestamp_projection}"
            "CAST(_replay_partition AS Int32) AS _replay_partition, "
            "CAST(_replay_offset AS Int64) AS _replay_offset, "
            "CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp, "
            "CAST(_replay_landed_at AS DateTime64(3)) AS _replay_landed_at "
            'FROM __source("events")'
        ),
    )

    with pytest.raises(
        PipelineCompileError,
        match=test_case.expected_error_fragment,
    ) as error_info:
        compile_logical_project(tmp_path)

    assert error_info.value.diagnostic is not None
    assert error_info.value.diagnostic.resource_name == "events_table"
    assert error_info.value.diagnostic.location is not None
    assert error_info.value.diagnostic.location.path == pipeline_dir / "events_table.sql"


@pytest.mark.parametrize(
    "test_case",
    [
        ProgrammaticDependencyScopeTestCase(
            description="programmatic project retains pipeline reference scope",
            configured_scope="pipeline",
            expected_scope="pipeline",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_programmatic_project_without_effective_config_when_building_then_scope_remains(
    test_case: ProgrammaticDependencyScopeTestCase,
    tmp_path: Path,
) -> None:
    source_file: DiscoveredProjectFile = DiscoveredProjectFile(
        file_path=tmp_path / "sqltide_project.toml",
        relative_path=Path("sqltide_project.toml"),
        contents="",
    )
    loaded_project: LoadedProject = LoadedProject(
        project=Project(
            dependencies=ProjectDependencies(model_reference_scope=test_case.configured_scope)
        ),
        source_file=source_file,
    )
    inputs: CompileProjectInputs = build_compile_inputs(
        discovered_inputs=DiscoveredProjectInputs(
            project_dir=tmp_path,
            loaded_project=loaded_project,
            source_files=(),
            pipeline_directories=(),
            model_files=(),
            test_files=(),
            audit_files=(),
            macro_files=(),
        ),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    assert inputs.model_reference_scope == ModelReferenceScope(test_case.expected_scope)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileRelationNameErrorTestCase(
            description="rejects an empty exact relation name",
            relation_name="",
            expected_error_fragment="expected a non-empty unqualified identifier",
        ),
        CompileRelationNameErrorTestCase(
            description="rejects a qualified exact relation name",
            relation_name="analytics.orders",
            expected_error_fragment="expected a non-empty unqualified identifier",
        ),
        CompileRelationNameErrorTestCase(
            description="rejects the reserved Kafka prefix",
            relation_name="kafka__orders",
            expected_error_fragment="uses reserved prefix",
        ),
        CompileRelationNameErrorTestCase(
            description="rejects the reserved raw prefix",
            relation_name="raw__orders",
            expected_error_fragment="uses reserved prefix",
        ),
        CompileRelationNameErrorTestCase(
            description="rejects the reserved materialized-view prefix",
            relation_name="mv__orders",
            expected_error_fragment="uses reserved prefix",
        ),
        CompileRelationNameErrorTestCase(
            description="rejects a fixed deployment suffix lookalike",
            relation_name="orders__20260731T120000Z_abcdef",
            expected_error_fragment="looks like a fixed deployment-suffixed physical name",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_effective_relation_name_when_compiling_then_rejects_before_realization(
    test_case: CompileRelationNameErrorTestCase,
) -> None:
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=Pipeline(
            name="views",
            source=None,
            transforms=(
                ViewStep(
                    name="orders",
                    relation_name=test_case.relation_name,
                    query="SELECT 1::UInt8 AS value",
                ),
            ),
        ),
        file_path=Path("pipelines/views"),
    )

    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        compile_test_pipeline(loaded_pipeline)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileRelationNameErrorTestCase(
            description="rejects a reserved prefix inherited from project naming",
            relation_name="raw__",
            expected_error_fragment="uses reserved prefix",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reserved_project_table_prefix_when_compiling_then_rejects_effective_name(
    test_case: CompileRelationNameErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=f"""
        name = "reserved_naming"
        default_target = "test"

        [naming]
        table_prefix = "{test_case.relation_name}"

        [targets.test]
        """,
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - name: events
            kind: kafka
            broker_list: kafka:9092
            topic: source.events
            replay_boundary: {mode: offsets}
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "pl__events" / "clean.sql",
        'MODEL (); SELECT kafka_value::String AS value FROM __source("events")',
    )

    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        compile_logical_project(tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileRelationCollisionTestCase(
            description="rejects duplicate model relation names project-wide",
            model_files={
                "first.sql": (
                    "MODEL (kind view, relation_name shared_output); SELECT 1::UInt8 AS value"
                ),
                "second.sql": (
                    "MODEL (kind view, relation_name shared_output); SELECT 2::UInt8 AS value"
                ),
            },
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
            """,
            expected_error_fragment="Relation name 'shared_output' is used by both",
        ),
        CompileRelationCollisionTestCase(
            description="rejects adopted source collision with generated Kafka table in VDE mode",
            model_files={"consumer.sql": "MODEL (kind view); SELECT 1::UInt8 AS value"},
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: adopted_orders
                kind: stream_table
                table_name: kafka__orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
            """,
            expected_error_fragment="Relation name 'kafka__orders' is used by both",
        ),
        CompileRelationCollisionTestCase(
            description="rejects adopted source collision with generated raw table in VDE mode",
            model_files={"consumer.sql": "MODEL (kind view); SELECT 1::UInt8 AS value"},
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: adopted_orders
                kind: stream_table
                table_name: raw__orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
            """,
            expected_error_fragment="Relation name 'raw__orders' is used by both",
        ),
        CompileRelationCollisionTestCase(
            description=(
                "rejects adopted source collision with generated materialized view in VDE mode"
            ),
            model_files={"consumer.sql": "MODEL (kind view); SELECT 1::UInt8 AS value"},
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: adopted_orders
                kind: stream_table
                table_name: mv__orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
            """,
            expected_error_fragment="Relation name 'mv__orders' is used by both",
        ),
        CompileRelationCollisionTestCase(
            description="rejects adopted source collision with authored view relation in VDE mode",
            model_files={
                "consumer.sql": (
                    "MODEL (kind view, relation_name customer_orders); SELECT 1::UInt8 AS value"
                )
            },
            source_contents="""
            sources:
              - name: adopted_orders
                kind: stream_table
                table_name: customer_orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
            """,
            expected_error_fragment="Relation name 'customer_orders' is used by both",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_relation_collision_when_assembling_project_then_rejects_before_graph(
    test_case: CompileRelationCollisionTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[defaults]\npipeline_mode = "virtual"\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents=test_case.source_contents,
    )
    pipeline_dir: Path = tmp_path / "pipelines" / "pl__views"
    file_name: str
    contents: str
    for file_name, contents in test_case.model_files.items():
        write_pipeline_file(pipeline_dir / file_name, contents)

    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        compile_logical_project(tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileAdoptedSourceSharingTestCase(
            description="preserves adopted-to-adopted sharing with identical replay mappings",
            source_contents="""
            sources:
              - name: orders
                kind: stream_table
                table_name: shared_orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
              - name: archived_orders
                kind: stream_table
                table_name: shared_orders
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: event_timestamp}
            """,
            expected_source_table_names=("shared_orders", "shared_orders"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_consistent_shared_adopted_relation_when_compiling_then_preserves_sharing(
    test_case: CompileAdoptedSourceSharingTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "test"\ndefault_target = "test"\n[targets.test]\n',
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents=test_case.source_contents,
    )

    project: CompiledProject = compile_logical_project(tmp_path)

    assert (
        tuple(cast(ExternalTableSourceStep, source.source).table_name for source in project.sources)
        == test_case.expected_source_table_names
    )
