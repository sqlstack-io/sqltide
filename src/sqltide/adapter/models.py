"""Neutral adapter identity, connection, and result models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqltide.adapter.constants import REDACTED_SECRET_PLACEHOLDER
from sqltide.adapter.exceptions import AdapterResultError
from sqltide.adapter.types import (
    AdapterOptionalStateStatus,
    AdapterReplayBoundaryMode,
    AdapterReplayLowerBoundMode,
    AdapterReplaySeedMode,
    AdapterWarehouseHealthAvailability,
    AdapterWarehouseHealthStatus,
)


@dataclass(frozen=True)
class AdapterIdentity:
    """The registered name of one adapter implementation."""

    name: str


@dataclass(frozen=True)
class AdapterCapabilities:
    """Capabilities implemented by one adapter."""

    virtual_environments: bool
    managed_source_kinds: frozenset[str]
    replay_boundary_modes: frozenset[AdapterReplayBoundaryMode]
    history_prefix_seed: bool
    stable_logical_bindings: bool
    per_relation_atomic_replace: bool
    graph_atomic_publish: bool
    set_difference_comparison: bool
    direct_rebuild: bool


@dataclass(frozen=True)
class AdapterReplayCoverageRange:
    """One retained lineage range required to reproduce a direct target."""

    driving_input_relation_name: str
    replay_boundary_mode: AdapterReplayBoundaryMode | str
    boundary_key: str
    source_partition_column_name: str | None
    source_position_column_name: str
    source_timestamp_column_name: str | None
    lower_value: str
    upper_value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "replay_boundary_mode", AdapterReplayBoundaryMode(self.replay_boundary_mode)
        )


@dataclass(frozen=True)
class AdapterSetDifferenceTarget:
    """One neutral actual/expected bag comparison requested from an adapter."""

    name: str
    column_names: tuple[str, ...]
    ctes: tuple[tuple[str, str], ...]
    actual_query: str
    expected_query: str | None


@dataclass(frozen=True)
class AdapterSetDifferenceComparisonRequest:
    """One executable statement containing every SQL-test comparison target."""

    targets: tuple[AdapterSetDifferenceTarget, ...]


@dataclass(frozen=True)
class AdapterColumn:
    """One column in a neutral adapter resource request."""

    name: str
    type: str
    default_expression: str | None = None


@dataclass(frozen=True)
class AdapterManagedSource:
    """A managed streaming source to realize in a warehouse."""

    source_kind: str
    name: str
    columns: tuple[AdapterColumn, ...]
    broker_list: str
    topic: str
    consumer_group: str
    format: str
    settings: tuple[tuple[str, str], ...] = ()
    naming_macro_fingerprint: str | None = None


@dataclass(frozen=True)
class AdapterTable:
    """A managed table to realize in a warehouse."""

    name: str
    columns: tuple[AdapterColumn, ...]
    engine: str
    order_by: tuple[str, ...]
    partition_by: str | None = None
    ttl: str | None = None
    settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AdapterMaterializedView:
    """A materialized view to realize between warehouse relations."""

    name: str
    source_relation_name: str
    target_relation_name: str
    query: str
    database_template: str
    refresh: str | None = None
    append: bool = True
    dead_letters: bool = False


@dataclass(frozen=True)
class AdapterView:
    """An ordinary query view to realize in a warehouse."""

    name: str
    query: str
    database_template: str


@dataclass(frozen=True)
class AdapterManagedSourceRealizationRequest:
    """Logical managed-source fields needed for adapter realization."""

    logical_name: str
    source_kind: str
    broker_list: str
    topic: str
    consumer_group: str | None
    format: str
    project_name: str | None = None
    target_name: str | None = None
    ttl: str | None = None
    settings: tuple[tuple[str, str], ...] = ()
    naming_macro_fingerprint: str | None = None


@dataclass(frozen=True)
class AdapterAdoptedSourceRealizationRequest:
    """Logical adopted-source fields needed for adapter realization."""

    logical_name: str
    relation_name: str


@dataclass(frozen=True)
class AdapterSourceRealization:
    """One logical source mapped to its adapter relation and resources."""

    relation_name: str
    resources: tuple[AdapterManagedSource | AdapterTable | AdapterMaterializedView, ...]


@dataclass(frozen=True)
class AdapterRefreshState:
    """The warehouse-reported refresh state of one scheduled relation."""

    view_name: str
    status: str
    last_refresh_at: str | None
    last_success_at: str | None
    next_refresh_at: str | None
    exception: str | None


@dataclass(frozen=True)
class AdapterModelRealizationRequest:
    """One semantically compiled logical model ready for adapter realization."""

    logical_name: str
    target_relation_name: str
    source_relation_name: str
    resolved_query: str
    resolved_database_template: str
    columns: tuple[AdapterColumn, ...]
    engine: str
    order_by: tuple[str, ...]
    partition_by: str | None = None
    ttl: str | None = None
    settings: tuple[tuple[str, str], ...] = ()
    refresh: str | None = None
    append: bool = True
    dead_letter_query: str | None = None
    dead_letter_database_template: str | None = None


@dataclass(frozen=True)
class AdapterViewRealizationRequest:
    """One compiled query-only view ready for adapter realization."""

    logical_name: str
    target_relation_name: str
    resolved_query: str
    resolved_database_template: str


@dataclass(frozen=True)
class AdapterModelRealization:
    """One logical model mapped to its adapter relation and resources."""

    relation_name: str
    resources: tuple[AdapterTable | AdapterMaterializedView | AdapterView, ...]


@dataclass(frozen=True)
class AdapterStableView:
    """A stable logical view to bind to one physical relation."""

    name: str
    target_relation_name: str


@dataclass(frozen=True)
class AdapterStableBinding:
    """One logical relation bound to one physical relation."""

    database: str
    logical_name: str
    physical_name: str


@dataclass(frozen=True)
class AdapterStableBindingRemoval:
    """One obsolete framework-owned stable binding to remove."""

    database: str
    logical_name: str


@dataclass(frozen=True)
class AdapterBindingReplacementRequest:
    """A set of stable bindings to replace during one publish operation."""

    bindings: tuple[AdapterStableBinding, ...]
    removals: tuple[AdapterStableBindingRemoval, ...] = ()


@dataclass(frozen=True)
class AdapterRelationCleanupRequest:
    """Physical relations to remove from one database."""

    database: str
    relation_names: tuple[str, ...]


@dataclass(frozen=True)
class AdapterTargetMutationLock:
    """Exclusive warehouse-backed ownership of one target mutation slot."""

    database: str
    owner_id: str


@dataclass(frozen=True)
class AdapterReadinessRootRequest:
    """One staged root whose live and candidate relations must be compared."""

    database: str
    logical_name: str
    staged_relation_name: str
    active_exists: bool


@dataclass(frozen=True)
class AdapterReadinessRequest:
    """A mode-neutral readiness comparison request."""

    roots: tuple[AdapterReadinessRootRequest, ...]


@dataclass(frozen=True)
class AdapterReadinessOffsetSummary:
    """Adapter-observed partition and lag metrics for offset replay."""

    active_partition_count: int
    staged_partition_count: int
    partitions_compared: int
    missing_staged_partition_count: int
    missing_freshness_partition_count: int
    lagging_partition_count: int
    max_offset_gap: int
    average_offset_gap: float
    lag_boundary_column: str | None
    max_lag_seconds: float | None
    average_lag_seconds: float | None


@dataclass(frozen=True)
class AdapterReadinessScalarSummary:
    """Adapter-observed range and lag metrics for scalar replay."""

    active_min_value: str | None
    active_max_value: str | None
    staged_min_value: str | None
    staged_max_value: str | None
    lag_seconds: float | None


@dataclass(frozen=True)
class AdapterReadinessRootObservation:
    """Warehouse observations for one requested staged root."""

    root: AdapterReadinessRootRequest
    staged_exists: bool
    active_row_count: int | None
    staged_row_count: int | None
    replay_source_name: str | None
    replay_source_row_count: int | None
    replay_boundary_mode: AdapterReplayBoundaryMode | None
    offset_summary: AdapterReadinessOffsetSummary | None
    scalar_summary: AdapterReadinessScalarSummary | None


@dataclass(frozen=True)
class AdapterPhysicalRelationMapping:
    """A logical relation mapped to its replay-time physical relation."""

    logical_name: str
    physical_name: str


@dataclass(frozen=True)
class AdapterReplayQuery:
    """A compiler-analyzed query ready for adapter replay realization."""

    query: str
    physical_relation_mappings: tuple[AdapterPhysicalRelationMapping, ...]
    aggregate_semantics: bool


@dataclass(frozen=True)
class AdapterReplayBoundary:
    """One inclusive replay cutoff, optionally scoped to a partition."""

    boundary_key: str
    cutoff_value: str
    cutoff_inclusive: bool
    partition_value: str | None = None


@dataclass(frozen=True)
class AdapterReplayColumns:
    """Physical boundary columns used to read an anchor relation."""

    partition: str
    offset: str
    timestamp: str
    landed_at: str
    cursor: str


@dataclass(frozen=True)
class AdapterReplayWindow:
    """The lower-bound policy for one replay root."""

    lower_bound_mode: AdapterReplayLowerBoundMode
    lower_bound_inclusive: bool
    boundary_time: str
    forced_start_time: str | None
    lookback_seconds: int | None


@dataclass(frozen=True)
class AdapterReplayRelations:
    """Logical and physical relations participating in one replay root."""

    root: str
    source: str
    anchor: str
    target: str


@dataclass(frozen=True)
class AdapterReplayRequest:
    """A mode-neutral request to seed and replay one rebuild root."""

    mode: AdapterReplayBoundaryMode
    database: str
    relations: AdapterReplayRelations
    replay_query: AdapterReplayQuery
    boundaries: tuple[AdapterReplayBoundary, ...]
    columns: AdapterReplayColumns
    window: AdapterReplayWindow
    seed_mode: AdapterReplaySeedMode
    target_column_names: tuple[str, ...]
    settings: tuple[tuple[str, str], ...] = ()
    filter_boundaries_at_source: bool = False
    dedupe_offsets: bool = False


@dataclass(frozen=True)
class AdapterReplayCoverageRequest:
    """A request for the retained physical ranges selected by one replay window."""

    replay: AdapterReplayRequest
    boundary_column_type: str | None
    count_anchor_rows: bool = False


@dataclass(frozen=True)
class AdapterReplayLowerBound:
    """One captured replay lower bound in adapter-neutral string form."""

    value: str
    partition_value: str | None = None


@dataclass(frozen=True)
class AdapterCapturedReplayRequest:
    """A replay rendered from boundaries captured by the current process."""

    replay: AdapterReplayRequest
    boundary_column_type: str | None
    lower_bounds: tuple[AdapterReplayLowerBound, ...]


@dataclass(frozen=True)
class AdapterDirectFingerprintRecord:
    """One logical SQL baseline recorded after direct materialization."""

    fingerprint_id: str
    logical_model_identity: str
    definition_sql: str
    definition_hash: str
    identity_metadata: str
    workflow_id: str
    tool_version: str
    applied_at: str | None = None


@dataclass(frozen=True)
class AdapterDirectFingerprintSnapshot:
    """Explicit availability and latest logical direct SQL baselines."""

    status: AdapterOptionalStateStatus | str
    baselines: tuple[AdapterDirectFingerprintRecord, ...]
    warning: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdapterOptionalStateStatus(self.status))


@dataclass(frozen=True)
class AdapterManifestResource:
    """One physical resource recorded in a published project manifest."""

    pipeline_name: str
    logical_type: str
    logical_name: str
    resource_role: str
    resource_database: str
    resource_name: str
    resource_kind: str


@dataclass(frozen=True)
class AdapterManifest:
    """One complete, immutable publication manifest."""

    manifest_id: str
    invocation_id: str
    project_identity: str
    target_name: str
    target_database: str
    is_production: bool
    project_revision: str | None
    manifest_fingerprint: str
    manifest_version: int
    pipelines: tuple[str, ...]
    resources: tuple[AdapterManifestResource, ...]
    tool_version: str
    published_at: str


@dataclass(frozen=True)
class AdapterManifestSnapshot:
    """Availability and complete publication history for one project target."""

    status: AdapterOptionalStateStatus | str
    manifests: tuple[AdapterManifest, ...]
    warning: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdapterOptionalStateStatus(self.status))


@dataclass(frozen=True)
class AdapterDeploymentReplayRequest:
    """A replay whose dynamic boundaries live in deployment watermark metadata."""

    replay: AdapterReplayRequest
    metadata_database: str
    deployment_id: str
    boundary_column_type: str | None
    active_relation_name: str
    active_column_names: tuple[str, ...]
    anchor_column_names: tuple[str, ...]


@dataclass(frozen=True)
class AdapterMetadataObjectKey:
    """A logical object identity stored in framework metadata."""

    database: str | None
    object_type: str
    name: str


@dataclass(frozen=True)
class AdapterPreparedObjectMapping:
    """A persisted logical-to-physical object mapping."""

    logical_key: AdapterMetadataObjectKey
    physical_name: str
    logical_model_name: str


@dataclass(frozen=True)
class AdapterObjectStateRecord:
    """One adapter-neutral object-state metadata record."""

    deployment_id: str
    key: AdapterMetadataObjectKey
    normalized_fingerprint: str
    normalized_query: str | None
    recorded_at: str
    observation_id: str = ""
    state_kind: str = "deployment"
    physical_database_name: str | None = None
    physical_relation_name: str | None = None
    logical_model_database: str | None = None
    logical_model_name: str | None = None
    is_selected_root: bool = False


@dataclass(frozen=True)
class AdapterDeploymentRecord:
    """One adapter-neutral staged deployment record."""

    deployment_id: str
    created_at: str
    status: str
    replay_lineage_mode: str
    selected_root_keys: tuple[AdapterMetadataObjectKey, ...]
    warning_codes: tuple[str, ...]
    prepared_object_mappings: tuple[AdapterPreparedObjectMapping, ...]
    workflow_fingerprint: str = ""
    boundary_time: str | None = None
    tool_version: str = ""


@dataclass(frozen=True)
class AdapterDeploymentWatermarkRecord:
    """One adapter-neutral replay watermark record."""

    deployment_id: str
    root_key: AdapterMetadataObjectKey
    anchor_key: AdapterMetadataObjectKey
    boundary_key: str
    cutoff_value: str
    lower_value: str | None = None
    cutoff_inclusive: bool = True
    captured_at: str = "1970-01-01 00:00:00.000"


@dataclass(frozen=True)
class AdapterPublishEventRecord:
    """One adapter-neutral publish-history record."""

    deployment_id: str
    published_at: str
    logical_view_names: tuple[str, ...]
    publication_id: str = ""
    bindings: tuple[AdapterStableBinding, ...] = ()
    operation: str = "promote"
    previous_deployment_id: str | None = None


@dataclass(frozen=True)
class AdapterInvocationRecord:
    """One immutable terminal CLI invocation observation."""

    invocation_id: str
    project_identity: str
    target_identity: str
    command: str
    mode: str | None
    outcome: str
    exit_code: int
    materialized_outcome: str | None
    deployment_id: str | None
    workflow_id: str | None
    selected_node_count: int
    started_at: str
    completed_at: str
    duration_ms: int
    error_message: str | None
    summary_json: str
    tool_version: str
    artifact_project_dir: str | None = None


@dataclass(frozen=True)
class AdapterNodeResultRecord:
    """One immutable terminal audit or test result observation."""

    result_id: str
    invocation_id: str
    node_kind: str
    node_name: str
    binding_key: str
    definition_fingerprint: str
    execution_fingerprint: str
    target_identity: str
    trigger: str
    scheduled_for: str | None
    cadence_seconds: int | None
    warmup_seconds: int
    status: str
    severity: str | None
    failure_count: int
    completed_at: str
    payload_json: str
    error_message: str | None


@dataclass(frozen=True)
class AdapterRunEventRecord:
    """One step-granular workflow event streamed while a run executes."""

    invocation_id: str
    sequence: int
    event_kind: str
    step_id: str | None
    phase: str | None
    payload_json: str


@dataclass(frozen=True)
class AdapterRunStatementRecord:
    """One display-safe warehouse statement durably associated with a run."""

    invocation_id: str
    statement_sequence: int
    step_id: str
    phase: str
    intent: str
    sql: str
    sql_sha256: str
    workflow_sha256: str
    workflow_revision: int


@dataclass(frozen=True)
class AdapterSensorCheckpointRecord:
    """One append-only latest-wins sensor stream position."""

    sensor_name: str
    source: str
    position_completed_at: str
    position_result_id: str


@dataclass(frozen=True)
class AdapterSensorTickRecord:
    """One persisted sensor evaluation attempt."""

    tick_id: str
    sensor_name: str
    definition_fingerprint: str
    kind: str
    event_id: str | None
    event_kind: str | None
    attempt: int
    status: str
    started_at: str
    completed_at: str | None
    error_message: str | None
    skip_reason: str | None
    cursor: str | None


@dataclass(frozen=True)
class AdapterSensorStepRecord:
    """One durable step marker keyed by sensor, event, and step key."""

    sensor_name: str
    event_id: str
    step_key: str
    policy: str
    status: str
    attempt: int
    result_json: str | None
    error_message: str | None


@dataclass(frozen=True)
class AdapterSensorOverrideRecord:
    """One append-only operator override of a sensor's declared status."""

    override_id: str
    sensor_name: str
    status: str
    actor: str | None


