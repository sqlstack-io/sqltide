from dataclasses import dataclass

from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterManifest,
    AdapterQueryCancellation,
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterStatementProgress,
    AdapterTargetMutationLock,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode


@dataclass(frozen=True)
class DriverErrorTranslationTestCase:
    description: str
    driver_error: Exception
    expected_error_type: type[AdapterWarehouseError]
    expected_message: str


@dataclass(frozen=True)
class ConnectionTranslationTestCase:
    description: str
    driver_error: Exception
    expected_error_type: type[AdapterWarehouseError]


@dataclass(frozen=True)
class ConnectionQueryNormalizationTestCase:
    description: str
    raw_column_names: list[str]
    raw_result_rows: list[list[object]]
    expected_column_names: tuple[str, ...]
    expected_rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class ClickHouseReplayFrontierTestCase:
    description: str
    request: AdapterReplayOffsetProgressRequest
    expected_frontiers: tuple[AdapterReplayOffsetFrontier, ...]
    expected_query_fragments: tuple[str, ...]


@dataclass(frozen=True)
class ClickHouseDropLimitTestCase:
    description: str
    setting_value: int
    server_default_value: int
    expected_limit: int | None
    expected_server_default: int | None


@dataclass(frozen=True)
class ClickHouseWorkflowCorrelationTestCase:
    description: str
    expected_query_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClickHouseStatementProgressTestCase:
    description: str
    expected_progress: AdapterStatementProgress


@dataclass(frozen=True)
class ClickHouseQueryCancellationTestCase:
    description: str
    query_id: str
    expected_cancellation: AdapterQueryCancellation
    expected_kill_fragment: str


@dataclass(frozen=True)
class CatalogInspectionTestCase:
    description: str
    expected_timezone: str
    expected_relation_names: frozenset[str]
    expected_query_count: int


@dataclass(frozen=True)
class CatalogRetryTestCase:
    description: str
    expected_query_count: int
    expected_delays: tuple[float, ...]


@dataclass(frozen=True)
class CatalogFailureTestCase:
    description: str
    expected_query_count: int
    expected_delays: tuple[float, ...]
    expected_error: str


@dataclass(frozen=True)
class ClickHouseLandingSchemaTestCase:
    description: str
    expected_columns: tuple[tuple[str, str], ...]
    expected_query_fragments: tuple[str, ...]


@dataclass(frozen=True)
class ClickHouseLandingTimestampProjectionTestCase:
    description: str
    expected_projection: str
    unexpected_projection: str


@dataclass(frozen=True)
class ClickHousePublishCapabilitiesTestCase:
    description: str
    expected_stable_logical_bindings: bool
    expected_per_relation_atomic_replace: bool
    expected_graph_atomic_publish: bool


@dataclass(frozen=True)
class ClickHouseCleanupProtectionTestCase:
    description: str
    active_relation_name: str
    expected_error_fragment: str


@dataclass(frozen=True)
class TerminalObservationInsertTestCase:
    description: str
    expected_invocation_id: str
    expected_result_id: str
    expected_invocation_values: str
    expected_node_result_values: str


@dataclass(frozen=True)
class LatestNodeStatusQueryTestCase:
    description: str
    expected_current_status_fragment: str
    expected_node_values_fragment: str
    expected_target_fragment: str
    expected_project_fragment: str
    expected_logical_slot_fragment: str


@dataclass(frozen=True)
class RunEventInsertsTestCase:
    description: str
    include_migration: bool
    expected_statement_count: int
    expected_insert_fragment: str
    expected_values_fragment: str


@dataclass(frozen=True)
class RunStatementInsertsTestCase:
    description: str
    expected_statement_count: int
    expected_fragments: tuple[str, ...]


@dataclass(frozen=True)
class BuildInspectedManagedTableStateTestCase:
    description: str
    active_binding_rows: tuple[tuple[str, str], ...]
    system_rows: tuple[tuple[str, str], ...]
    expected_logical_names: tuple[str, ...]
    expected_active_bindings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RenderManagedSourceTestCase:
    description: str
    extra_settings: dict[str, str] | None
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class RenderTableTestCase:
    description: str
    partition_by: str | None
    ttl: str | None
    settings: dict[str, str] | None
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class RenderMaterializedViewTestCase:
    description: str
    query: str
    expected_source_reference: str
    expected_target_reference: str
    expected_query_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderStableViewTestCase:
    description: str
    database: str
    view_name: str
    target_table_name: str
    expected_fragments: tuple[str, ...]


@dataclass(frozen=True)
class RenderViewTestCase:
    description: str
    database: str
    view_name: str
    database_template: str
    expected_ddl: str


@dataclass(frozen=True)
class RenderMetadataStateDdlTestCase:
    description: str
    statement_index: int
    expected_sql: str


@dataclass(frozen=True)
class MetadataStateInsertStatementTestCase:
    description: str
    statement_index: int
    expected_sql: str
    expected_values: str


@dataclass(frozen=True)
class RenderOffsetReplayStatementTestCase:
    description: str
    source_table_name: str
    target_table_name: str
    shadow_target_name: str
    anchor_table_name: str
    query: str
    replay_table_name_by_logical_name: dict[str, str]
    expected_statement: str
    settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RenderSourceFilteredOffsetPhysicalBoundaryTestCase:
    description: str
    query: str
    expected_inclusive_predicate: str
    expected_exclusive_predicate: str
    expected_source_fragment: str
    expected_occurrence_count: int
    expected_absent_fragment: str


