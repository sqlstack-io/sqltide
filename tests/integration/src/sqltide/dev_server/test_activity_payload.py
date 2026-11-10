from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import read_model_activity
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from tests.integration.src.sqltide.adapters.clickhouse.helpers import connect_clickhouse
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.dev_server._test_types import ActivityEvidenceTestCase


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ActivityEvidenceTestCase(
            description="recent inserted part is reported as trustworthy model activity",
            relation_name="tbl__activity_probe",
            expected_state="moving",
            expected_source="part_log",
            expected_rows_written=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_recent_insert_when_reading_activity_then_part_log_evidence_is_returned(
    test_case: ActivityEvidenceTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    clickhouse_client.command(
        f"CREATE TABLE {clickhouse_database}.{test_case.relation_name} "
        "(value UInt64) ENGINE = MergeTree ORDER BY value"
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{test_case.relation_name}",
        data=[[1], [2]],
        column_names=["value"],
    )
    clickhouse_client.command("SYSTEM FLUSH LOGS")
    captured_at: str = str(clickhouse_client.query("SELECT toString(now64(3))").result_rows[0][0])
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings,
        database=clickhouse_database,
    )

    try:
        payload: dict[str, object] = read_model_activity(
            connection=connection,
            database=clickhouse_database,
            relation_names=(test_case.relation_name,),
            captured_at=captured_at,
        )[test_case.relation_name]
    finally:
        connection.close()

    assert payload["state"] == test_case.expected_state
    assert payload["source"] == test_case.expected_source
    assert payload["sourceAvailable"] is True
    assert payload["approximate"] is False
    assert payload["rowsWritten"] == test_case.expected_rows_written
    assert cast(str | None, payload["lastWriteAt"]) is not None
