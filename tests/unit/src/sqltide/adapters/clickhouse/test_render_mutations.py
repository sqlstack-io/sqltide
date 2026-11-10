from typing import cast

import pytest

from sqltide._native import (
    build_adapter_metadata_state,
)
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterIdentity,
    AdapterMetadataState,
    AdapterRelationCleanupRequest,
    AdapterStableBinding,
    AdapterStableBindingRemoval,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse.classes.clickhouse_connection import ClickHouseConnection
from sqltide.adapters.clickhouse.types import RawClickHouseClient
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    RenderLifecycleMutationSqlTestCase,
    RenderMetadataMutationSqlTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    FakeRawClickHouseQueryResult,
    GuardedRenderingClickHouseConnection,
    StubRawClickHouseClient,
    build_metadata_state,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RenderMetadataMutationSqlTestCase(
            description="renders database and metadata mutations as exact executable SQL",
            expected_database_sql="CREATE DATABASE IF NOT EXISTS metadata;",
            expected_migration_statement_count=21,
            expected_migration_last_sql=(
                "INSERT INTO metadata._sqltide_schema_versions "
                "(version, applied_at) SELECT 8, now64(3, 'UTC') WHERE NOT EXISTS ("
                "SELECT 1 FROM metadata._sqltide_schema_versions WHERE version = 8);"
            ),
            expected_persistence_first_sql=(
                "INSERT INTO metadata._sqltide_virtual_object_state "
                "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
                "logical_object_type, logical_object_name, physical_database_name, "
                "physical_relation_name, logical_model_database, logical_model_name, "
                "is_selected_root, object_fingerprint, canonical_query, observed_at) VALUES\n"
                "('20260408T130000Z_ab12cd', "
                "'622c1dd60e3a5b7e56eae621b143b0756a7fdb685c421d446873718ef43e979e', "
                "'deployment', '20260408T130000Z_ab12cd', NULL, "
                "'table', 'tbl__orders_enriched', NULL, "
                "'tbl__orders_enriched__20260408T130000Z_ab12cd', NULL, 'orders_enriched', false, "
                "'fingerprint_transform', 'SELECT * FROM raw__orders', "
                "'2026-04-08T13:00:00Z');"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_metadata_requests_when_rendering_mutations_then_sql_is_exact_and_terminated(
    test_case: RenderMetadataMutationSqlTestCase,
) -> None:
    raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
        FakeRawClickHouseQueryResult(column_names=[], result_rows=[])
    )
    connection: ClickHouseConnection = ClickHouseConnection(cast(RawClickHouseClient, raw_client))
    state: AdapterMetadataState = build_adapter_metadata_state(build_metadata_state())

    migration_sql: tuple[str, ...] = connection.render_migrate_metadata_state("metadata")
    persistence_sql: tuple[str, ...] = connection.render_persist_metadata_state(
        database="metadata",
        state=state,
    )

    assert connection.render_ensure_database("metadata") == test_case.expected_database_sql
    assert len(migration_sql) == test_case.expected_migration_statement_count
    assert migration_sql[-1] == test_case.expected_migration_last_sql
    assert persistence_sql[0] == test_case.expected_persistence_first_sql
    assert all(statement.endswith(";") for statement in (*migration_sql, *persistence_sql))
    assert all(not statement.endswith(";;") for statement in (*migration_sql, *persistence_sql))


@pytest.mark.parametrize(
    "test_case",
    [
        RenderLifecycleMutationSqlTestCase(
            description="renders binding and kind-aware cleanup statements in request order",
            expected_binding_sql=(
                "CREATE OR REPLACE VIEW analytics.tbl__orders AS\n"
                "SELECT * FROM analytics.tbl__orders__candidate;",
                "DROP VIEW IF EXISTS analytics.tbl__obsolete SYNC;",
            ),
            expected_cleanup_sql=(
                "DROP TABLE IF EXISTS analytics.tbl__orders__old SYNC;",
                "DROP VIEW IF EXISTS analytics.view__orders__old SYNC;",
            ),
            expected_inspection_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_lifecycle_requests_when_rendering_mutations_then_guards_and_sql_are_exact(
    test_case: RenderLifecycleMutationSqlTestCase,
) -> None:
    catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(
            adapter=AdapterIdentity(name="clickhouse"),
            database="analytics",
        ),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="tbl__orders__old",
                engine="MergeTree",
                columns=(CatalogColumn(name="order_id", type="String"),),
            ),
            CatalogRelation(
                name="view__orders__old",
                engine="View",
                columns=(CatalogColumn(name="order_id", type="String"),),
            ),
        ),
    )
    connection: GuardedRenderingClickHouseConnection = GuardedRenderingClickHouseConnection(
        catalog=catalog,
        managed_table_state=InspectedManagedTableState(active_bindings=(), physical_candidates=()),
    )
    binding_sql: tuple[str, ...] = connection.render_replace_stable_bindings(
        AdapterBindingReplacementRequest(
            bindings=(
                AdapterStableBinding(
                    database="analytics",
                    logical_name="tbl__orders",
                    physical_name="tbl__orders__candidate",
                ),
            ),
            removals=(
                AdapterStableBindingRemoval(
                    database="analytics",
                    logical_name="tbl__obsolete",
                ),
            ),
        )
    )
    cleanup_sql: tuple[str, ...] = connection.render_cleanup_relations(
        AdapterRelationCleanupRequest(
            database="analytics",
            relation_names=("tbl__orders__old", "view__orders__old"),
        )
    )

    assert binding_sql == test_case.expected_binding_sql
    assert cleanup_sql == test_case.expected_cleanup_sql
    assert connection.inspection_count == test_case.expected_inspection_count
