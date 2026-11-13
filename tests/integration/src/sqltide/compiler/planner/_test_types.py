from dataclasses import dataclass

from sqltide.compiler.planner.types import (
    PlannedChangeType,
    RebuildExecutionMode,
    TableSchemaChangeKind,
    TableSchemaSeedCompatibility,
)


@dataclass(frozen=True)
class PlannerNoOpAfterPublishIntegrationTestCase:
    description: str
    deployment_id: str
    created_at: str
    boundary_time: str
    expected_rebuild_subtrees: tuple[object, ...]
    expected_steps: tuple[object, ...]


@dataclass(frozen=True)
class PlannerNormalizedTypeNoOpIntegrationTestCase:
    description: str
    deployment_id: str
    created_at: str
    boundary_time: str
    expected_change_type: PlannedChangeType


@dataclass(frozen=True)
class PlannerStorageAfterPublishIntegrationTestCase:
    description: str
    deployment_id: str
    created_at: str
    boundary_time: str
    published_ttl: str
    desired_ttl: str
    published_settings: dict[str, str] | None
    desired_settings: dict[str, str] | None
    expected_change_type: PlannedChangeType


@dataclass(frozen=True)
class PlannerSqlChangeAfterPublishIntegrationTestCase:
    description: str
    deployment_id: str
    created_at: str
    boundary_time: str
    expected_rebuild_root_names: tuple[str, ...]


@dataclass(frozen=True)
class PlannerSchemaChangeAfterPublishIntegrationTestCase:
    description: str
    deployment_id: str
    created_at: str
    boundary_time: str
    changed_pipeline_kind: str
    expected_rebuild_root_names: tuple[str, ...]
    expected_sql_diff_names: tuple[str, ...]
    expected_schema_change_kind: TableSchemaChangeKind | None
    expected_seed_compatibility: TableSchemaSeedCompatibility | None
    expected_execution_mode: RebuildExecutionMode


@dataclass(frozen=True)
class LoadActualStateIntegrationTestCase:
    description: str
    setup_steps: tuple[str, ...]
    expected_actual_object_names: tuple[str, ...]
    expected_error_fragment: str | None


@dataclass(frozen=True)
class LoadActualStateWithoutMetadataIntegrationTestCase:
    description: str
    dropped_metadata_tables: tuple[str, ...]
    expected_actual_object_names: tuple[str, ...]


@dataclass(frozen=True)
class LoadActualStateMixedRootsIntegrationTestCase:
    description: str
    setup_steps: tuple[str, ...]
    expected_actual_object_names: tuple[str, ...]


@dataclass(frozen=True)
class LoadActualStateWithConflictingMetadataIntegrationTestCase:
    description: str
    expected_actual_object_names: tuple[str, ...]


@dataclass(frozen=True)
class LoadActualStateWithLatestObjectStateIntegrationTestCase:
    description: str
    latest_record_deployment_id: str
    latest_record_state_kind: str
    latest_record_query: str
    expected_materialized_view_query: str
