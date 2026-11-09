"""Planner runtime models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqltide.adapter.models import (
    AdapterDirectFingerprintSnapshot,
    AdapterReplayColumns,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import (
    Column,
    KafkaSettings,
    KafkaTableSpec,
    LogicalResourceKey,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    ViewSpec,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    ReplayLineageMode,
    ReplayOnChangeMode,
    RowErrorPolicy,
)
from sqltide.compiler.planner.types import (
    DeploymentAction,
    DeploymentPhase,
    DirectPlanReason,
    DirectRelationAction,
    DirectResourceKind,
    DirectSelectionMode,
    DirectSqlBaselineStatus,
    PlannedChangeType,
    RebuildExecutionMode,
    RebuildStrategy,
    TableSchemaChangeKind,
    TableSchemaSeedCompatibility,
)


@dataclass(frozen=True)
class PlanningWarehouseSnapshot:
    """Immutable live catalog and persisted state captured for one plan."""

    catalog: CatalogSnapshot
    object_state_records: tuple[ObjectStateRecord, ...]


@dataclass(frozen=True)
class ActualKafkaTable:
    """A normalized actual Kafka engine table."""

    key: ObjectKey
    spec: KafkaTableSpec

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def columns(self) -> tuple[Column, ...]:
        return self.spec.columns

    @property
    def kafka(self) -> KafkaSettings:
        return self.spec.kafka


@dataclass(frozen=True)
class ActualTable:
    """A normalized actual managed ClickHouse table."""

    key: ObjectKey
    spec: TableSpec

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def columns(self) -> tuple[Column, ...]:
        return self.spec.columns

    @property
    def engine(self) -> str:
        return self.spec.storage.engine

    @property
    def order_by(self) -> tuple[str, ...]:
        return self.spec.storage.order_by

    @property
    def partition_by(self) -> str | None:
        return self.spec.storage.partition_by

    @property
    def ttl(self) -> str | None:
        return self.spec.storage.ttl

    @property
    def settings(self) -> Mapping[str, str] | None:
        return self.spec.storage.settings


@dataclass(frozen=True)
class ActualMaterializedView:
    """A normalized actual materialized view."""

    key: ObjectKey
    spec: MaterializedViewSpec

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def source_table_name(self) -> str:
        return self.spec.source_table_name

    @property
    def target_table_name(self) -> str:
        return self.spec.target_table_name

    @property
    def query(self) -> str:
        return self.spec.query


@dataclass(frozen=True)
class ActualView:
    """A normalized actual ordinary view."""

    key: ObjectKey
    spec: ViewSpec

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def query(self) -> str:
        return self.spec.query


@dataclass(frozen=True)
class ActualState:
    """Project-level flat actual object graph."""

    objects: tuple[ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView, ...]


@dataclass(frozen=True)
class RootDeploymentInspection:
    """Inspection result for one managed root logical table."""

    root_key: ObjectKey
    state_kind: str
    active_deployment_id: str | None


@dataclass(frozen=True)
class ActualStateInspection:
    """Live and persisted inputs needed to assemble actual state."""

    existing_names: frozenset[str]
    active_deployment_by_root: dict[ObjectKey, RootDeploymentInspection]
    object_state_by_deployment_and_key: dict[tuple[str, ObjectKey], ObjectStateRecord]
    latest_object_state_by_key: dict[ObjectKey, ObjectStateRecord]
    active_physical_names_by_logical_name: dict[str, str]
    active_table_specs_by_name: dict[str, TableSpec]


@dataclass(frozen=True)
class ObjectStateMetadataRow:
    """Row shape for persisted object-state metadata."""

    deployment_id: str
    database_name: str | None
    object_type: str
    object_name: str
    normalized_fingerprint: str
    normalized_query: str | None
    recorded_at: str
    observation_id: str
    state_kind: str


@dataclass(frozen=True)
class TableNameSystemRow:
    """Row shape for system table name lookups."""

    name: str


@dataclass(frozen=True)
class TableColumnSystemRow:
    """Row shape for system column inspection."""

    table_name: str
    name: str
    type: str
    default_expression: str | None


@dataclass(frozen=True)
class TableStorageSystemRow:
    """Row shape for system table storage inspection."""

    table_name: str
    engine: str
    sorting_key: str
    partition_key: str | None


@dataclass(frozen=True)
class PlannedObjectChange:
    """A planner-local object change classification."""

    key: ObjectKey
    change_type: PlannedChangeType | str
    force_full_refresh: bool = False
    forced_start_time: str | None = None
    schema_change_kind: TableSchemaChangeKind | str | None = None
    seed_compatibility: TableSchemaSeedCompatibility | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "change_type", PlannedChangeType(self.change_type))
        if self.schema_change_kind is not None:
            object.__setattr__(
                self,
                "schema_change_kind",
                TableSchemaChangeKind(self.schema_change_kind),
            )
        if self.seed_compatibility is not None:
            object.__setattr__(
                self,
                "seed_compatibility",
                TableSchemaSeedCompatibility(self.seed_compatibility),
            )


@dataclass(frozen=True)
class PlannedSqlDiff:
    """A unified SQL diff for one changed planned object."""

    key: ObjectKey
    object_type: str
    name: str
    diff_lines: tuple[str, ...]


@dataclass(frozen=True)
class RebuildSubtree:
    """A transitive desired-object rebuild subtree."""

    root_key: ObjectKey
    affected_keys: tuple[ObjectKey, ...]
    upstream_boundary_key: ObjectKey
    strategy: RebuildStrategy | str
    replay_required: bool = True
    execution_mode: RebuildExecutionMode | str = RebuildExecutionMode.FULL_REBUILD
    forced_full_refresh: bool = False
    forced_start_time: str | None = None
    requested_start_time: str | None = None
    configured_backfill_mode: ReplayOnChangeMode | str | None = None
    execution_lookback_seconds: int | None = None
    history_preserving_bounded_supported: bool = True
    resolved_bounded_replay_fallback: BoundedReplayFallback | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", RebuildStrategy(self.strategy))
        object.__setattr__(self, "execution_mode", RebuildExecutionMode(self.execution_mode))
        if self.configured_backfill_mode is not None:
            object.__setattr__(
                self,
                "configured_backfill_mode",
                ReplayOnChangeMode(self.configured_backfill_mode),
            )
        if self.resolved_bounded_replay_fallback is not None:
            object.__setattr__(
                self,
                "resolved_bounded_replay_fallback",
                BoundedReplayFallback(self.resolved_bounded_replay_fallback),
            )


@dataclass(frozen=True)
class DeploymentStep:
    """A staged deployment step for a rebuild plan."""

    step_id: str
    phase: DeploymentPhase | str
    action: DeploymentAction | str
    root_key: ObjectKey
    target_key: ObjectKey | None = None
    physical_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", DeploymentPhase(self.phase))
        object.__setattr__(self, "action", DeploymentAction(self.action))


@dataclass(frozen=True)
class PreparedShadowObject:
    """A deterministic physical shadow-object identity for a logical object."""

    logical_key: ObjectKey
    physical_name: str
    logical_model_name: str


@dataclass(frozen=True)
class PlannerWarning:
    """A planner-visible warning about rollout semantics."""

    warning_code: str
    message: str
    root_key: ObjectKey
    target_key: ObjectKey | None = None


@dataclass(frozen=True)
class DeploymentPlan:
    """A conservative staged deployment plan."""

    deployment_id: str | None
    object_changes: tuple[PlannedObjectChange, ...]
    rebuild_subtrees: tuple[RebuildSubtree, ...]
    steps: tuple[DeploymentStep, ...]
    prepared_shadow_objects: tuple[PreparedShadowObject, ...]
    warnings: tuple[PlannerWarning, ...]
    sql_diffs: tuple[PlannedSqlDiff, ...] = ()


@dataclass(frozen=True)
class PreparedObjectMapping:
    """A logical-to-physical prepared object mapping for a deployment."""

    logical_key: ObjectKey
    physical_name: str
    logical_model_name: str


@dataclass(frozen=True)
class ObjectStateRecord:
    """Framework-owned applied state for a logical object."""

    deployment_id: str
    key: ObjectKey
    normalized_fingerprint: str
    normalized_query: str | None
    recorded_at: str
    observation_id: str = ""
    state_kind: str = "deployment"


@dataclass(frozen=True)
class DeploymentRecord:
    """Stored staged deployment metadata."""

    deployment_id: str
    created_at: str
    status: str
    replay_lineage_mode: ReplayLineageMode | str
    selected_root_keys: tuple[ObjectKey, ...]
    warning_codes: tuple[str, ...]
    prepared_object_mappings: tuple[PreparedObjectMapping, ...]
    workflow_fingerprint: str = ""
    boundary_time: str | None = None
    tool_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "replay_lineage_mode", ReplayLineageMode(self.replay_lineage_mode))


@dataclass(frozen=True)
class DeploymentWatermarkRecord:
    """Stored replay-boundary or progress metadata for a deployment."""

    deployment_id: str
    root_key: ObjectKey
    anchor_key: ObjectKey
    boundary_key: str
    cutoff_value: str


@dataclass(frozen=True)
class PublishEventRecord:
    """Stored publish/activation history for one deployment."""

    deployment_id: str
    published_at: str
    logical_view_names: tuple[str, ...]
    database: str = ""
    physical_relation_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataState:
    """Project-level stored metadata-state records."""

    object_states: tuple[ObjectStateRecord, ...]
    deployments: tuple[DeploymentRecord, ...]
    deployment_watermarks: tuple[DeploymentWatermarkRecord, ...]
    publish_events: tuple[PublishEventRecord, ...]


@dataclass(frozen=True)
class DirectWarehouseSnapshot:
    """Immutable live catalog captured for one Direct plan."""

    catalog: CatalogSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
        status="absent",
        baselines=(),
    )


@dataclass(frozen=True)
class DirectPlanEntry:
    """One logical model the direct plan will tear down and rebuild."""

    model_key: LogicalResourceKey
    reason: DirectPlanReason | str
    relation_names: tuple[str, ...]
    resource_kinds: tuple[DirectResourceKind | str, ...]
    driving_input_key: LogicalResourceKey | None
    is_replay_root: bool
    sql_change: DirectSqlChange | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", DirectPlanReason(self.reason))
        object.__setattr__(
            self,
            "resource_kinds",
            tuple(DirectResourceKind(kind) for kind in self.resource_kinds),
        )


@dataclass(frozen=True)
class DirectSqlChange:
    """Optional logical SQL baseline comparison for one selected direct model."""

    status: DirectSqlBaselineStatus | str
    current_sql: str
    current_hash: str
    previous_sql: str | None
    previous_hash: str | None
    unified_diff: str | None
    warning: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DirectSqlBaselineStatus(self.status))


@dataclass(frozen=True)
class DirectPrerequisite:
    """One upstream resource that must exist but is never executed."""

    key: LogicalResourceKey
    relation_names: tuple[str, ...]
    present: bool
    framework_managed: bool = False


@dataclass(frozen=True)
class DirectReplayRoot:
    """One executed model replayed from its own preserved driving input."""

    model_key: LogicalResourceKey
    driving_input_key: LogicalResourceKey
    driving_input_relation_name: str
    driving_input_replay_columns: AdapterReplayColumns
    replay_boundary_mode: ReplayLineageMode | str
    propagated_model_keys: tuple[LogicalResourceKey, ...]
    has_aggregate_semantics: bool = False
    replay_settings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "replay_boundary_mode", ReplayLineageMode(self.replay_boundary_mode)
        )


@dataclass(frozen=True)
class DirectRelationOperation:
    """One destructive or constructive relation action in dependency-safe order."""

    relation_name: str
    action: DirectRelationAction | str
    model_key: LogicalResourceKey
    resource_kind: DirectResourceKind | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DirectRelationAction(self.action))
        object.__setattr__(self, "resource_kind", DirectResourceKind(self.resource_kind))


@dataclass(frozen=True)
class DirectSourceDriftSelection:
    """One owning pipeline a changed selection rebuilds so a drifted managed source is recreated."""

    source_key: LogicalResourceKey
    pipeline_name: str
    reader_pipeline_names: tuple[str, ...]
    drifted_setting_names: tuple[str, ...]
    model_keys: tuple[LogicalResourceKey, ...]


@dataclass(frozen=True)
class DirectRowErrorPolicyChange:
    """A view-only switch of one live model's row-error policy, without replay."""

    model_key: LogicalResourceKey
    view_name: str
    dead_letter_view_name: str
    desired_policy: RowErrorPolicy
    landing_view_names: tuple[str, ...]
    logical_view_name: str = ""
    logical_dead_letter_view_name: str = ""


@dataclass(frozen=True)
class DirectPlan:
    """One deterministic direct-mode execution plan for a selected closure."""

    database: str
    user_scope: tuple[LogicalResourceKey, ...]
    execution_scope: tuple[LogicalResourceKey, ...]
    prerequisite_scope: tuple[DirectPrerequisite, ...]
    entries: tuple[DirectPlanEntry, ...]
    replay_roots: tuple[DirectReplayRoot, ...]
    teardown_operations: tuple[DirectRelationOperation, ...]
    creation_operations: tuple[DirectRelationOperation, ...]
    selection_mode: DirectSelectionMode | str = DirectSelectionMode.EXPLICIT
    prerequisite_execution_scope: tuple[LogicalResourceKey, ...] = ()
    warnings: tuple[PlannerWarning, ...] = ()
    effective_start_time: str | None = None
    source_drift_selections: tuple[DirectSourceDriftSelection, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "selection_mode", DirectSelectionMode(self.selection_mode))
