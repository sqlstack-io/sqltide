from dataclasses import dataclass


@dataclass(frozen=True)
class QueryCancellationIntegrationTestCase:
    description: str
    query_id: str
    unrelated_query_id: str
    statement: str
    expected_query_found: bool
    expected_termination_confirmed: bool


@dataclass(frozen=True)
class ClickHouseClientIntegrationTestCase:
    description: str
    inserted_rows: tuple[dict[str, object], ...]
    expected_rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class ClickHouseReplayProgressIntegrationTestCase:
    description: str
    source_rows: int
    expected_partition: int


@dataclass(frozen=True)
class ClickHouseOffsetReplayIntegrationTestCase:
    description: str
    source_rows: tuple[tuple[str, int, int], ...]
    expected_positions: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ClickHouseWarehouseTimestampIntegrationTestCase:
    description: str
    expected_fractional_digits: int


@dataclass(frozen=True)
class ClickHouseWarehouseHealthIntegrationTestCase:
    description: str
    table_name: str
    expected_minimum_rows: int


@dataclass(frozen=True)
class ClickHouseCatalogIntegrationTestCase:
    description: str
    expected_relation_names: frozenset[str]
    expected_stable_binding_name: str
    expected_materialized_view_source: str
    expected_materialized_view_target: str


@dataclass(frozen=True)
class InspectManagedTableStateIntegrationTestCase:
    description: str
    expected_active_bindings: tuple[tuple[str, str], ...]
    expected_physical_candidates: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RenderTransformMaterializedViewIntegrationTestCase:
    description: str
    expected_order_id: str
    expected_customer_id: str
    expected_order_total: float


@dataclass(frozen=True)
class MetadataMigrationIntegrationTestCase:
    description: str
    expected_table_names: tuple[str, ...]
    expected_version_rows: tuple[tuple[int], ...]


@dataclass(frozen=True)
class ManifestHistoryIntegrationTestCase:
    description: str
    expected_manifest_ids: tuple[str, ...]
    expected_latest_revision: str


@dataclass(frozen=True)
class LatestNodeStatusIntegrationTestCase:
    description: str
    expected_status_rows: tuple[tuple[str, str], ...]
    expected_drift_rows: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class LegacyNodeResultsSchemaTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class LegacyPublicationMigrationTestCase:
    description: str
    expected_operation: str
    expected_publication_id: str


@dataclass(frozen=True)
class RefreshableViewIntegrationTestCase:
    description: str
    refresh: str
    append: bool
    expected_row_count: int


@dataclass(frozen=True)
class RefreshStateIntegrationTestCase:
    description: str
    refresh: str
    expected_statuses: tuple[str, ...]


@dataclass(frozen=True)
class PostgresRefreshEndToEndTestCase:
    description: str
    source_table: str
    refresh: str
    expected_rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TargetMutationLockIntegrationTestCase:
    description: str
    initial_owner_id: str
    competing_owner_id: str
    expected_initial_owner_id: str
    expected_reacquired_owner_id: str
    expected_error_message: str


@dataclass(frozen=True)
class MissingTargetMutationLockIntegrationTestCase:
    description: str
    expected_table_count_while_locked: int
    expected_table_count_after_release: int


@dataclass(frozen=True)
class StreamBuildMetadataRenameTestCase:
    description: str
    extra_tables: tuple[str, ...]
    expected_error_fragment: str | None
    expected_table_prefix: str


@dataclass(frozen=True)
class WarehouseHealthMissingDeadLettersTestCase:
    description: str
    expected_dead_letters: None


@dataclass(frozen=True)
class WarehouseHealthDeadLettersTestCase:
    description: str
    payload_length: int
    expected_row_count: int
    expected_failed_columns: tuple[str, ...]
    expected_payload_length: int