@dataclass(frozen=True)
class AdapterSensorLeaseRecord:
    """One warehouse-time dispatch lease claim."""

    lease_name: str
    owner_id: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class AdapterSensorState:
    """Sensor state records persisted in one batch."""

    checkpoints: tuple[AdapterSensorCheckpointRecord, ...] = ()
    ticks: tuple[AdapterSensorTickRecord, ...] = ()
    steps: tuple[AdapterSensorStepRecord, ...] = ()
    overrides: tuple[AdapterSensorOverrideRecord, ...] = ()
    leases: tuple[AdapterSensorLeaseRecord, ...] = ()


@dataclass(frozen=True)
class AdapterCurrentQualityNode:
    """One current manifest node joined to persisted terminal history."""

    node_kind: str
    node_name: str
    binding_key: str
    definition_fingerprint: str
    execution_fingerprint: str
    cadence_seconds: int | None = None
    warmup_seconds: int = 0


@dataclass(frozen=True)
class AdapterDeploymentInventory:
    """Persisted deployments and publish events used for lifecycle cleanup."""

    deployments: tuple[AdapterDeploymentRecord, ...]
    publish_events: tuple[AdapterPublishEventRecord, ...]


@dataclass(frozen=True)
class AdapterMetadataState:
    """A batch of framework metadata records for adapter persistence."""

    object_states: tuple[AdapterObjectStateRecord, ...]
    deployments: tuple[AdapterDeploymentRecord, ...]
    deployment_watermarks: tuple[AdapterDeploymentWatermarkRecord, ...]
    publish_events: tuple[AdapterPublishEventRecord, ...]
    invocations: tuple[AdapterInvocationRecord, ...] = ()
    node_results: tuple[AdapterNodeResultRecord, ...] = ()


