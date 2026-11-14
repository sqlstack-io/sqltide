from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
)
from sqltide.compiler.compile.models import CompiledModel
from sqltide.dev_server.types import RunPresentationStatus


@dataclass(frozen=True)
class DestructionRequestValidationTestCase:
    description: str
    field: str
    value: object
    expected_error_fragment: str


@dataclass(frozen=True)
class DestructionChallengeWhitespaceTestCase:
    description: str
    responses: list[str]
    expected_responses: list[str]


@dataclass(frozen=True)
class DestructionAuthorizationRouteTestCase:
    description: str
    expected_denied_status: int
    expected_reason: str
    expected_allowed_status: int
    expected_plan_id: str
    expected_planner_call_count: int


@dataclass(frozen=True)
class InactivePipelineRouteTestCase:
    description: str
    expected_pipeline_name: str
    expected_model_count: int
    expected_resource_count: int
    expected_last_published_at: str


@dataclass(frozen=True)
class DestructionResourceConflictRouteTestCase:
    description: str
    conflict_message: str
    expected_status: int
    expected_reason: str


@dataclass(frozen=True)
class DestructionClosureAuthorizationRouteTestCase:
    description: str
    dependent_pipeline: str
    expected_status: int
    expected_permission: str
    expected_authorization_call_count: int
    expected_planner_call_count: int


@dataclass(frozen=True)
class DestructionResetRouteTestCase:
    description: str
    expected_status: int
    expected_managed_sources_included: bool


@dataclass(frozen=True)
class DestructionReviewGateRouteTestCase:
    description: str
    expected_status: int
    expected_detail_fragment: str


@dataclass(frozen=True)
class DestructionAsyncExecutionRouteTestCase:
    description: str
    expected_status: int
    expected_execution_status: str
    maximum_response_seconds: float


@dataclass(frozen=True)
class DestructionRecoveryRouteTestCase:
    description: str
    invocation_id: str
    command: str
    operation_kind: str
    expected_plan_id: str
    expected_pipeline_names: tuple[str, ...]
    expected_included_pipeline_names: tuple[str, ...]


@dataclass(frozen=True)
class DestructionRecoveryRejectionRouteTestCase:
    description: str
    mode: str
    outcome: str
    command: str
    operation_kind: str
    project_identity_kind: str
    target: str
    included_dependant_pipelines: object
    expected_status: int
    expected_error_fragment: str


@dataclass(frozen=True)
class DestructionRecoveryDependencyRouteTestCase:
    description: str
    newly_required_pipelines: tuple[str, ...]
    expected_status: int


@dataclass(frozen=True)
class DestructionActorBindingRouteTestCase:
    description: str
    expected_status: int


@dataclass(frozen=True)
class DestructionRestartRouteTestCase:
    description: str
    expected_plan_id: str
    expected_reviewed_status: int
    expected_reloaded_status: int
    expected_mismatched_review_status: int


@dataclass(frozen=True)
class DevRefactorTestCase:
    description: str
    expected_value: object


@dataclass(frozen=True)
class ReadConnectionRouteTestCase:
    description: str
    path: str
    expected_status: int


@dataclass(frozen=True)
class BuildConflictScopeTestCase:
    description: str
    executed_logical_ids: tuple[str, ...]
    context_logical_ids: tuple[str, ...]
    expected_status: int
    expected_started: bool
    expected_detail_fragment: str


@dataclass(frozen=True)
class CompileOutcomeTestCase:
    description: str
    break_compile: bool
    expected_state: str
    expected_has_analysis: bool


@dataclass(frozen=True)
class StatusEndpointTestCase:
    description: str
    break_compile: bool
    expected_state: str
    expected_warehouse_connected: bool


@dataclass(frozen=True)
class SnapshotRefreshPerformanceTestCase:
    description: str
    expected_status: int
    expected_probe_calls: int


@dataclass(frozen=True)
class BootstrapEndpointTestCase:
    description: str
    expected_auth_mode: str
    expected_compile_state: str
    expected_has_definitions: bool
    expected_has_state: bool


@dataclass(frozen=True)
class BootstrapAuthorizationTestCase:
    description: str
    expected_status: int