@dataclass(frozen=True)
class RenderScalarReplayBoundaryTestCase:
    description: str
    mode: AdapterReplayBoundaryMode
    boundary_key: str
    boundary_column_type: str
    cutoff_value: str
    lower_bound_value: str
    cutoff_inclusive: bool
    lower_bound_inclusive: bool
    query: str
    expected_lower_fragment: str
    expected_upper_fragment: str
    expected_where_fragment: str


@dataclass(frozen=True)
class RenderAggregateScalarPhysicalBoundaryTestCase:
    description: str
    expected_source_fragment: str
    expected_lower_fragment: str
    expected_upper_fragment: str
    expected_outer_where_fragment: str
    expected_absent_fragment: str


@dataclass(frozen=True)
class RenderDeploymentLookbackTestCase:
    description: str
    expected_boundary_lookup_fragment: str
    expected_root_filter_fragment: str
    expected_root_filter_count: int


@dataclass(frozen=True)
class RenderTemplateReplayTestCase:
    description: str
    mode: AdapterReplayBoundaryMode
    query: str
    database_template: str
    boundary_column_type: str | None
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class ClickHouseConnectionConfigErrorTestCase:
    description: str
    values: tuple[tuple[str, object], ...]
    expected_error_fragment: str


@dataclass(frozen=True)
class ClickHouseConnectionSettingsTestCase:
    description: str
    values: tuple[tuple[str, object], ...]
    expected_settings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClickHouseConnectionDriverSettingsTestCase:
    description: str
    database: str | None
    settings: tuple[tuple[str, str], ...]
    expected_driver_settings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClickHouseConnectionReprTestCase:
    description: str
    values: tuple[tuple[str, object], ...]
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class ClickHouseManagedSourceRealizationTestCase:
    description: str
    expected_relation_name: str
    expected_resource_names: tuple[str, ...]
    expected_consumer_group: str
    expected_landing_ttl: str | None


@dataclass(frozen=True)
class ClickHouseConsumerGroupTestCase:
    description: str
    consumer_group: str
    expected_consumer_group: str


@dataclass(frozen=True)
class ClickHouseSourceRealizationErrorTestCase:
    description: str
    source_format: str
    settings: tuple[tuple[str, str], ...]
    expected_error_type: type[Exception]
    expected_error_fragment: str


@dataclass(frozen=True)
class ClickHouseModelRealizationTestCase:
    description: str
    expected_relation_name: str
    expected_resource_names: tuple[str, ...]
    expected_source_relation_name: str


@dataclass(frozen=True)
class RenderMetadataMutationSqlTestCase:
    description: str
    expected_database_sql: str
    expected_migration_statement_count: int
    expected_migration_last_sql: str
    expected_persistence_first_sql: str


@dataclass(frozen=True)
class RenderLifecycleMutationSqlTestCase:
    description: str
    expected_binding_sql: tuple[str, ...]
    expected_cleanup_sql: tuple[str, ...]
    expected_inspection_count: int


@dataclass(frozen=True)
class RefreshRenderingTestCase:
    description: str
    refresh: str | None
    append: bool
    expected_fragments: tuple[str, ...]
    forbidden_fragments: tuple[str, ...]


@dataclass(frozen=True)
class SecretRenderingTestCase:
    description: str
    environment: tuple[tuple[str, str], ...]
    variable_name: str
    expected_fragment: str


@dataclass(frozen=True)
class MissingSecretTestCase:
    description: str
    variable_name: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ClickHouseWarehouseHealthTestCase:
    description: str
    disk_rows: tuple[tuple[object, ...], ...]
    metric_rows: tuple[tuple[object, ...], ...]
    expected_disk_statuses: tuple[str, ...]
    expected_total_bytes: tuple[int | None, ...]
    expected_availability: str
    expected_status: str
    expected_memory_basis: str
    expected_table_name: str
    expected_query_count: int
    managed_source_names: tuple[str, ...]
    consumer_row: tuple[object, ...]
    expected_configured_tables: int
    expected_materialized_tables: int
    expected_materialized_table_names: tuple[str, ...]
    expected_polling_tables: int
    expected_exception_tables: int
    expected_consumer_query_fragment: str


@dataclass(frozen=True)
class ClickHouseOptionalHealthFailureTestCase:
    description: str
    expected_warning: str


@dataclass(frozen=True)
class TargetMutationLockAcquireTestCase:
    description: str
    database: str
    owner_id: str
    expected_lock: AdapterTargetMutationLock
    expected_statements: tuple[str, ...]


@dataclass(frozen=True)
class TargetMutationLockConflictTestCase:
    description: str
    database: str
    current_owner_id: str
    requested_owner_id: str
    expected_error_message: str
    expected_statements: tuple[str, ...]


@dataclass(frozen=True)
class TargetMutationLockReleaseTestCase:
    description: str
    lock: AdapterTargetMutationLock
    expected_statements: tuple[str, ...]


@dataclass(frozen=True)
class TargetMutationLockOwnershipChangeTestCase:
    description: str
    lock: AdapterTargetMutationLock
    current_owner_id: str
    expected_error_message: str
    expected_statements: tuple[str, ...]


@dataclass(frozen=True)
class ManifestMigrationDdlTestCase:
    description: str
    expected_ddl: str
    expected_drop: str


@dataclass(frozen=True)
class ManifestMigrationWorkflowTestCase:
    description: str
    expected_last_statement: str


@dataclass(frozen=True)
class ManifestPublicationTestCase:
    description: str
    expected_statements: tuple[str, ...]


@dataclass(frozen=True)
class ManifestLoadingTestCase:
    description: str
    expected_query: str
    expected_manifest: AdapterManifest