@dataclass(frozen=True)
class CatalogIdentity:
    """Stable identity of one adapter/database catalog observation."""

    adapter: AdapterIdentity
    database: str


@dataclass(frozen=True)
class CatalogColumn:
    """One warehouse relation column observed by an adapter."""

    name: str
    type: str
    default_expression: str | None = None


@dataclass(frozen=True)
class CatalogRelation:
    """One immutable warehouse relation observation."""

    name: str
    engine: str
    columns: tuple[CatalogColumn, ...]
    full_engine: str | None = None
    order_by: tuple[str, ...] = ()
    partition_by: str | None = None
    ttl: str | None = None
    settings: tuple[tuple[str, str], ...] = ()
    definition_sql: str | None = None
    query_sql: str | None = None
    source_relation_names: tuple[str, ...] = ()
    target_relation_name: str | None = None
    stable_binding_name: str | None = None
    ownership_generation: str | None = None

    @property
    def storage_engine(self) -> str:
        """Return the engine including storage arguments when available."""

        return self.full_engine or self.engine

    @property
    def source_relation_name(self) -> str | None:
        """Return the first physical source when one exists."""

        return self.source_relation_names[0] if self.source_relation_names else None


@dataclass(frozen=True)
class CatalogSnapshot:
    """One immutable point-in-time catalog for an adapter and database."""

    identity: CatalogIdentity
    warehouse_timezone: str
    relations: tuple[CatalogRelation, ...]

    def relation(self, name: str) -> CatalogRelation | None:
        """Return one relation by unqualified name when it exists."""

        return next((relation for relation in self.relations if relation.name == name), None)

    def relation_names(self) -> frozenset[str]:
        """Return every observed unqualified relation name."""

        return frozenset(relation.name for relation in self.relations)