@dataclass(frozen=True)
class StateRouteErrorTestCase:
    description: str
    expected_status: int
    expected_detail: str


@dataclass(frozen=True)
class ReloadAuthorizationTestCase:
    description: str
    expected_denied_status: int
    expected_allowed_status: int
    expected_denied_reason: str


@dataclass(frozen=True)
class OperationAuthorizationRouteTestCase:
    description: str
    path: str
    body: dict[str, object]
    expected_allowed_status: int


@dataclass(frozen=True)
class DeploymentRollbackAuthorizationTestCase:
    description: str
    username: str
    expected_status: int
    expected_denial_message: str | None
    expected_denied_permission: str | None
    expected_rollback_calls: int


@dataclass(frozen=True)
class CapabilitiesTestCase:
    description: str
    expected_project: str
    expected_target: str
    expected_quality_pipelines: tuple[str, ...]


@dataclass(frozen=True)
class DevAppLifespanTestCase:
    description: str
    expected_status: int


@dataclass(frozen=True)
class DefinitionsFieldTestCase:
    description: str
    expected_model_name: str
    expected_model_description: str
    expected_column_description: str
    expected_anchor: str
    expected_audit_name: str
    expected_audit_file_suffix: str
    expected_audit_generic_name: str
    expected_driving_input: str
    expected_source_kind: str
    expected_managed_ddl_fragment: str
    expected_model_reference_scope: str
    expected_allowed_cross_pipeline_references: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class DependencyPolicyPayloadTestCase:
    description: str
    project_config_suffix: str
    expected_model_reference_scope: str
    expected_allowed_cross_pipeline_references: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class FailingAnalysisTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class StateFieldTestCase:
    description: str
    fingerprint_status: str
    definition_hash_builder: Callable[[CompiledModel], str]
    identity_metadata_builder: Callable[[dict[str, object]], str]
    expected_source_freshness: str
    expected_model_freshness: str
    expected_model_lag_seconds: float
    expected_drift_reasons: tuple[str, ...]
    expected_source_rows_per_second: float
    expected_partition_max_offset: int
    expected_bucket_count: int


@dataclass(frozen=True)
class UnconfiguredFreshnessTestCase:
    description: str
    expected_source_freshness: str | None
    expected_model_freshness: str | None


@dataclass(frozen=True)
class ViewFreshnessTestCase:
    description: str
    expected_freshness: str | None
    expected_lag_seconds: float | None
    expected_newest_row_at: str | None


@dataclass(frozen=True)
class WarehouseHealthPayloadTestCase:
    description: str
    expected_status: str
    expected_disk_status: str
    expected_memory_basis: str
    expected_table_name: str
    expected_polling_tables: int
    expected_capacity_warning_fraction: float


@dataclass(frozen=True)
class ModelDriftUnavailablePayloadTestCase:
    description: str
    expected_message: str


@dataclass(frozen=True)
class ModelDriftDifferencePayloadTestCase:
    description: str
    expected_query_fragment: str
    expected_physical_field: str


@dataclass(frozen=True)
class ActivityPayloadTestCase:
    description: str
    capabilities: tuple[str, ...]
    view_rows: tuple[tuple[object, ...], ...]
    part_log_rows: tuple[tuple[object, ...], ...]
    parts_rows: tuple[tuple[object, ...], ...]
    expected_state: str
    expected_source: str
    expected_approximate: bool
    expected_rows_written: int
    expected_last_triggered_at: str | None
    physical_relation_name: str = "tbl__orders"


@dataclass(frozen=True)
class PlanEndpointTestCase:
    description: str
    selectors: tuple[str, ...]
    expected_status: int
    expected_entry_names: tuple[str, ...]
    expected_command: str
    expected_replay_root_rows: tuple[int | None, ...]
    expected_sql_changes: tuple[str, ...]


@dataclass(frozen=True)
class ModeAwarePlanTestCase:
    description: str
    mode: str
    expected_execution_order: tuple[str, ...]
    preparation_builder: Callable[[str], VirtualWorkflowPreparation | MixedWorkflowPreparation]


