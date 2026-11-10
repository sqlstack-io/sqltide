import pytest

from sqltide._native import build_adapter_resource
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    RenderManagedSourceTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    build_kafka_table,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RenderManagedSourceTestCase(
            description="renders kafka engine ddl for current json-as-string source shape",
            extra_settings=None,
            expected_fragments=(
                "CREATE TABLE analytics.kafka__orders",
                "message String",
                "ENGINE = Kafka",
                "kafka_broker_list = 'kafka:9092'",
                "kafka_topic_list = 'source.orders.created'",
                "kafka_group_name = 'sqltide_orders_orders_analytics'",
                "kafka_format = 'JSONAsString'",
            ),
            expected_absent_fragments=("ORDER BY", "PARTITION BY", "TTL "),
        ),
        RenderManagedSourceTestCase(
            description="renders sorted extra kafka settings after typed settings",
            extra_settings={
                "kafka_handle_error_mode": "stream",
                "kafka_num_consumers": "4",
            },
            expected_fragments=(
                "kafka_broker_list = 'kafka:9092'",
                "kafka_group_name = 'sqltide_orders_orders_analytics'",
                "kafka_format = 'JSONAsString'",
                "kafka_handle_error_mode = 'stream'",
                "kafka_num_consumers = '4'",
            ),
            expected_absent_fragments=("ORDER BY",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_supported_kafka_table_when_rendering_then_it_returns_expected_create_table_ddl(
    test_case: RenderManagedSourceTestCase,
) -> None:
    rendered_ddl: str = ClickHouseAdapter().render_resource(
        resource=build_adapter_resource(build_kafka_table(extra_settings=test_case.extra_settings)),
        database="analytics",
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered_ddl
    for expected_absent_fragment in test_case.expected_absent_fragments:
        assert expected_absent_fragment not in rendered_ddl
