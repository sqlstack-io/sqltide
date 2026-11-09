import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterWarehouseDeadLetterModel, AdapterWarehouseHealth
from sqltide.adapters.clickhouse.classes.warehouse_health_reader import (
    ClickHouseWarehouseHealthReader,
)
from tests.integration.src.sqltide.adapters.clickhouse._test_types import (
    WarehouseHealthDeadLettersTestCase,
    WarehouseHealthMissingDeadLettersTestCase,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthMissingDeadLettersTestCase(
            description="absent table is unavailable, not zero",
            expected_dead_letters=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_dead_letter_table_when_reading_then_dead_letters_are_unavailable(
    test_case: WarehouseHealthMissingDeadLettersTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_database: str,
) -> None:
    health: AdapterWarehouseHealth = ClickHouseWarehouseHealthReader(
        connection=managed_clickhouse_client
    ).read(database=clickhouse_database)

    assert health.dead_letters is test_case.expected_dead_letters


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthDeadLettersTestCase(
            description="counts the last day and truncates the latest payload",
            payload_length=600,
            expected_row_count=2,
            expected_failed_columns=("amount",),
            expected_payload_length=500,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dead_letters_when_reading_then_recent_counts_and_latest_row_are_returned(
    test_case: WarehouseHealthDeadLettersTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    for statement in managed_clickhouse_client.render_migrate_metadata_state(clickhouse_database):
        clickhouse_client.command(statement)
    latest_payload: str = "x" * test_case.payload_length
    clickhouse_client.command(
        f"""
        INSERT INTO {clickhouse_database}._sqltide_dead_letters
        SELECT model_name, failed_columns, payload, NULL, NULL, quarantined_at
        FROM VALUES(
            'model_name String, failed_columns Array(String), payload String,
             quarantined_at DateTime64(3)',
            ('orders', ['order_id'], 'older', now64(3) - INTERVAL 2 HOUR),
            ('orders', ['amount'], '{latest_payload}', now64(3) - INTERVAL 1 HOUR),
            ('orders', ['order_id'], 'expired', now64(3) - INTERVAL 25 HOUR)
        )
        """
    )

    health: AdapterWarehouseHealth = ClickHouseWarehouseHealthReader(
        connection=managed_clickhouse_client
    ).read(database=clickhouse_database)

    assert health.dead_letters is not None
    assert len(health.dead_letters) == 1
    model: AdapterWarehouseDeadLetterModel = health.dead_letters[0]
    assert model.model_name == "orders"
    assert model.row_count == test_case.expected_row_count
    assert model.latest_failed_columns == test_case.expected_failed_columns
    assert len(model.latest_payload) == test_case.expected_payload_length


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
