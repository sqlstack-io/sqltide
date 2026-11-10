import pytest

from sqltide._native import build_adapter_resource
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    RenderTableTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    build_table,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RenderTableTestCase(
            description="renders base create table ddl without optional clauses",
            partition_by=None,
            ttl=None,
            settings=None,
            expected_fragments=(
                "CREATE TABLE analytics.tbl__orders_enriched",
                "order_id String",
                "_replay_landed_at DateTime64(3) DEFAULT now64(3)",
                "ENGINE = ReplacingMergeTree(_replay_landed_at)",
                "ORDER BY (order_id, _replay_landed_at)",
            ),
            expected_absent_fragments=("PARTITION BY", "TTL ", "SETTINGS "),
        ),
        RenderTableTestCase(
            description="renders optional partition ttl and sorted settings clauses",
            partition_by="toYYYYMM(_replay_landed_at)",
            ttl="toDateTime(_replay_landed_at) + INTERVAL 30 DAY",
            settings={"index_granularity": "8192", "allow_nullable_key": "1"},
            expected_fragments=(
                "PARTITION BY toYYYYMM(_replay_landed_at)",
                "TTL toDateTime(_replay_landed_at) + INTERVAL 30 DAY",
                "SETTINGS allow_nullable_key = 1, index_granularity = 8192",
            ),
            expected_absent_fragments=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_desired_table_when_rendering_then_it_returns_expected_create_table_ddl(
    test_case: RenderTableTestCase,
) -> None:
    rendered_ddl: str = ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(
            build_table(
                partition_by=test_case.partition_by,
                ttl=test_case.ttl,
                settings=test_case.settings,
            )
        ),
        database="analytics",
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered_ddl
    for expected_absent_fragment in test_case.expected_absent_fragments:
        assert expected_absent_fragment not in rendered_ddl
