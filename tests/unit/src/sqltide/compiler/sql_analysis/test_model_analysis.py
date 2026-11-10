import pytest

from sqltide._native import SqlModelAnalyzer
from sqltide.adapter.constants import ADAPTER_DATABASE_PLACEHOLDER
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import SqlModelAnalysis, SqlResolvedQuery
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    ModelAggregateAnalysisTestCase,
    ModelAnalysisOrderingTestCase,
    ModelRawRelationTestCase,
    ModelRepeatedResolutionTestCase,
    ModelReservedPlaceholderTestCase,
    ModelResolutionTestCase,
    ModelStorageAnalysisTestCase,
    ModelTypeAnalysisTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ModelTypeAnalysisTestCase(
            description="preserves exact ClickHouse signed wrapper nested and state types",
            sql=(
                "SELECT "
                "CAST(a AS Int8) AS signed_value, "
                "CAST(b AS UInt256) AS unsigned_value, "
                "CAST(c AS Nullable(LowCardinality(String))) AS wrapped_value, "
                "CAST(d AS Array(Tuple(String, UInt64))) AS nested_value, "
                "CAST(e AS Map(String, Decimal(18, 4))) AS mapped_value, "
                "CAST(f AS DateTime64(6, 'UTC')) AS timed_value, "
                "CAST(g AS AggregateFunction(sum, UInt64)) AS state_value, "
                "CAST(h AS SimpleAggregateFunction(sum, UInt64)) AS simple_state_value, "
                "i::String AS string_value, "
                "j::Array(String) AS string_array, "
                "k::Date AS date_value "
                "FROM raw_values"
            ),
            expected_columns=(
                ("signed_value", "Int8"),
                ("unsigned_value", "UInt256"),
                ("wrapped_value", "Nullable(LowCardinality(String))"),
                ("nested_value", "Array(Tuple(String, UInt64))"),
                ("mapped_value", "Map(String, Decimal(18, 4))"),
                ("timed_value", "DateTime64(6, 'UTC')"),
                ("state_value", "AggregateFunction(sum, UInt64)"),
                ("simple_state_value", "SimpleAggregateFunction(sum, UInt64)"),
                ("string_value", "String"),
                ("string_array", "Array(String)"),
                ("date_value", "Date"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_type_corpus_when_analyzing_then_preserves_exact_types(
    test_case: ModelTypeAnalysisTestCase,
) -> None:
    analysis: SqlModelAnalysis = SqlModelAnalyzer(dialect="clickhouse").analyze(
        sql=test_case.sql,
        engine="MergeTree()",
        order_by=("signed_value",),
        partition_by=None,
        ttl=None,
    )

    assert tuple((column.name, column.type) for column in analysis.output_columns) == (
        test_case.expected_columns
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ModelAggregateAnalysisTestCase(
            description="recognizes ClickHouse If and State combinators",
            sql=(
                "SELECT CAST(sumIfState(amount, accepted) AS AggregateFunction(sum, UInt64)) "
                "AS total_state FROM raw_values"
            ),
            engine="MergeTree()",
            expected_order_by="total_state",
            expected_function_names=("sumIfState",),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=False,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="recognizes parameterized quantile aggregates",
            sql=("SELECT CAST(quantile(0.9)(amount) AS Float64) AS p90 FROM raw_values"),
            engine="MergeTree()",
            expected_order_by="p90",
            expected_function_names=("quantile",),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=False,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="recognizes the ClickHouse 24.8 aggregate family corpus",
            sql=(
                "SELECT "
                "CAST(quantileExact(0.9)(amount) AS Float64) AS exact_p90, "
                "CAST(groupArraySample(10)(amount) AS Array(UInt64)) AS sample, "
                "CAST(analysisOfVariance(amount, cohort) AS Tuple(Float64, Float64)) AS anova "
                "FROM raw_values"
            ),
            engine="MergeTree()",
            expected_order_by="exact_p90",
            expected_function_names=(
                "quantileExact",
                "groupArraySample",
                "analysisOfVariance",
            ),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=False,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="recognizes finalize aggregation state consumption",
            sql=(
                "SELECT CAST(finalizeAggregation(total_state) AS UInt64) AS total FROM raw_values"
            ),
            engine="MergeTree()",
            expected_order_by="total",
            expected_function_names=("finalizeAggregation",),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=False,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="retains group by and exact aggregating engine semantics",
            sql="SELECT CAST(region AS String) AS region FROM raw_values GROUP BY region",
            engine="AggregatingMergeTree()",
            expected_order_by="region",
            expected_function_names=(),
            expected_has_group_by=True,
            expected_engine_has_aggregate_semantics=True,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="retains replicated aggregating engine semantics",
            sql="SELECT CAST(region AS String) AS region FROM raw_values",
            engine=(
                "ReplicatedAggregatingMergeTree('/clickhouse/tables/{shard}/events', '{replica}')"
            ),
            expected_order_by="region",
            expected_function_names=(),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=True,
            expected_has_semantics=True,
        ),
        ModelAggregateAnalysisTestCase(
            description="retains replicated summing engine semantics",
            sql="SELECT CAST(region AS String) AS region FROM raw_values",
            engine="ReplicatedSummingMergeTree('/clickhouse/tables/events', '{replica}')",
            expected_order_by="region",
            expected_function_names=(),
            expected_has_group_by=False,
            expected_engine_has_aggregate_semantics=True,
            expected_has_semantics=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_aggregate_forms_when_analyzing_then_returns_complete_facts(
    test_case: ModelAggregateAnalysisTestCase,
) -> None:
    analysis: SqlModelAnalysis = SqlModelAnalyzer(dialect="clickhouse").analyze(
        sql=test_case.sql,
        engine=test_case.engine,
        order_by=(test_case.expected_order_by,),
        partition_by=None,
        ttl=None,
    )

    assert analysis.aggregate_facts.function_names == test_case.expected_function_names
    assert analysis.aggregate_facts.has_group_by is test_case.expected_has_group_by
    assert (
        analysis.aggregate_facts.engine_has_aggregate_semantics
        is test_case.expected_engine_has_aggregate_semantics
    )
    assert analysis.aggregate_facts.has_semantics is test_case.expected_has_semantics


@pytest.mark.parametrize(
    "test_case",
    [
        ModelStorageAnalysisTestCase(
            description="analyzes order partition and ttl expressions once into one record",
            sql=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(created_at AS DateTime64(3)) AS created_at FROM raw_values"
            ),
            order_by=("order_id", "toYYYYMM(created_at)"),
            partition_by="toYYYYMM(created_at)",
            ttl="created_at + INTERVAL 30 DAY",
            expected_storage_facts=(
                ("order_by", "order_id", ("order_id",)),
                ("order_by", "toYYYYMM(created_at)", ("created_at",)),
                ("partition_by", "toYYYYMM(created_at)", ("created_at",)),
                ("ttl", "created_at + INTERVAL 30 DAY", ("created_at",)),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_storage_contract_when_analyzing_then_retains_canonical_reference_facts(
    test_case: ModelStorageAnalysisTestCase,
) -> None:
    analysis: SqlModelAnalysis = SqlModelAnalyzer(dialect="clickhouse").analyze(
        sql=test_case.sql,
        engine="MergeTree()",
        order_by=test_case.order_by,
        partition_by=test_case.partition_by,
        ttl=test_case.ttl,
    )

    assert (
        tuple(
            (expression.kind, expression.sql, expression.referenced_column_names)
            for expression in analysis.storage_expressions
        )
        == test_case.expected_storage_facts
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ModelResolutionTestCase(
            description="qualifies physical CTE inputs without qualifying the CTE identity",
            sql=(
                'WITH staged AS (SELECT order_id FROM __source("orders")) '
                "SELECT CAST(order_id AS UInt64) AS order_id FROM staged"
            ),
            resolver={"orders": "raw__orders"},
            expected_fragments=(
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.raw__orders",
                "FROM staged",
            ),
            expected_absent_fragments=(
                f"{ADAPTER_DATABASE_PLACEHOLDER}.staged",
                "__source",
            ),
        ),
        ModelResolutionTestCase(
            description="preserves authored ClickHouse function spellings in the executed template",
            sql=(
                "SELECT CAST(event_id AS UInt64) AS order_id, "
                "CAST(startsWith(topic, 'races') AS Bool) AS is_race, "
                "CAST(length(topic) AS UInt64) AS topic_length, "
                "CAST(position(topic, '.') AS UInt64) AS dot_position "
                "FROM __source(\"events\") WHERE startsWith(topic, 'races')"
            ),
            resolver={"events": "tbl_kafka__events"},
            expected_fragments=(
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl_kafka__events",
                "CAST(startsWith(topic, 'races') AS Bool) AS is_race",
                "CAST(length(topic) AS UInt64) AS topic_length",
                "CAST(position(topic, '.') AS UInt64) AS dot_position",
                "WHERE startsWith(topic, 'races')",
            ),
            expected_absent_fragments=(
                "STARTS_WITH",
                "LENGTH(",
                "POSITION(",
                "__source",
            ),
        ),
        ModelResolutionTestCase(
            description="treats union-scoped subquery CTE names as relations",
            sql=(
                "SELECT CAST(projected.order_id AS UInt64) AS order_id FROM ("
                'WITH grouped AS (SELECT order_id FROM __ref("orders")), '
                'flat AS (SELECT order_id FROM __ref("orders")) '
                "SELECT order_id FROM grouped "
                "UNION ALL SELECT order_id FROM flat"
                ") AS projected"
            ),
            resolver={"orders": "tbl__orders"},
            expected_fragments=(
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl__orders), ",
                "FROM grouped ",
                "UNION ALL SELECT order_id FROM flat",
            ),
            expected_absent_fragments=(
                f"{ADAPTER_DATABASE_PLACEHOLDER}.grouped",
                f"{ADAPTER_DATABASE_PLACEHOLDER}.flat",
                "__ref",
            ),
        ),
        ModelResolutionTestCase(
            description="preserves authored aliases and layout around substituted reference spans",
            sql=(
                "SELECT CAST(o.order_id AS UInt64) AS order_id\n"
                'FROM __ref("orders") AS o\n'
                'JOIN __source("events") AS e ON o.order_id = e.order_id'
            ),
            resolver={"orders": "tbl__orders", "events": "tbl_kafka__events"},
            expected_fragments=(
                f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.tbl__orders AS o\n",
                f"JOIN {ADAPTER_DATABASE_PLACEHOLDER}.tbl_kafka__events AS e ON",
            ),
            expected_absent_fragments=(
                "__ref",
                "__source",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_analyzed_cte_query_when_resolving_then_generates_one_qualified_query(
    test_case: ModelResolutionTestCase,
) -> None:
    analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    analysis: SqlModelAnalysis = analyzer.analyze(
        sql=test_case.sql,
        engine="MergeTree()",
        order_by=("order_id",),
        partition_by=None,
        ttl=None,
    )
    resolved_query: SqlResolvedQuery = analyzer.resolve(
        analysis=analysis,
        resolver=test_case.resolver,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in resolved_query.database_template
    for expected_absent_fragment in test_case.expected_absent_fragments:
        assert expected_absent_fragment not in resolved_query.database_template


@pytest.mark.parametrize(
    "test_case",
    [
        ModelRepeatedResolutionTestCase(
            description="resolves an aliased source reference twice without consuming the analysis",
            sql='SELECT CAST(o.order_id AS UInt64) AS order_id FROM __source("orders") AS o',
            resolver={"orders": "raw__orders"},
            expected_template_fragment=(f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.raw__orders AS o"),
        ),
        ModelRepeatedResolutionTestCase(
            description="resolves an aliased reference inside an authored cte twice",
            sql=(
                'WITH staged AS (SELECT order_id FROM __source("orders") AS o) '
                "SELECT CAST(staged.order_id AS UInt64) AS order_id FROM staged"
            ),
            resolver={"orders": "raw__orders"},
            expected_template_fragment=(f"FROM {ADAPTER_DATABASE_PLACEHOLDER}.raw__orders AS o"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_analyzed_model_when_resolving_twice_then_both_results_match(
    test_case: ModelRepeatedResolutionTestCase,
) -> None:
    analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    analysis: SqlModelAnalysis = analyzer.analyze(
        sql=test_case.sql,
        engine="MergeTree()",
        order_by=("order_id",),
        partition_by=None,
        ttl=None,
    )

    first: SqlResolvedQuery = analyzer.resolve(analysis=analysis, resolver=test_case.resolver)
    second: SqlResolvedQuery = analyzer.resolve(analysis=analysis, resolver=test_case.resolver)

    assert test_case.expected_template_fragment in first.database_template
    assert test_case.expected_template_fragment in second.database_template
    assert first.canonical_sql == second.canonical_sql
    assert first.database_template == second.database_template


@pytest.mark.parametrize(
    "test_case",
    [
        ModelReservedPlaceholderTestCase(
            description="rejects the reserved database placeholder inside authored SQL",
            sql=(
                f"SELECT '{ADAPTER_DATABASE_PLACEHOLDER}.literal' AS reserved_value FROM raw_values"
            ),
            expected_error_fragment="reserved adapter database placeholder",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reserved_database_placeholder_when_canonicalizing_then_it_raises_an_error(
    test_case: ModelReservedPlaceholderTestCase,
) -> None:
    with pytest.raises(SqlAnalysisError, match=test_case.expected_error_fragment):
        SqlModelAnalyzer(dialect="clickhouse").canonicalize_query(sql=test_case.sql)


@pytest.mark.parametrize(
    "test_case",
    [
        ModelRawRelationTestCase(
            description="rejects one raw physical relation in the model FROM clause",
            sql=("SELECT CAST(order_id AS UInt64) AS order_id FROM raw__orders"),
            resolver={},
            expected_error_fragment="must be referenced via __ref",
        ),
        ModelRawRelationTestCase(
            description="rejects one raw physical relation joined beside a resolved reference",
            sql=(
                "SELECT CAST(o.order_id AS UInt64) AS order_id "
                'FROM __ref("orders") AS o '
                "JOIN raw__events AS e ON o.order_id = e.order_id"
            ),
            resolver={"orders": "tbl__orders"},
            expected_error_fragment="must be referenced via __ref",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_raw_model_relation_when_resolving_then_it_raises_an_error(
    test_case: ModelRawRelationTestCase,
) -> None:
    analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    analysis: SqlModelAnalysis = analyzer.analyze(
        sql=test_case.sql,
        engine="MergeTree()",
        order_by=("order_id",),
        partition_by=None,
        ttl=None,
    )

    with pytest.raises(SqlAnalysisError, match=test_case.expected_error_fragment):
        analyzer.resolve(analysis=analysis, resolver=test_case.resolver)


@pytest.mark.parametrize(
    "test_case",
    [
        ModelAnalysisOrderingTestCase(
            description="preserves one-model analysis order below any parallel threshold",
            sql_by_model=("SELECT CAST(0 AS UInt64) AS model_000 FROM raw_values",),
            expected_output_names=("model_000",),
        ),
        ModelAnalysisOrderingTestCase(
            description="preserves authored order for thirty-three sequential analyses",
            sql_by_model=tuple(
                f"SELECT CAST({index} AS UInt64) AS model_{index:03d} FROM raw_values"
                for index in range(33)
            ),
            expected_output_names=tuple(f"model_{index:03d}" for index in range(33)),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_ordered_model_sql_when_analyzing_then_result_order_is_deterministic(
    test_case: ModelAnalysisOrderingTestCase,
) -> None:
    analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    analyses: tuple[SqlModelAnalysis, ...] = tuple(
        analyzer.analyze(
            sql=sql,
            engine="MergeTree()",
            order_by=(expected_name,),
            partition_by=None,
            ttl=None,
        )
        for sql, expected_name in zip(
            test_case.sql_by_model,
            test_case.expected_output_names,
            strict=True,
        )
    )

    assert tuple(analysis.output_columns[0].name for analysis in analyses) == (
        test_case.expected_output_names
    )
