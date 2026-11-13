import pytest

from sqltide._native import analyze_catalog_sql, parse_expression_list, sql_render_string_literal
from sqltide.compiler.sql_analysis.models import SqlCatalogAnalysis
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    CatalogPhysicalSourcesTestCase,
    CatalogSqlAnalysisTestCase,
    SqlExpressionListTestCase,
    SqlStringLiteralTestCase,
)

# Captured verbatim from ClickHouse 24.8 system.tables on 2026-07-28.
_CAPTURED_CREATE_TABLE_QUERY: str = (
    "CREATE TABLE slice12b.tbl__orders__dep_a (`order_id` String, "
    "`updated_at` DateTime64(3) DEFAULT now64(3)) ENGINE = "
    "ReplacingMergeTree(updated_at) PARTITION BY toYYYYMM(updated_at) "
    "ORDER BY (order_id, updated_at) TTL toDateTime(updated_at) + "
    "toIntervalDay(30) SETTINGS index_granularity = 8192"
)
_CAPTURED_CREATE_MATERIALIZED_VIEW_QUERY: str = (
    "CREATE MATERIALIZED VIEW slice12b.mv__orders TO "
    "slice12b.tbl__orders__dep_a (`order_id` String, "
    "`updated_at` DateTime64(3)) AS SELECT payload AS order_id, "
    "now64(3) AS updated_at FROM slice12b.kafka__orders"
)
_CAPTURED_STABLE_VIEW_AS_SELECT: str = "SELECT * FROM slice12b.tbl__orders__dep_a"
# Persisted by a real staged deployment in planner integration coverage.
_PERSISTED_NORMALIZED_QUERY: str = (
    "SELECT\n"
    "  CAST(kafka_key AS String) AS order_id,\n"
    "  CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp\n"
    "FROM raw__orders"
)


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogSqlAnalysisTestCase(
            description="extracts table ttl actions and settings",
            sql=(
                "CREATE TABLE analytics.events (id UInt64, observed_at DateTime64(3)) "
                "ENGINE = MergeTree ORDER BY id "
                "TTL observed_at + INTERVAL 7 DAY DELETE, "
                "observed_at + INTERVAL 30 DAY TO VOLUME 'cold' "
                "SETTINGS index_granularity = 8192, storage_policy = 'hot'"
            ),
            expected_query_fragment="None",
            expected_first_source=(None, None),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=(
                "observed_at + INTERVAL '7' DAY DELETE, "
                "observed_at + INTERVAL '30' DAY TO VOLUME 'cold'"
            ),
            expected_settings=(
                ("index_granularity", "8192"),
                ("storage_policy", "'hot'"),
            ),
        ),
        CatalogSqlAnalysisTestCase(
            description="parses captured clickhouse create table query",
            sql=_CAPTURED_CREATE_TABLE_QUERY,
            expected_query_fragment="None",
            expected_first_source=(None, None),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl="toDateTime(updated_at) + toIntervalDay(30)",
            expected_settings=(("index_granularity", "8192"),),
        ),
        CatalogSqlAnalysisTestCase(
            description="keeps a ttl where clause instead of dropping it",
            sql=(
                "CREATE TABLE analytics.events (`at` DateTime, `x` UInt8) ENGINE = MergeTree "
                "ORDER BY at TTL at + toIntervalMonth('1'), at + toIntervalDay(7) WHERE x = 1 "
                "SETTINGS index_granularity = 8192"
            ),
            expected_query_fragment="None",
            expected_first_source=(None, None),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl="at + toIntervalMonth('1'), at + toIntervalDay(7) WHERE x = 1",
            expected_settings=(("index_granularity", "8192"),),
        ),
        CatalogSqlAnalysisTestCase(
            description="parses captured clickhouse materialized view query",
            sql=_CAPTURED_CREATE_MATERIALIZED_VIEW_QUERY,
            expected_query_fragment="FROM slice12b.kafka__orders",
            expected_first_source=("slice12b", "kafka__orders"),
            expected_direct_source=(None, None),
            expected_target_relation=("slice12b", "tbl__orders__dep_a"),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="parses captured clickhouse stable view as select",
            sql=_CAPTURED_STABLE_VIEW_AS_SELECT,
            expected_query_fragment="FROM slice12b.tbl__orders__dep_a",
            expected_first_source=("slice12b", "tbl__orders__dep_a"),
            expected_direct_source=("slice12b", "tbl__orders__dep_a"),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="parses persisted normalized deployment query",
            sql=_PERSISTED_NORMALIZED_QUERY,
            expected_query_fragment="FROM raw__orders",
            expected_first_source=(None, "raw__orders"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="extracts quoted materialized view target and CTE physical source",
            sql=(
                "CREATE MATERIALIZED VIEW `db-x`.`mv.dot` "
                "TO `target-db`.`target.dot` AS "
                "WITH `source alias` AS (SELECT * FROM `raw-db`.`source.table`) "
                "SELECT * FROM `source alias`"
            ),
            expected_query_fragment='WITH "source alias" AS',
            expected_first_source=("raw-db", "source.table"),
            expected_direct_source=(None, None),
            expected_target_relation=("target-db", "target.dot"),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="extracts quoted direct stable binding without splitting dotted names",
            sql="SELECT * FROM `db-name`.`table.with.dot`",
            expected_query_fragment='FROM "db-name"."table.with.dot"',
            expected_first_source=("db-name", "table.with.dot"),
            expected_direct_source=("db-name", "table.with.dot"),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="resolves the referenced cte before unused ctes and outer joins",
            sql=(
                "WITH unused AS (SELECT * FROM wrong), "
                "driving AS (SELECT * FROM raw) "
                "SELECT * FROM driving JOIN side USING (id)"
            ),
            expected_query_fragment="FROM driving JOIN side",
            expected_first_source=(None, "raw"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="resolves a chained referenced cte to its physical source",
            sql=(
                "WITH first_source AS (SELECT * FROM raw), "
                "second_source AS (SELECT * FROM first_source) "
                "SELECT * FROM second_source JOIN side USING (id)"
            ),
            expected_query_fragment="FROM second_source JOIN side",
            expected_first_source=(None, "raw"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a projected query as a stable binding",
            sql="SELECT id FROM tbl",
            expected_query_fragment="SELECT id FROM tbl",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a filtered query as a stable binding",
            sql="SELECT * FROM tbl WHERE active",
            expected_query_fragment="WHERE active",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a prefiltered query as a stable binding",
            sql="SELECT * FROM tbl PREWHERE active",
            expected_query_fragment="PREWHERE active",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a sampled table query as a stable binding",
            sql="SELECT * FROM tbl SAMPLE 0.1",
            expected_query_fragment="SAMPLE 0.1",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a grouped query as a stable binding",
            sql="SELECT * FROM tbl GROUP BY id",
            expected_query_fragment="GROUP BY id",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify an ordered query as a stable binding",
            sql="SELECT * FROM tbl ORDER BY id",
            expected_query_fragment="ORDER BY id",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a limited query as a stable binding",
            sql="SELECT * FROM tbl LIMIT 1",
            expected_query_fragment="LIMIT 1",
            expected_first_source=(None, "tbl"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify cte sql as a stable binding",
            sql="WITH source AS (SELECT * FROM raw) SELECT * FROM source",
            expected_query_fragment="SELECT * FROM source",
            expected_first_source=(None, "raw"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
        CatalogSqlAnalysisTestCase(
            description="does not classify a set query as a stable binding",
            sql="SELECT * FROM left_table UNION ALL SELECT * FROM right_table",
            expected_query_fragment="UNION ALL",
            expected_first_source=(None, "left_table"),
            expected_direct_source=(None, None),
            expected_target_relation=(None, None),
            expected_ttl=None,
            expected_settings=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_catalog_sql_when_analyzing_then_returns_expected_semantic_facts(
    test_case: CatalogSqlAnalysisTestCase,
) -> None:
    result: SqlCatalogAnalysis = analyze_catalog_sql(sql=test_case.sql, dialect="clickhouse")
    first_source: tuple[str | None, str | None] = (
        getattr(result.first_source, "database", None),
        getattr(result.first_source, "name", None),
    )
    direct_source: tuple[str | None, str | None] = (
        getattr(result.direct_source, "database", None),
        getattr(result.direct_source, "name", None),
    )
    target_relation: tuple[str | None, str | None] = (
        getattr(result.target_relation, "database", None),
        getattr(result.target_relation, "name", None),
    )

    assert test_case.expected_query_fragment in str(result.query_sql)
    assert first_source == test_case.expected_first_source
    assert direct_source == test_case.expected_direct_source
    assert target_relation == test_case.expected_target_relation
    assert result.ttl == test_case.expected_ttl
    assert result.settings == test_case.expected_settings


@pytest.mark.parametrize(
    "test_case",
    [
        CatalogPhysicalSourcesTestCase(
            description="resolves referenced ctes joins and set branches",
            sql=(
                "WITH unused AS (SELECT * FROM ignored), "
                "left_side AS (SELECT * FROM raw_left JOIN lookup_left USING (id)), "
                "right_side AS (SELECT * FROM raw_right) "
                "SELECT * FROM left_side JOIN right_side USING (id) "
                "JOIN direct_side USING (id) "
                "UNION ALL SELECT * FROM union_side"
            ),
            expected_sources=(
                (None, "raw_left"),
                (None, "lookup_left"),
                (None, "raw_right"),
                (None, "direct_side"),
                (None, "union_side"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_ctes_joins_and_set_query_when_analyzing_then_all_physical_sources_are_returned(
    test_case: CatalogPhysicalSourcesTestCase,
) -> None:
    result: SqlCatalogAnalysis = analyze_catalog_sql(
        sql=test_case.sql,
        dialect="clickhouse",
    )

    sources: tuple[tuple[str | None, str], ...] = tuple(
        (source.database, source.name) for source in result.source_relations
    )

    assert sources == test_case.expected_sources
    assert result.first_source == result.source_relations[0]


@pytest.mark.parametrize(
    "test_case",
    [
        SqlExpressionListTestCase(
            description="splits only top-level sorting expressions",
            sql="(id, cityHash64(concat(x, ',', y)), tuple(z, if(q, 1, 2)))",
            expected_expressions=(
                "id",
                "cityHash64(concat(x, ',', y))",
                "tuple(z, IF(q, 1, 2))",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_nested_expression_list_when_parsing_then_preserves_inner_commas(
    test_case: SqlExpressionListTestCase,
) -> None:
    result: tuple[str, ...] = parse_expression_list(sql=test_case.sql, dialect="clickhouse")

    assert result == test_case.expected_expressions


@pytest.mark.parametrize(
    "test_case",
    [
        SqlStringLiteralTestCase(
            description="escapes apostrophes",
            value="O'Reilly",
            expected_literal="'O''Reilly'",
        ),
        SqlStringLiteralTestCase(
            description="escapes backslashes",
            value=r"a\b",
            expected_literal=r"'a\\b'",
        ),
        SqlStringLiteralTestCase(
            description="preserves unicode",
            value="cafe雪",
            expected_literal="'cafe雪'",
        ),
        SqlStringLiteralTestCase(
            description="renders empty strings",
            value="",
            expected_literal="''",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_string_value_when_rendering_then_returns_clickhouse_literal(
    test_case: SqlStringLiteralTestCase,
) -> None:
    result: str = sql_render_string_literal(test_case.value, "clickhouse")

    assert result == test_case.expected_literal
