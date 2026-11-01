"""ClickHouse system-row models used while assembling a neutral catalog."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClickHouseCatalogRelationRow:
    name: str
    engine: str
    sorting_key: str
    partition_key: str
    create_table_query: str
    as_select: str
    uuid: str


@dataclass(frozen=True)
class ClickHouseCatalogColumnRow:
    table_name: str
    name: str
    type: str
    default_expression: str | None


@dataclass(frozen=True)
class ActiveBindingSystemRow:
    """One stable view binding read from ClickHouse system tables."""

    name: str
    as_select: str


@dataclass(frozen=True)
class PhysicalCandidateSystemRow:
    """One deployment-suffixed physical table candidate."""

    name: str


@dataclass(frozen=True)
class ClickHouseMetadataStatement:
    """A ClickHouse metadata insert statement and its row mappings."""

    table: str
    sql: str
    rows: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class ClickHouseDeploymentInventoryRow:
    """One persisted deployment row used by lifecycle cleanup."""

    deployment_id: str
    created_at: str
    replay_lineage_mode: str
    header_present: bool = True


@dataclass(frozen=True)
class ClickHouseObjectStateInventoryRow:
    """One structured deployment object-state row used by lifecycle commands."""

    deployment_id: str
    database_name: str | None
    object_type: str
    object_name: str
    physical_name: str | None
    logical_model_name: str | None
    is_selected_root: bool
    observed_at: str


@dataclass(frozen=True)
class ClickHousePublishEventInventoryRow:
    """One persisted publish event used by lifecycle cleanup."""

    publication_id: str
    deployment_id: str
    published_at: str
    database_name: str
    logical_view_name: str
    physical_relation_name: str
    operation: str = "promote"
    previous_deployment_id: str | None = None


@dataclass(frozen=True)
class ClickHouseReplayColumn:
    """One ClickHouse relation column needed during replay realization."""

    name: str
    type: str


@dataclass(frozen=True)
class ClickHouseReadinessCountRow:
    """One count returned while comparing readiness."""

    value: int


@dataclass(frozen=True)
class ClickHouseReadinessColumnRow:
    """One column name returned while inferring replay semantics."""

    name: str


@dataclass(frozen=True)
class ClickHouseReadinessOffsetRow:
    """One aggregate offset-readiness result row."""

    active_partition_count: int
    staged_partition_count: int
    partitions_compared: int
    missing_staged_partition_count: int
    missing_freshness_partition_count: int
    lagging_partition_count: int
    max_offset_gap: int
    average_offset_gap: float
    max_lag_seconds: float | None
    average_lag_seconds: float | None


@dataclass(frozen=True)
class ClickHouseReadinessScalarRow:
    """One aggregate scalar-readiness result row."""

    active_min_value: str | None
    active_max_value: str | None
    staged_min_value: str | None
    staged_max_value: str | None
    lag_seconds: float | None


@dataclass(frozen=True)
class ClickHouseReadinessCreateQueryRow:
    """One materialized-view DDL result used to resolve its source."""

    create_table_query: str