@dataclass(frozen=True)
class InspectedActiveTableBinding:
    """A stable logical view pointing at an active physical table."""

    database: str
    logical_name: str
    physical_name: str


@dataclass(frozen=True)
class InspectedPhysicalTableCandidate:
    """A deployment-suffixed physical model candidate for a logical root."""

    database: str
    logical_name: str
    physical_name: str
    object_type: str = "table"


@dataclass(frozen=True)
class InspectedManagedTableState:
    """Managed table state used for active-deployment resolution."""

    active_bindings: tuple[InspectedActiveTableBinding, ...]
    physical_candidates: tuple[InspectedPhysicalTableCandidate, ...]


@dataclass(frozen=True, repr=False)
class AdapterConnectionConfig:
    """Resolved, format-neutral connection settings for one adapter."""

    host: str
    port: int
    username: str
    password: str
    database: str | None = None
    settings: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        """Render the configuration without exposing the password."""

        return (
            f"{type(self).__name__}(host={self.host!r}, port={self.port!r}, "
            f"username={self.username!r}, password={REDACTED_SECRET_PLACEHOLDER!r}, "
            f"database={self.database!r}, settings={self.settings!r})"
        )


@dataclass(frozen=True)
class AdapterQueryResult:
    """A normalized query result returned by the adapter boundary."""

    rows: tuple[tuple[object, ...], ...]
    column_names: tuple[str, ...] = ()

    def named_rows(self) -> tuple[Mapping[str, object], ...]:
        """Return rows keyed by the query's column names."""

        if not self.column_names:
            if not self.rows:
                return ()
            raise AdapterResultError("Query result does not include column names")
        return tuple(dict(zip(self.column_names, row, strict=True)) for row in self.rows)


