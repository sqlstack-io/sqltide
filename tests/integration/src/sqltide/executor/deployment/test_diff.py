import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_deployment_diff
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.executor.deployment.models import (
    DeploymentDiffRelation,
    DeploymentDiffRequest,
    DeploymentDiffResult,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.deployment._test_types import (
    DeploymentDiffIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.janitor.helpers import (
    JanitorIntegrationState,
    build_janitor_integration_state,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffIntegrationTestCase(
            description="compares active and retained physical relations in ClickHouse",
            expected_logical_name="tbl__orders_enriched",
            expected_from_row_count=2,
            expected_to_row_count=1,
            expected_status="changed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_and_retained_deployments_when_diffing_then_reads_real_schema_and_counts(
    test_case: DeploymentDiffIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    connection: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    try:
        state: JanitorIntegrationState = build_janitor_integration_state(
            clickhouse_client=clickhouse_client,
            managed_client=connection,
            database=clickhouse_database,
        )
        result: DeploymentDiffResult = execute_deployment_diff(
            request=DeploymentDiffRequest(
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                comparison=state.old_published_deployment_id,
            ),
            client=connection,
        )
    finally:
        connection.close()

    assert result.from_endpoint == "active"
    assert result.to_endpoint == state.old_published_deployment_id
    assert len(result.relations) == 1
    relation: DeploymentDiffRelation = result.relations[0]
    assert relation.logical_name == test_case.expected_logical_name
    assert relation.from_row_count == test_case.expected_from_row_count
    assert relation.to_row_count == test_case.expected_to_row_count
    assert relation.status == test_case.expected_status


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
