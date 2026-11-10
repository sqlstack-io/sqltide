from collections.abc import Sequence
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_reconcile
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.models import ActualState
from sqltide.executor.reconcile.models import ReconcileResult
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.reconcile._test_types import (
    ReconcilePersistenceIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.reconcile.helpers import (
    build_matching_reconcile_states,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ReconcilePersistenceIntegrationTestCase(
            description="persists compatible table and materialized-view baseline records",
            expected_rows=(
                ("materialized_view", "mv__orders", "SELECT order_id FROM raw__orders"),
                ("table", "tbl__orders", None),
            ),
            expected_id_prefix_matches=(True, True),
            expected_reconcile_id_prefix="reconcile_",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compatible_live_objects_when_applying_reconcile_then_persists_baseline(
    test_case: ReconcilePersistenceIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    desired_state: DesiredState
    actual_state: ActualState
    desired_state, actual_state = build_matching_reconcile_states()
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    try:
        result: ReconcileResult = cast(
            ReconcileResult,
            execute_reconcile(
                client=managed_client,
                target_database=clickhouse_database,
                metadata_database=clickhouse_database,
                desired_state=desired_state,
                actual_state=actual_state,
                selected_model_keys=frozenset(),
                apply=True,
            ),
        )
    finally:
        managed_client.close()

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT state_id, logical_object_type, logical_object_name, canonical_query FROM "
        f"{clickhouse_database}._sqltide_virtual_object_state "
        "ORDER BY logical_object_type, logical_object_name"
    ).result_rows

    assert tuple(tuple(row[1:]) for row in rows) == test_case.expected_rows
    assert (
        tuple(str(row[0]).startswith(test_case.expected_reconcile_id_prefix) for row in rows)
        == test_case.expected_id_prefix_matches
    )
    assert result.reconcile_id.startswith(test_case.expected_reconcile_id_prefix)
