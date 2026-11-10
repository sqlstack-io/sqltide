from pathlib import Path

import sqltide._native as _native
from sqltide._native import SqlModelAnalyzer, SqlReferenceRewriter, compile_pipeline
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledSource,
    CompiledTableModel,
    LogicalResourceKey,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    KafkaSettings,
    Pipeline,
    TransformStep,
)
from sqltide.compiler.discovery.types import BoundedReplayFallback, ReplayLineageMode
from sqltide.compiler.sql_analysis.models import (
    SqlAggregateFacts,
    SqlModelAnalysis,
    SqlOutputColumn,
    SqlProjection,
    SqlReference,
    SqlSourceSpan,
)
from sqltide.compiler.sql_analysis.types import SqlQueryShape, SqlRelationType
from sqltide.compiler.test_discovery.models import (
    LoadedSqlTest,
    SqlTestCte,
    SqlTestMock,
    SqlTestModelPayload,
)
from sqltide.compiler.test_discovery.types import SqlTestMode
from sqltide.compiler.testing.models import SqlTestCase
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
    write_project_configuration_and_source,
)
from tests.unit.src.sqltide.compiler.test_discovery.helpers import (
    write_sql_test_file,
)

MODEL_SQL_BY_NAME: tuple[tuple[str, str], ...] = (
    (
        "order_items",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __ref("orders")
        """,
    ),
    (
        "daily_revenue",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total AS Nullable(Float64)) AS line_total
        FROM __ref("order_items")
        """,
    ),
    (
        "revenue_report",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total AS Nullable(Float64)) AS reported_total
        FROM __ref("daily_revenue")
        """,
    ),
    (
        "order_tax",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total * 0.1 AS Nullable(Float64)) AS tax_total
        FROM __ref("order_items")
        """,
    ),
    (
        "order_summary",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(daily.order_id AS String) AS order_id,
          CAST(daily.line_total + tax.tax_total AS Nullable(Float64)) AS total_with_tax
        FROM __ref("daily_revenue") AS daily
        JOIN __ref("order_tax", ref_type='reference') AS tax ON daily.order_id = tax.order_id
        """,
    ),
)


def write_sql_test_project(*, tmp_path: Path, test_file_contents: str) -> Path:
    pipeline_dir: Path = tmp_path / "pipelines" / "order_events"
    write_project_configuration_and_source(project_dir=tmp_path)
    model_name: str
    model_sql: str
    for model_name, model_sql in MODEL_SQL_BY_NAME:
        write_pipeline_file(pipeline_dir / f"{model_name}.sql", model_sql)
    write_sql_test_file(tmp_path / "tests" / "order_events" / "test_case.sql", test_file_contents)
    return pipeline_dir


def build_compiled_pipeline_with_tests(
    *,
    tmp_path: Path,
    test_file_contents: str,
) -> tuple[CompiledPipeline, LoadedSqlTest]:
    pipeline_dir: Path = write_sql_test_project(
        tmp_path=tmp_path, test_file_contents=test_file_contents
    )
    compiled_pipeline: CompiledPipeline = compile_pipeline(
        loaded_pipeline=_native.load_pipeline_directory(pipeline_dir),
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )
    loaded_test: LoadedSqlTest = _native.discover_sql_tests(root=tmp_path / "tests")[0]
    return compiled_pipeline, loaded_test


def build_single_sql_test_case(*, tmp_path: Path, test_file_contents: str) -> SqlTestCase:
    compiled_pipeline: CompiledPipeline
    loaded_test: LoadedSqlTest
    compiled_pipeline, loaded_test = build_compiled_pipeline_with_tests(
        tmp_path=tmp_path,
        test_file_contents=test_file_contents,
    )
    return _native.build_sql_test_cases(
        loaded_tests=(loaded_test,),
        compiled_pipelines=(compiled_pipeline,),
        reference_rewriter=SqlReferenceRewriter(dialect="clickhouse"),
        comparison_renderer=ClickHouseAdapter().render_set_difference_comparison,
        dialect="clickhouse",
    )[0]


def build_deep_chain_test_case(*, model_count: int) -> SqlTestCase:
    source: KafkaLandingStep = KafkaLandingStep(
        name="orders",
        kafka=KafkaSettings(broker_list="localhost:9092", topic="orders"),
    )
    source_key: LogicalResourceKey = LogicalResourceKey(
        resource_type=LogicalResourceType.SOURCE,
        name=source.name,
    )
    compiled_source: CompiledSource = CompiledSource(
        key=source_key,
        source=source,
        effective_replay_lineage_mode=ReplayLineageMode.OFFSETS,
    )
    dependency_names: tuple[str, ...] = (
        source.name,
        *(f"model_{index}" for index in range(model_count - 1)),
    )
    transforms: list[TransformStep] = []
    models: list[CompiledModel] = []
    span: SqlSourceSpan = SqlSourceSpan(0, 15, 1, 1, 1, 16)
    index: int
    for index in range(model_count):
        model_name: str = f"model_{index}"
        dependency_name: str = dependency_names[index]
        query: str = f'SELECT CAST(1 AS UInt64) AS id FROM __ref("{dependency_name}")'
        transform: TransformStep = TransformStep(
            name=model_name,
            source=dependency_name,
            engine="MergeTree",
            order_by=("id",),
            query=query,
        )
        transforms.append(transform)
        models.append(
            CompiledTableModel(
                key=LogicalResourceKey(
                    resource_type=LogicalResourceType.MODEL,
                    name=model_name,
                ),
                pipeline_name="deep_chain",
                relation_name=f"tbl__{model_name}",
                kind="table",
                transform=transform,
                sql_analysis=SqlModelAnalysis(
                    authored_sql=query,
                    shape=SqlQueryShape.SELECT,
                    projections=(
                        SqlProjection(
                            index=0,
                            output=SqlOutputColumn(name="id", type="UInt64"),
                            span=span,
                        ),
                    ),
                    references=(
                        SqlReference(
                            name=dependency_name,
                            relation_type=SqlRelationType.REF,
                            span=span,
                        ),
                    ),
                    storage_expressions=(),
                    aggregate_facts=SqlAggregateFacts(
                        has_group_by=False,
                        function_names=(),
                        engine_name="MergeTree",
                        engine_has_aggregate_semantics=False,
                    ),
                ),
                preserves_required_lineage=True,
                replay_anchor_eligible=True,
                effective_bounded_replay_fallback=BoundedReplayFallback.FULL,
            )
        )
    pipeline: Pipeline = Pipeline(name="deep_chain", source=source, transforms=tuple(transforms))
    payload: SqlTestModelPayload = SqlTestModelPayload(
        mocks=(
            SqlTestMock(
                cte_name="__source__orders",
                name="orders",
                relation_type=SqlRelationType.SOURCE,
                query="SELECT 1 AS id",
            ),
        ),
        expected_targets=(
            SqlTestCte(
                name=f"__expected__model_{model_count - 1}",
                query="SELECT CAST(1 AS UInt64) AS id",
            ),
        ),
        assertions=(),
        assertion_reference_names=(),
    )
    loaded_test: LoadedSqlTest = LoadedSqlTest(
        file_path=Path("/project/tests/deep_chain.sql"),
        mode=SqlTestMode.MODEL,
        authored_ctes=(SqlTestCte(name="__source__orders", query="SELECT 1 AS id"),),
        payload=payload,
    )
    return _native.build_sql_test_cases(
        loaded_tests=(loaded_test,),
        compiled_pipelines=(
            CompiledPipeline(
                pipeline=pipeline,
                project=None,
                file_path=Path("/project/pipelines/deep_chain"),
                effective_replay_lineage_mode=ReplayLineageMode.OFFSETS,
                source=compiled_source,
                models=tuple(models),
            ),
        ),
        reference_rewriter=SqlReferenceRewriter(dialect="clickhouse"),
        comparison_renderer=ClickHouseAdapter().render_set_difference_comparison,
        dialect="clickhouse",
    )[0]


CYCLIC_MODEL_SQL_BY_NAME: tuple[tuple[str, str], ...] = (
    (
        "loop_a",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT CAST(order_id AS String) AS order_id FROM __ref("loop_b")
        """,
    ),
    (
        "loop_b",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT CAST(order_id AS String) AS order_id FROM __ref("loop_a")
        """,
    ),
)


def build_cyclic_sql_test_case(*, tmp_path: Path, test_file_contents: str) -> SqlTestCase:
    pipeline_dir: Path = tmp_path / "pipelines" / "order_events"
    write_project_configuration_and_source(project_dir=tmp_path)
    model_name: str
    model_sql: str
    for model_name, model_sql in CYCLIC_MODEL_SQL_BY_NAME:
        write_pipeline_file(pipeline_dir / f"{model_name}.sql", model_sql)
    write_sql_test_file(tmp_path / "tests" / "order_events" / "test_case.sql", test_file_contents)
    compiled_pipeline: CompiledPipeline = compile_pipeline(
        loaded_pipeline=_native.load_pipeline_directory(pipeline_dir),
        sql_analyzer=SqlModelAnalyzer(dialect="clickhouse"),
    )
    loaded_test: LoadedSqlTest = _native.discover_sql_tests(root=tmp_path / "tests")[0]
    return _native.build_sql_test_cases(
        loaded_tests=(loaded_test,),
        compiled_pipelines=(compiled_pipeline,),
        reference_rewriter=SqlReferenceRewriter(dialect="clickhouse"),
        comparison_renderer=ClickHouseAdapter().render_set_difference_comparison,
        dialect="clickhouse",
    )[0]
