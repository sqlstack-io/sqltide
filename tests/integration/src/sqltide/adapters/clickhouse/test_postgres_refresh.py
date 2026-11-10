"""Apply refreshable materialized view DDL against a real ClickHouse server."""

from collections.abc import Sequence

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import render_clickhouse_resource
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterColumn,
    AdapterMaterializedView,
    AdapterRefreshState,
    AdapterTable,
)
from tests.integration.src.sqltide.adapters.clickhouse._test_types import (
    PostgresRefreshEndToEndTestCase,
    RefreshableViewIntegrationTestCase,
    RefreshStateIntegrationTestCase,
)
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    refreshed_rows,
    start_postgres_container,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RefreshableViewIntegrationTestCase(
            description="an hourly refreshable view is accepted and registered",
            refresh="1 HOUR",
            append=False,
            expected_row_count=1,
        ),
        RefreshableViewIntegrationTestCase(
            description="a ten minute refreshable view is accepted and registered",
            refresh="10 MINUTE",
            append=False,
            expected_row_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_refreshable_view_when_applied_to_real_clickhouse_then_it_registers_its_schedule(
    test_case: RefreshableViewIntegrationTestCase,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    target: AdapterTable = AdapterTable(
        name="pg__course",
        columns=(
            AdapterColumn(name="course_key", type="String"),
            AdapterColumn(name="refresh_ts", type="DateTime64(3)"),
        ),
        engine="ReplacingMergeTree(refresh_ts)",
        order_by=("course_key",),
    )
    query: str = "SELECT 'course-1' AS course_key, now64(3) AS refresh_ts"
    view: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__pg__course",
        source_relation_name="inventory__course",
        target_relation_name=target.name,
        query=query,
        database_template=query,
        refresh=test_case.refresh,
        append=test_case.append,
    )

    clickhouse_client.command(
        render_clickhouse_resource(resource=target, database=clickhouse_database)
    )
    clickhouse_client.command(
        render_clickhouse_resource(resource=view, database=clickhouse_database)
    )

    registered: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT count() FROM system.view_refreshes "
        f"WHERE database = '{clickhouse_database}' AND view = '{view.name}'"
    ).result_rows

    assert int(str(registered[0][0])) == test_case.expected_row_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RefreshStateIntegrationTestCase(
            description="a scheduled relation reports its refresh state",
            refresh="1 HOUR",
            expected_statuses=("Scheduled", "Running", "RunningOnAnotherReplica"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_scheduled_relation_when_loading_refresh_states_then_status_is_reported(
    test_case: RefreshStateIntegrationTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    target: AdapterTable = AdapterTable(
        name="pg__course",
        columns=(
            AdapterColumn(name="course_key", type="String"),
            AdapterColumn(name="refresh_ts", type="DateTime64(3)"),
        ),
        engine="ReplacingMergeTree(refresh_ts)",
        order_by=("course_key",),
    )
    query: str = "SELECT 'course-1' AS course_key, now64(3) AS refresh_ts"
    view: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__pg__course",
        source_relation_name="inventory__course",
        target_relation_name=target.name,
        query=query,
        database_template=query,
        refresh=test_case.refresh,
        append=False,
    )
    clickhouse_client.command(
        render_clickhouse_resource(resource=target, database=clickhouse_database)
    )
    clickhouse_client.command(
        render_clickhouse_resource(resource=view, database=clickhouse_database)
    )

    states: tuple[AdapterRefreshState, ...] = managed_clickhouse_client.load_refresh_states(
        clickhouse_database
    )

    assert tuple(state.view_name for state in states) == (view.name,)
    assert states[0].status in test_case.expected_statuses
    assert states[0].exception is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PostgresRefreshEndToEndTestCase(
            description="a refreshable view pulls real rows out of real postgres",
            source_table="course",
            refresh="1 HOUR",
            expected_rows=(("partner_orders::10", "JPN"), ("web_orders::7", "GBR")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_real_postgres_when_view_refreshes_then_rows_land_in_clickhouse(
    test_case: PostgresRefreshEndToEndTestCase,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    with start_postgres_container() as postgres:
        target: AdapterTable = AdapterTable(
            name="pg__course",
            columns=(
                AdapterColumn(name="course_key", type="String"),
                AdapterColumn(name="country_normalised", type="String"),
            ),
            engine="ReplacingMergeTree()",
            order_by=("course_key",),
        )
        table_function: str = (
            f"postgresql('{postgres.container_host}:{postgres.container_port}', "
            f"'{postgres.database}', '{postgres.table}', "
            f"'{postgres.user}', '{postgres.password}')"
        )
        query: str = f"SELECT course_key, country_normalised FROM {table_function}"
        view: AdapterMaterializedView = AdapterMaterializedView(
            name="mv__pg__course",
            source_relation_name="inventory__course",
            target_relation_name=target.name,
            query=query,
            database_template=query,
            refresh=test_case.refresh,
            append=False,
        )
        clickhouse_client.command(
            render_clickhouse_resource(resource=target, database=clickhouse_database)
        )
        clickhouse_client.command(
            render_clickhouse_resource(resource=view, database=clickhouse_database)
        )

        landed: Sequence[Sequence[object]] = refreshed_rows(
            client=clickhouse_client,
            database=clickhouse_database,
            table=target.name,
            expected_count=len(test_case.expected_rows),
        )

    assert tuple((str(row[0]), str(row[1])) for row in landed) == test_case.expected_rows