@dataclass(frozen=True)
class PlanSourceRecreationTestCase:
    description: str
    preparation_builder: Callable[[], DirectWorkflowPreparation]
    expected_warning_code: str
    expected_message_fragments: tuple[str, ...]
    expected_source_recreation: dict[str, object]
    expected_actions: tuple[tuple[str, str, str, str], ...]
    expected_entry_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ChecksStatusTestCase:
    description: str
    expected_name: str
    expected_status: str
    expected_failure_count: int
    expected_completed_at: str


@dataclass(frozen=True)
class RunEventsFeedTestCase:
    description: str
    invocation_id: str
    expected_event_kinds: tuple[str, ...]
    expected_written_rows: int
    expected_executed_logical_ids: tuple[str, ...] = ()
    expected_context_logical_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecksRunTestCase:
    description: str
    kind: str
    name: str
    expected_status: int
    expected_passed: bool


@dataclass(frozen=True)
class AuditBatchRouteTestCase:
    description: str
    names: tuple[str, ...]
    expected_name: str


@dataclass(frozen=True)
class StaticAssetsPresenceTestCase:
    description: str
    expected_present: bool


@dataclass(frozen=True)
class SpaFallbackTestCase:
    description: str
    request_path: str
    expected_body_fragment: str
    expected_cache_control: str


@dataclass(frozen=True)
class ReplayCountQueryTestCase:
    description: str
    start_time: str | None
    expected_query: str


@dataclass(frozen=True)
class ReplayTimeColumnTestCase:
    description: str
    boundary_mode: str
    expected_column: str


@dataclass(frozen=True)
class RunStatusDerivationTestCase:
    description: str
    terminal_outcome: str | None
    completed_event_outcome: str | None
    signal_age_seconds: int
    expected_status: RunPresentationStatus
    presumed_failed_after_seconds: int = 600


@dataclass(frozen=True)
class RunDurationDerivationTestCase:
    description: str
    started_at: str
    completed_at: str | None
    warehouse_now: datetime
    expected_duration_ms: int


@dataclass(frozen=True)
class RunDetailHistoryTestCase:
    description: str
    invocation_id: str
    expected_status: str
    expected_found: bool


@dataclass(frozen=True)
class MissingRunDetailTestCase:
    description: str
    invocation_id: str
    expected_status: None
    expected_found: bool


@dataclass(frozen=True)
class RunStatementReadTestCase:
    description: str
    invocation_id: str
    statement_sequence: int
    row: tuple[object, ...]
    expected_sql: str
    expected_step_id: str


@dataclass(frozen=True)
class RunHistoryQueryTestCase:
    description: str
    expected_invocation_ids: frozenset[str]
    expected_terminal_calls: tuple[tuple[int | None, bool | None], ...]
    expected_exclude_terminal_invocations: bool


@dataclass(frozen=True)
class TerminalRunQueryTestCase:
    description: str
    invocation_id: str
    expected_command: str


@dataclass(frozen=True)
class MessageQuerySqlTestCase:
    description: str
    request_json: dict
    window_seconds: int | None
    expected_sql: str


@dataclass(frozen=True)
class MessageQueryValidationTestCase:
    description: str
    request_json: dict
    expected_error_fragment: str


@dataclass(frozen=True)
class MessageRecordSqlTestCase:
    description: str
    partition: int
    offset: int
    expected_sql: str


@dataclass(frozen=True)
class MessageFacetsSqlTestCase:
    description: str
    request_json: dict
    facet_path: tuple[str | int, ...]
    expected_top_values_sql: str
    expected_totals_sql: str


@dataclass(frozen=True)
class MessageRouteTestCase:
    description: str
    path: str
    body: dict
    expected_status_code: int
    expected_fragment: str


@dataclass(frozen=True)
class MessageListRouteTestCase:
    description: str
    limit: int
    expected_keys: tuple[str, ...]
    expected_first_headers: tuple[tuple[str, str], ...]
    expected_window_seconds: int
    expected_next_cursor: dict


@dataclass(frozen=True)
class MessageWideningRouteTestCase:
    description: str
    limit: int
    expected_row_count: int