@dataclass(frozen=True)
class AdapterWarehouseDisk:
    """Capacity and severity for one configured warehouse disk."""

    name: str
    path: str | None
    disk_type: str | None
    total_bytes: int | None
    free_bytes: int | None
    unreserved_bytes: int | None
    keep_free_bytes: int | None
    status: AdapterWarehouseHealthStatus | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdapterWarehouseHealthStatus(self.status))


@dataclass(frozen=True)
class AdapterWarehouseMemory:
    """Truthfully labelled warehouse process and execution-boundary memory."""

    resident_bytes: int | None
    host_total_bytes: int | None
    cgroup_used_bytes: int | None
    cgroup_limit_bytes: int | None
    basis: str
    pressure_fraction: float | None


@dataclass(frozen=True)
class AdapterWarehouseActivity:
    """Current bounded warehouse work counts."""

    active_queries: int | None
    active_merges: int | None
    incomplete_mutations: int | None


@dataclass(frozen=True)
class AdapterWarehouseStuckKafkaConsumer:
    """A Kafka table that keeps failing without committing an offset."""

    table: str
    stuck_seconds: int | None
    recent_errors: int
    latest_error: str


@dataclass(frozen=True)
class AdapterWarehouseKafkaConsumers:
    """Managed Kafka consumer health observed inside the warehouse."""

    configured_tables: int
    materialized_tables: int
    materialized_table_names: tuple[str, ...]
    polling_tables: int
    exception_tables: int
    stuck_consumers: tuple[AdapterWarehouseStuckKafkaConsumer, ...] = ()


