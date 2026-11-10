import pytest

from sqltide._native import SqlModelAnalyzer
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.sql_analysis.models import SqlResolvedQuery
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    RenderMaterializedViewTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    build_materialized_view,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RenderMaterializedViewTestCase(
            description="qualifies plain source table reference",
            query="SELECT order_id FROM raw__orders",
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=("SELECT\n  order_id", "FROM analytics.raw__orders"),
        ),
        RenderMaterializedViewTestCase(
            description="qualifies aliased source table reference",
            query="SELECT orders.order_id FROM raw__orders AS orders",
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=(
                "SELECT\n  orders.order_id",
                "FROM analytics.raw__orders AS orders",
            ),
        ),
        RenderMaterializedViewTestCase(
            description="qualifies repeated source table references in nested query",
            query=(
                "SELECT count() FROM raw__orders WHERE order_id IN (SELECT order_id FROM "
                "raw__orders)"
            ),
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=(
                "FROM analytics.raw__orders\nWHERE\n  order_id IN",
                "(\n    SELECT\n      order_id\n    FROM analytics.raw__orders\n  )",
            ),
        ),
        RenderMaterializedViewTestCase(
            description="does not double qualify an already qualified source table",
            query="SELECT order_id FROM analytics.raw__orders",
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=("SELECT\n  order_id", "FROM analytics.raw__orders"),
            expected_absent_fragments=("analytics.analytics.raw__orders",),
        ),
        RenderMaterializedViewTestCase(
            description="does not rewrite string literals containing the source table name",
            query=("SELECT 'raw__orders' AS source_label, order_id FROM raw__orders"),
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=(
                "SELECT\n  'raw__orders' AS source_label,\n  order_id",
                "FROM analytics.raw__orders",
            ),
            expected_absent_fragments=("'analytics.raw__orders' AS source_label",),
        ),
        RenderMaterializedViewTestCase(
            description="qualifies joined managed references in the rendered query",
            query=(
                "SELECT orders.order_id FROM raw__orders AS orders "
                "JOIN tbl__region_lookup AS regions USING region_id"
            ),
            expected_source_reference="analytics.raw__orders",
            expected_target_reference="analytics.tbl__orders_enriched",
            expected_query_fragments=(
                "FROM analytics.raw__orders AS orders",
                "JOIN analytics.tbl__region_lookup AS regions\n  USING (region_id)",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_analyzed_materialized_view_when_rendering_then_it_uses_qualified_query(
    test_case: RenderMaterializedViewTestCase,
) -> None:
    analyzed_query: SqlResolvedQuery = SqlModelAnalyzer(dialect="clickhouse").canonicalize_query(
        sql=test_case.query
    )
    rendered_ddl: str = ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(
            build_materialized_view(
                query=analyzed_query.canonical_sql,
                database_template=analyzed_query.database_template,
            )
        ),
        database="analytics",
    )

    assert "CREATE MATERIALIZED VIEW analytics.mv__orders_enriched" in rendered_ddl
    assert f"TO {test_case.expected_target_reference} AS" in rendered_ddl
    for expected_fragment in test_case.expected_query_fragments:
        assert expected_fragment in rendered_ddl
    for expected_absent_fragment in test_case.expected_absent_fragments:
        assert expected_absent_fragment not in rendered_ddl
    assert test_case.expected_source_reference in rendered_ddl