@dataclass(frozen=True)
class MessageRecordRouteTestCase:
    description: str
    partition: int
    offset: int
    expected_value: str
    expected_topic: str
    expected_headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MessageRecordMissingTestCase:
    description: str
    partition: int
    offset: int
    expected_fragment: str


@dataclass(frozen=True)
class MessageFacetsRouteTestCase:
    description: str
    expected_values: tuple[tuple[str, int], ...]
    expected_null_count: int
    expected_other_count: int
    expected_total_count: int


@dataclass(frozen=True)
class TopicsMergedPayloadTestCase:
    description: str
    expected_topic_names: frozenset[str]
    expected_managed_sources: tuple[dict, ...]
    expected_lag_messages: int
    expected_retained_rows: int
    expected_retained_bytes: int


@dataclass(frozen=True)
class TopicsColdCachePayloadTestCase:
    description: str
    expected_pending_brokers: tuple[str, ...]
    expected_topic_names: tuple[str, ...]


@dataclass(frozen=True)
class TopicsUnavailableTestCase:
    description: str
    expected_reason_fragment: str


@dataclass(frozen=True)
class TopicsRouteTestCase:
    description: str
    expected_topic_name: str


@dataclass(frozen=True)
class DeploymentsPayloadTestCase:
    description: str
    expected_deployment_ids: tuple[str, ...]
    expected_states: tuple[str, ...]
    expected_model_counts: tuple[int, ...]
    expected_rows: tuple[int, ...]


@dataclass(frozen=True)
class DeploymentDetailTestCase:
    description: str
    deployment_id: str
    expected_state: str
    expected_logical_names: tuple[str, ...]
    expected_staged_rows: tuple[int, ...]
    expected_live_rows: tuple[int | None, ...]
    expected_new_flags: tuple[bool, ...]
    expected_orphan_relations: int
    expected_preview_classification: str | None
    expected_additions: tuple[str, ...]
    expected_replacements: tuple[str, ...]
    expected_removals: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentDetailMissingTestCase:
    description: str
    deployment_id: str
    expected_payload: dict[str, object] | None


@dataclass(frozen=True)
class DeploymentInitialPublishSafetyTestCase:
    description: str
    expected_candidate_new_flags: tuple[bool, ...]
    expected_classification: str
    expected_removed_logical_names: tuple[str, ...]
    expected_orphan_relation_names: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentPartialPromotionTestCase:
    description: str
    expected_state: str
    expected_additions: tuple[str, ...]
    expected_replacements: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentStaleUpstreamTestCase:
    description: str
    upstream_deployment_id: str
    expected_blocked_reason: str | None


@dataclass(frozen=True)
class BoundRelationStatsTestCase:
    description: str
    stats: tuple[tuple[str, int, int, int], ...]
    bindings: tuple[tuple[str, str], ...]
    expected_rows_by_relation: tuple[tuple[str, int], ...]
    expected_parts_by_relation: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DeploymentDiffPayloadTestCase:
    description: str
    expected_statuses: tuple[str, ...]
    expected_row_pairs: tuple[tuple[int | None, int | None], ...]
    expected_added_columns: tuple[tuple[str, ...], ...]
    expected_removed_columns: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class SensorRoutesTestCase:
    description: str
    expected_sensor_names: tuple[str, ...]
    expected_effective_statuses: tuple[str, ...]
    expected_event_types: tuple[str | None, ...]


@dataclass(frozen=True)
class SensorRouteErrorTestCase:
    description: str
    method: str
    path: str
    body: dict[str, object] | None
    expected_status_code: int
    expected_detail_fragment: str


@dataclass(frozen=True)
class StateSnapshotTestCase:
    description: str
    request_count: int
    expected_build_count: int


@dataclass(frozen=True)
class SourceObservationBatchTestCase:
    description: str
    relation_count: int
    expected_statement_count: int
    expected_union_count: int


@dataclass(frozen=True)
class ConnectionSettingsPayloadTestCase:
    description: str
    expected_settings: dict[str, str]


@dataclass(frozen=True)
class WarehouseRefreshSnapshotTestCase:
    description: str
    refresh_count: int
    expected_build_count: int


@dataclass(frozen=True)
class OverlayReaderTestCase:
    description: str
    read_count: int
    expected_connection_count: int