@dataclass(frozen=True)
class AdapterWarehouseDeadLetterModel:
    """Quarantined rows of one model over the last 24 hours and its latest row."""

    model_name: str
    row_count: int
    latest_failed_columns: tuple[str, ...]
    latest_payload: str
    latest_quarantined_at: str


@dataclass(frozen=True)
class AdapterWarehouseTable:
    """Active physical footprint for one project-database table."""

    name: str
    rows: int | None
    bytes_on_disk: int | None
    active_parts: int | None


@dataclass(frozen=True)
class AdapterWarehouseHealth:
    """Adapter-neutral point-in-time warehouse diagnostic snapshot."""

    availability: AdapterWarehouseHealthAvailability | str
    status: AdapterWarehouseHealthStatus | str
    version: str | None
    uptime_seconds: int | None
    disks: tuple[AdapterWarehouseDisk, ...]
    inode_total: int | None
    inode_free: int | None
    inode_status: AdapterWarehouseHealthStatus | str
    memory: AdapterWarehouseMemory | None
    activity: AdapterWarehouseActivity | None
    tables: tuple[AdapterWarehouseTable, ...] | None
    collection_duration_ms: int
    kafka_consumers: AdapterWarehouseKafkaConsumers | None = None
    dead_letters: tuple[AdapterWarehouseDeadLetterModel, ...] | None = None
    capacity_warning_fraction: float | None = None
    capacity_critical_fraction: float | None = None
    warnings: tuple[str, ...] = ()
    stale: bool = False
    measured_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "availability", AdapterWarehouseHealthAvailability(self.availability)
        )
        object.__setattr__(self, "status", AdapterWarehouseHealthStatus(self.status))
        object.__setattr__(self, "inode_status", AdapterWarehouseHealthStatus(self.inode_status))


@dataclass(frozen=True)
class AdapterMutationResult:
    """Warehouse-reported evidence for one executed mutation statement."""

    written_rows: int | None = None


@dataclass(frozen=True)
class AdapterStatementProgress:
    """Ephemeral warehouse telemetry for one currently executing statement."""

    elapsed_seconds: float
    read_rows: int
    read_bytes: int
    total_rows_approx: int
    memory_usage_bytes: int
    settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AdapterQueryCancellation:
    """Adapter-confirmed outcome for one exactly identified workflow query."""

    query_id: str
    supported: bool
    query_found: bool
    termination_confirmed: bool
    detail: str | None = None


@dataclass(frozen=True)
class AdapterReplayOffsetRange:
    """One captured partition span used only for approximate replay progress."""

    partition: int
    lower_offset: int
    upper_offset: int


@dataclass(frozen=True)
class AdapterReplayOffsetProgressRequest:
    """Target relation and immutable captured ranges for frontier observation."""

    database: str
    relation: str
    partition_column: str
    offset_column: str
    ranges: tuple[AdapterReplayOffsetRange, ...]


@dataclass(frozen=True)
class AdapterReplayOffsetFrontier:
    """Greatest committed target offset observed for one replay partition."""

    partition: int
    completed_offset: int
