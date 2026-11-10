from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_janitor
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.executor.janitor.models import (
    JanitorApplyResult,
    JanitorPreviewResult,
    JanitorRequest,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.janitor._test_types import (
    ExecuteJanitorApplyIntegrationTestCase,
    ExecuteJanitorPreviewIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.janitor.helpers import (
    JanitorIntegrationState,
    build_janitor_integration_state,
    group_candidate_deployment_ids,
    load_existing_table_names,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteJanitorPreviewIntegrationTestCase(
            description="classifies active recent old and stale deployments conservatively",
            retention_days=7,
            expected_deletable_deployment_ids=(
                "20260409T104000Z_failed1",
                "20260409T103000Z_stale11",
            ),
            expected_kept_deployment_ids=(
                "20260409T102000Z_active1",
                "20260409T101000Z_recent1",
                "20260409T100000Z_old111",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_real_deployments_when_previewing_janitor_then_it_classifies_candidates(
    test_case: ExecuteJanitorPreviewIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
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
        build_janitor_integration_state(
            clickhouse_client=clickhouse_client,
            managed_client=managed_client,
            database=clickhouse_database,
        )
        result: JanitorPreviewResult = cast(
            JanitorPreviewResult,
            execute_janitor(
                request=JanitorRequest(
                    database=clickhouse_database,
                    metadata_database=clickhouse_database,
                    retention_days=test_case.retention_days,
                    apply=False,
                ),
                client=managed_client,
            ),
        )
    finally:
        managed_client.close()

    deletable_deployment_ids: tuple[str, ...]
    kept_deployment_ids: tuple[str, ...]
    deletable_deployment_ids, kept_deployment_ids = group_candidate_deployment_ids(
        result.candidates
    )
    assert deletable_deployment_ids == test_case.expected_deletable_deployment_ids
    assert kept_deployment_ids == test_case.expected_kept_deployment_ids


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteJanitorApplyIntegrationTestCase(
            description="drops only old and stale physical objects while keeping metadata",
            retention_days=7,
            expected_deleted_deployment_ids=(
                "20260409T104000Z_failed1",
                "20260409T103000Z_stale11",
            ),
            expected_deleted_target_tables=(
                "tbl__orders_enriched__20260409T104000Z_failed1",
                "tbl__orders_enriched__20260409T103000Z_stale11",
            ),
            expected_retained_target_tables=(
                "tbl__orders_enriched__20260409T102000Z_active1",
                "tbl__orders_enriched__20260409T101000Z_recent1",
                "tbl__orders_enriched__20260409T100000Z_old111",
                "tbl__orders_enriched",
                "_sqltide_virtual_deployments",
                "_sqltide_virtual_publications",
            ),
            expected_deployment_row_count=4,
            expected_publish_history_row_count=3,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_real_deletable_deployments_when_applying_janitor_then_it_drops_only_physical_objects(
    test_case: ExecuteJanitorApplyIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
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
        integration_state: JanitorIntegrationState = build_janitor_integration_state(
            clickhouse_client=clickhouse_client,
            managed_client=managed_client,
            database=clickhouse_database,
        )
        result: JanitorApplyResult = cast(
            JanitorApplyResult,
            execute_janitor(
                request=JanitorRequest(
                    database=clickhouse_database,
                    metadata_database=clickhouse_database,
                    retention_days=test_case.retention_days,
                    apply=True,
                ),
                client=managed_client,
            ),
        )
    finally:
        managed_client.close()

    existing_table_names: tuple[str, ...] = load_existing_table_names(
        clickhouse_client=clickhouse_client,
        database=clickhouse_database,
    )
    metadata_row_counts: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT "
        f"(SELECT count() FROM {clickhouse_database}._sqltide_virtual_deployments) "
        "AS deployment_count, "
        f"(SELECT count() FROM {clickhouse_database}._sqltide_virtual_publications) "
        "AS publish_count"
    ).result_rows

    assert result.deleted_deployment_ids == test_case.expected_deleted_deployment_ids
    assert (
        integration_state.old_published_target_table_name
        in test_case.expected_retained_target_tables
    )
    assert (
        integration_state.stale_unpublished_target_table_name
        in test_case.expected_deleted_target_tables
    )
    assert (
        integration_state.failed_incomplete_target_table_name
        in test_case.expected_deleted_target_tables
    )
    expected_deleted_target_table: str
    for expected_deleted_target_table in test_case.expected_deleted_target_tables:
        assert expected_deleted_target_table not in existing_table_names
    expected_retained_target_table: str
    for expected_retained_target_table in test_case.expected_retained_target_tables:
        assert expected_retained_target_table in existing_table_names
    assert metadata_row_counts == [
        (test_case.expected_deployment_row_count, test_case.expected_publish_history_row_count)
    ]
