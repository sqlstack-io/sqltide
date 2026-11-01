from dataclasses import replace
from typing import cast

import pytest

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterInvocationRecord,
    AdapterManifestSnapshot,
    AdapterMetadataState,
    AdapterNodeResultRecord,
    AdapterQueryResult,
)
from sqltide.adapters.clickhouse._helpers.metadata import (
    load_clickhouse_manifests,
    render_clickhouse_manifest_publication,
    render_clickhouse_metadata_migration_statements,
    render_clickhouse_metadata_migration_workflow,
    render_clickhouse_metadata_state,
)
from sqltide.compiler.planner.main.build_adapter_metadata_state import (
    build_adapter_metadata_state,
)
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    ManifestLoadingTestCase,
    ManifestMigrationDdlTestCase,
    ManifestMigrationWorkflowTestCase,
    ManifestPublicationTestCase,
    MetadataStateInsertStatementTestCase,
    RenderMetadataStateDdlTestCase,
    TerminalObservationInsertTestCase,
)
from tests.unit.src.sqltide.adapters.clickhouse.helpers import (
    build_metadata_state,
    build_test_manifest,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RenderMetadataStateDdlTestCase(
            description="renders object-state metadata table ddl",
            statement_index=0,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_virtual_object_state (\n"
                "    state_id String,\n"
                "    observation_id String,\n"
                "    state_kind LowCardinality(String),\n"
                "    deployment_id Nullable(String),\n"
                "    logical_database_name Nullable(String),\n"
                "    logical_object_type String,\n"
                "    logical_object_name String,\n"
                "    physical_database_name Nullable(String),\n"
                "    physical_relation_name Nullable(String),\n"
                "    logical_model_database Nullable(String),\n"
                "    logical_model_name Nullable(String),\n"
                "    is_selected_root Bool,\n"
                "    object_fingerprint String,\n"
                "    canonical_query Nullable(String),\n"
                "    observed_at DateTime64(3, 'UTC')\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (state_kind, state_id, logical_object_type, logical_object_name)"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders deployments metadata table ddl",
            statement_index=1,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_virtual_deployments (\n"
                "    deployment_id String,\n"
                "    workflow_fingerprint String,\n"
                "    replay_lineage_mode String,\n"
                "    boundary_time DateTime64(3, 'UTC'),\n"
                "    created_at DateTime64(3, 'UTC'),\n"
                "    tool_version String\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (deployment_id)"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders deployment-watermarks metadata table ddl",
            statement_index=2,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_virtual_replay_boundaries (\n"
                "    deployment_id String,\n"
                "    root_database_name Nullable(String),\n"
                "    root_object_type String,\n"
                "    root_object_name String,\n"
                "    anchor_database_name Nullable(String),\n"
                "    anchor_object_type String,\n"
                "    anchor_object_name String,\n"
                "    boundary_kind LowCardinality(String),\n"
                "    value_kind LowCardinality(String),\n"
                "    partition_value Nullable(String),\n"
                "    lower_value Nullable(String),\n"
                "    cutoff_value String,\n"
                "    cutoff_inclusive Bool,\n"
                "    captured_at DateTime64(3, 'UTC')\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (deployment_id, root_object_type, root_object_name, boundary_kind, "
                "ifNull(partition_value, ''))"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders publish-history metadata table ddl",
            statement_index=3,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_virtual_publications (\n"
                "    publication_id String,\n"
                "    deployment_id String,\n"
                "    operation LowCardinality(String) DEFAULT 'promote',\n"
                "    previous_deployment_id Nullable(String),\n"
                "    logical_database_name String,\n"
                "    logical_view_name String,\n"
                "    physical_database_name String,\n"
                "    physical_relation_name String,\n"
                "    published_at DateTime64(3, 'UTC')\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (publication_id, logical_database_name, logical_view_name)"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders logical direct fingerprints metadata table ddl",
            statement_index=4,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_direct_fingerprints (\n"
                "    fingerprint_id String,\n"
                "    logical_model_identity String,\n"
                "    definition_sql String,\n"
                "    definition_hash String,\n"
                "    identity_metadata String,\n"
                "    workflow_id String,\n"
                "    tool_version String,\n"
                "    applied_at DateTime64(3, 'UTC')\n"
                ") ENGINE = MergeTree ORDER BY (logical_model_identity, applied_at, "
                "fingerprint_id)"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders invocation history table ddl",
            statement_index=5,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_invocations (\n"
                "    invocation_id String,\n"
                "    project_identity String,\n"
                "    target_identity String,\n"
                "    command LowCardinality(String),\n"
                "    mode Nullable(String),\n"
                "    outcome LowCardinality(String),\n"
                "    exit_code Int32,\n"
                "    materialized_outcome Nullable(String),\n"
                "    deployment_id Nullable(String),\n"
                "    workflow_id Nullable(String),\n"
                "    selected_node_count UInt64,\n"
                "    started_at DateTime64(3, 'UTC'),\n"
                "    completed_at DateTime64(3, 'UTC'),\n"
                "    duration_ms UInt64,\n"
                "    error_message Nullable(String),\n"
                "    summary_json String,\n"
                "    tool_version String\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (project_identity, target_identity, completed_at, invocation_id)"
            ),
        ),
        RenderMetadataStateDdlTestCase(
            description="renders node result history table ddl",
            statement_index=6,
            expected_sql=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_node_results (\n"
                "    result_id String,\n"
                "    invocation_id String,\n"
                "    node_kind LowCardinality(String),\n"
                "    node_name String,\n"
                "    binding_key String,\n"
                "    definition_fingerprint String,\n"
                "    execution_fingerprint String,\n"
                "    target_identity String,\n"
                "    trigger LowCardinality(String),\n"
                "    scheduled_for Nullable(DateTime64(3, 'UTC')),\n"
                "    cadence_seconds Nullable(UInt64),\n"
                "    warmup_seconds UInt64,\n"
                "    status LowCardinality(String),\n"
                "    severity Nullable(String),\n"
                "    failure_count UInt64,\n"
                "    completed_at DateTime64(3, 'UTC'),\n"
                "    payload_json String,\n"
                "    error_message Nullable(String)\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (node_kind, node_name, completed_at, result_id)"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_metadata_database_when_rendering_then_it_returns_expected_metadata_ddl(
    test_case: RenderMetadataStateDdlTestCase,
) -> None:
    rendered_statements: tuple[str, ...] = render_clickhouse_metadata_migration_statements(
        "metadata"
    )

    matching_statement: str = rendered_statements[test_case.statement_index]

    assert matching_statement == test_case.expected_sql


@pytest.mark.parametrize(
    "test_case",
    [
        MetadataStateInsertStatementTestCase(
            description="builds object-state insert statement rows",
            statement_index=0,
            expected_sql=(
                "INSERT INTO metadata._sqltide_virtual_object_state "
                "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
                "logical_object_type, logical_object_name, physical_database_name, "
                "physical_relation_name, logical_model_database, logical_model_name, "
                "is_selected_root, object_fingerprint, canonical_query, observed_at) VALUES"
            ),
            expected_values=(
                "('20260408T130000Z_ab12cd', "
                "'622c1dd60e3a5b7e56eae621b143b0756a7fdb685c421d446873718ef43e979e', "
                "'deployment', '20260408T130000Z_ab12cd', NULL, 'table', "
                "'tbl__orders_enriched', NULL, "
                "'tbl__orders_enriched__20260408T130000Z_ab12cd', NULL, 'orders_enriched', "
                "false, 'fingerprint_transform', 'SELECT * FROM raw__orders', "
                "'2026-04-08T13:00:00Z')"
            ),
        ),
        MetadataStateInsertStatementTestCase(
            description="builds deployments insert statement rows",
            statement_index=1,
            expected_sql=(
                "INSERT INTO metadata._sqltide_virtual_deployments "
                "(deployment_id, workflow_fingerprint, replay_lineage_mode, boundary_time, "
                "created_at, tool_version) VALUES"
            ),
            expected_values=(
                "('20260408T130000Z_ab12cd', 'workflow-fingerprint', 'offsets', "
                "'2026-04-08T13:00:05Z', '2026-04-08T13:00:00Z', '1.2.3')"
            ),
        ),
        MetadataStateInsertStatementTestCase(
            description="builds deployment-watermarks insert statement rows",
            statement_index=2,
            expected_sql=(
                "INSERT INTO metadata._sqltide_virtual_replay_boundaries "
                "(deployment_id, root_database_name, root_object_type, root_object_name, "
                "anchor_database_name, anchor_object_type, anchor_object_name, boundary_kind, "
                "value_kind, partition_value, lower_value, cutoff_value, cutoff_inclusive, "
                "captured_at) VALUES"
            ),
            expected_values=(
                "('20260408T130000Z_ab12cd', NULL, 'table', 'tbl__orders_enriched', NULL, "
                "'table', 'raw__orders', 'offsets', 'integer', '0', NULL, '12345', true, "
                "'1970-01-01 00:00:00.000')"
            ),
        ),
        MetadataStateInsertStatementTestCase(
            description="builds publish-history insert statement rows",
            statement_index=3,
            expected_sql=(
                "INSERT INTO metadata._sqltide_virtual_publications "
                "(publication_id, deployment_id, operation, previous_deployment_id, "
                "logical_database_name, logical_view_name, physical_database_name, "
                "physical_relation_name, published_at) VALUES"
            ),
            expected_values=(
                "('7fde4a17795328da5b8f307aea327ca739a8475d033005bf2208d00e93b21d26', "
                "'20260408T130000Z_ab12cd', 'promote', NULL, 'analytics', "
                "'tbl__orders_enriched', 'analytics', "
                "'tbl__orders_enriched__20260408T130000Z_ab12cd', '2026-04-08T13:30:00Z')"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_metadata_records_when_building_insert_statement_then_it_returns_expected_rows(
    test_case: MetadataStateInsertStatementTestCase,
) -> None:
    statements: tuple[str, ...] = render_clickhouse_metadata_state(
        database="metadata",
        state=build_adapter_metadata_state(build_metadata_state()),
    )

    assert statements[test_case.statement_index] == (
        f"{test_case.expected_sql}\n{test_case.expected_values};"
    )


@pytest.mark.parametrize(
    "test_case",
    [
        TerminalObservationInsertTestCase(
            description="builds structured terminal observation rows",
            expected_invocation_id="inv-1",
            expected_result_id="result-1",
            expected_invocation_values=(
                "\n('inv-1', '/projects/orders', 'analytics', 'audit', NULL, 'failed', 1, NULL, "
                "NULL, NULL, 1, '2026-08-02 12:00:00.000', '2026-08-02 12:00:01.000', 1000, NULL, "
                "'{\"error_failure_count\":1}', '1.2.3');"
            ),
            expected_node_result_values=(
                "\n('result-1', 'inv-1', 'audit', 'order ids are present', 'binding', "
                "'fingerprint', 'execution', 'analytics', 'manual', NULL, NULL, 0, 'failed', "
                "'error', 2, '2026-08-02 12:00:01.000', '{\"sample_rows\":[[1],[2]]}', NULL);"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_terminal_observations_when_building_inserts_then_rows_are_structured(
    test_case: TerminalObservationInsertTestCase,
) -> None:
    invocation: AdapterInvocationRecord = AdapterInvocationRecord(
        invocation_id=test_case.expected_invocation_id,
        project_identity="/projects/orders",
        target_identity="analytics",
        command="audit",
        mode=None,
        outcome="failed",
        exit_code=1,
        materialized_outcome=None,
        deployment_id=None,
        workflow_id=None,
        selected_node_count=1,
        started_at="2026-08-02 12:00:00.000",
        completed_at="2026-08-02 12:00:01.000",
        duration_ms=1000,
        error_message=None,
        summary_json='{"error_failure_count":1}',
        tool_version="1.2.3",
    )
    node_result: AdapterNodeResultRecord = AdapterNodeResultRecord(
        result_id=test_case.expected_result_id,
        invocation_id=test_case.expected_invocation_id,
        node_kind="audit",
        node_name="order ids are present",
        binding_key="binding",
        definition_fingerprint="fingerprint",
        execution_fingerprint="execution",
        target_identity="analytics",
        trigger="manual",
        scheduled_for=None,
        cadence_seconds=None,
        warmup_seconds=0,
        status="failed",
        severity="error",
        failure_count=2,
        completed_at="2026-08-02 12:00:01.000",
        payload_json='{"sample_rows":[[1],[2]]}',
        error_message=None,
    )
    state: AdapterMetadataState = replace(
        build_adapter_metadata_state(build_metadata_state()),
        invocations=(invocation,),
        node_results=(node_result,),
    )

    statements: tuple[str, ...] = render_clickhouse_metadata_state(database="metadata", state=state)

    assert statements[4].endswith(test_case.expected_invocation_values)
    assert statements[5].endswith(test_case.expected_node_result_values)


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestMigrationDdlTestCase(
            description="renders manifest table and removes legacy table",
            expected_ddl=(
                "CREATE TABLE IF NOT EXISTS metadata._sqltide_manifests (\n"
                "    manifest_id String,\n"
                "    invocation_id String,\n"
                "    project_identity String,\n"
                "    target_name String,\n"
                "    target_database String,\n"
                "    is_production Bool,\n"
                "    project_revision Nullable(String),\n"
                "    manifest_fingerprint String,\n"
                "    manifest_version UInt64,\n"
                "    pipelines Array(String),\n"
                "    resources Array(Tuple(\n"
                "        pipeline_name String,\n"
                "        logical_type String,\n"
                "        logical_name String,\n"
                "        resource_role String,\n"
                "        resource_database String,\n"
                "        resource_name String,\n"
                "        resource_kind String\n"
                "    )),\n"
                "    tool_version String,\n"
                "    published_at DateTime64(6, 'UTC')\n"
                ") ENGINE = MergeTree\n"
                "ORDER BY (project_identity, target_name, target_database, published_at, "
                "manifest_id)"
            ),
            expected_drop="DROP TABLE IF EXISTS metadata._sqltide_owned_resources SYNC",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_metadata_migration_when_rendering_then_manifest_ddl_and_legacy_drop_are_exact(
    test_case: ManifestMigrationDdlTestCase,
) -> None:
    rendered: tuple[str, ...] = render_clickhouse_metadata_migration_statements("metadata")

    assert rendered[-3] == test_case.expected_ddl
    assert rendered[-2] == test_case.expected_drop


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestMigrationWorkflowTestCase(
            description="records schema version seven last",
            expected_last_statement=(
                "INSERT INTO metadata._sqltide_schema_versions (version, applied_at) "
                "SELECT 7, now64(3, 'UTC') WHERE NOT EXISTS (SELECT 1 FROM "
                "metadata._sqltide_schema_versions WHERE version = 7);"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_metadata_migration_when_rendering_workflow_then_version_seven_marker_is_last(
    test_case: ManifestMigrationWorkflowTestCase,
) -> None:
    rendered: tuple[str, ...] = render_clickhouse_metadata_migration_workflow("metadata")

    assert rendered[-1] == test_case.expected_last_statement


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestPublicationTestCase(
            description="escapes every complete manifest value",
            expected_statements=(
                "INSERT INTO metadata._sqltide_manifests (manifest_id, invocation_id, "
                "project_identity, target_name, target_database, is_production, "
                "project_revision, manifest_fingerprint, manifest_version, pipelines, "
                "resources, tool_version, published_at) VALUES\n"
                "('manifest-\\'1', 'invocation-1', '/projects\\\\orders', 'uat', 'analytics', "
                "false, NULL, 'fingerprint-1', 1, ['orders', 'quotes\\'live'], [('orders', "
                "'model', 'orders', 'model_table', 'analytics', 'tbl__orders', 'table')], "
                "'0.37.0', toDateTime64('2026-08-28 12:00:00.123456', 6, 'UTC'));",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_complete_manifest_when_rendering_publication_then_values_are_exactly_escaped(
    test_case: ManifestPublicationTestCase,
) -> None:
    rendered: tuple[str, ...] = render_clickhouse_manifest_publication(
        database="metadata", manifest=build_test_manifest()
    )

    assert rendered == test_case.expected_statements


class ManifestLoadingConnection:
    def __init__(self) -> None:
        self.statement: str | None = None

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        del database, table
        return frozenset(
            {
                "manifest_id",
                "invocation_id",
                "project_identity",
                "target_name",
                "target_database",
                "is_production",
                "project_revision",
                "manifest_fingerprint",
                "manifest_version",
                "pipelines",
                "resources",
                "tool_version",
                "published_at",
            }
        )

    def query(self, statement: str) -> AdapterQueryResult:
        self.statement = statement
        return AdapterQueryResult(
            rows=(
                (
                    "manifest-1",
                    "invocation-1",
                    "/projects/orders",
                    "uat",
                    "analytics",
                    False,
                    "revision-1",
                    "fingerprint-1",
                    1,
                    ["orders"],
                    [
                        (
                            "orders",
                            "model",
                            "orders",
                            "model_table",
                            "analytics",
                            "tbl__orders",
                            "table",
                        )
                    ],
                    "0.37.0",
                    "2026-08-28 12:00:00.123456",
                ),
            ),
            column_names=(),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestLoadingTestCase(
            description="preserves scoped nested manifest rows",
            expected_query=(
                "SELECT manifest.manifest_id, manifest.invocation_id, "
                "manifest.project_identity, manifest.target_name, manifest.target_database, "
                "manifest.is_production, manifest.project_revision, "
                "manifest.manifest_fingerprint, manifest.manifest_version, "
                "manifest.pipelines, manifest.resources, manifest.tool_version, "
                "manifest.published_at FROM metadata._sqltide_manifests AS manifest "
                "WHERE manifest.project_identity = '/projects/orders' "
                "AND manifest.target_name = 'uat' "
                "AND manifest.target_database = 'analytics' "
                "ORDER BY manifest.published_at DESC, manifest.manifest_id DESC"
            ),
            expected_manifest=replace(
                build_test_manifest(),
                manifest_id="manifest-1",
                project_identity="/projects/orders",
                project_revision="revision-1",
                pipelines=("orders",),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_manifest_rows_when_loading_then_scope_order_and_models_are_preserved(
    test_case: ManifestLoadingTestCase,
) -> None:
    connection: ManifestLoadingConnection = ManifestLoadingConnection()

    snapshot: AdapterManifestSnapshot = load_clickhouse_manifests(
        connection=cast(AdapterConnection, connection),
        database="metadata",
        project_identity="/projects/orders",
        target_name="uat",
        target_database="analytics",
    )

    assert connection.statement == test_case.expected_query
    assert snapshot.status == "available"
    assert snapshot.manifests == (test_case.expected_manifest,)
