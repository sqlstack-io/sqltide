from dataclasses import dataclass


@dataclass(frozen=True)
class DevServerBrowserE2ETestCase:
    description: str
    expected_connected: bool
    expected_compile_state: str


@dataclass(frozen=True)
class WarehouseHealthBrowserE2ETestCase:
    description: str
    expected_adapter: str
    expected_capacity_heading: str
    expected_table_heading: str
    expected_freshness_state: str
    expected_test_state: str
    expected_header_font_size: str
    expected_header_font_weight: str
    expected_body_font_size: str
    expected_body_font_weight: str


@dataclass(frozen=True)
class KafkaNotBuiltBrowserE2ETestCase:
    description: str
    expected_not_built_text: str
    expected_live_polling_text: str
    excluded_error_text: str


@dataclass(frozen=True)
class SchedulerProcessE2ETestCase:
    description: str
    expected_scheduler_state: str
    expected_result_status: str
    expected_run_mode: str


@dataclass(frozen=True)
class MessageBrowserProcessE2ETestCase:
    description: str
    produced_messages: tuple[tuple[str, str, tuple[tuple[str, bytes], ...]], ...]
    filtered_order_id: str
    expected_filtered_key: str
    expected_filtered_headers: tuple[tuple[str, str], ...]
    expected_facet_values: tuple[str, ...]


@dataclass(frozen=True)
class KafkaReaderReuseE2ETestCase:
    description: str
    consumer_group: str
    expected_committed_offset: int
    expected_end_offset: int


@dataclass(frozen=True)
class LineageExactActivityE2ETestCase:
    description: str
    expected_title: str
    expected_logical_counts: str
    expected_physical_counts: str
    expected_multi_command: str
    expected_model_only_command: str
    expected_moving_state: str
    expected_idle_state: str
    expected_stalled_state: str
    expected_source: str
    expected_collapsed_pipelines: int


@dataclass(frozen=True)
class LineageApproximateActivityE2ETestCase:
    description: str
    expected_counts: str
    expected_moving_state: str
    expected_unknown_state: str
    expected_moving_source: str
    expected_unknown_source: str


@dataclass(frozen=True)
class RunCommandBoxE2ETestCase:
    description: str
    protected_model: str
    expected_confirmation: str
    expected_quoted_selector: str


@dataclass(frozen=True)
class PlanBrowserE2ETestCase:
    description: str
    selector: str
    expected_command_suffix: str
    expected_replay_rows: int


@dataclass(frozen=True)
class DestructionBrowserE2ETestCase:
    description: str
    pipeline_name: str
    expected_model_name: str
    expected_relation_name: str
    expected_invocation_id: str


@dataclass(frozen=True)
class BuildRunBrowserE2ETestCase:
    description: str
    selector: str
    expected_start_status: str
    expected_outcome: str
    expected_model_node_id: str


@dataclass(frozen=True)
class ReplayProgressBrowserE2ETestCase:
    description: str
    statement_progress_fields: dict[str, object]
    expected_progress_text: str
    expected_eta_count: int


@dataclass(frozen=True)
class SourceTopicBrowserE2ETestCase:
    description: str
    source_name: str
    expected_kind: str
    expected_retained_rows: int


@dataclass(frozen=True)
class MessageConsoleBrowserE2ETestCase:
    description: str
    source_name: str
    filtered_order_id: str
    expected_header_name: str
    expected_header_value: str


@dataclass(frozen=True)
class CatalogPipelineBrowserE2ETestCase:
    description: str
    pipeline_name: str
    source_name: str
    parent_model: str
    child_model: str
    expected_relation: str
    expected_child_relation: str


@dataclass(frozen=True)
class SecondaryStatePerformanceE2ETestCase:
    description: str
    pipeline_name: str
    expected_tree_nodes: int


@dataclass(frozen=True)
class DeploymentBrowserE2ETestCase:
    description: str
    expected_changed_model: str
    expected_active_value: str
    expected_staged_value: str
    missing_deployment_id: str


@dataclass(frozen=True)
class SensorsBrowserE2ETestCase:
    description: str
    expected_running_sensor: str
    expected_paused_sensor: str
    expected_dead_letter_fragment: str
    expected_tick_statuses: tuple[str, ...]


@dataclass(frozen=True)
class QualityBrowserE2ETestCase:
    description: str
    expected_passing: int
    expected_warning: int
    expected_failing: int
    expected_sample_key: str


@dataclass(frozen=True)
class CompleteStreamingBrowserE2ETestCase:
    description: str
    message_key: str
    message_value: str
    expected_order_id: str
    expected_final_order_id: str
    expected_activity_source: str


@dataclass(frozen=True)
class UnsafeReplayBrowserE2ETestCase:
    description: str
    unsafe_selector: str
    safe_selector: str
    expected_missing_column: str
    expected_preserved_row: str


@dataclass(frozen=True)
class AuthorizationBrowserE2ETestCase:
    description: str
    expected_denied_status: int
    expected_denied_reason: str
    expected_stale_reason: str
    expected_allowed_state: str
    expected_assigned_role: str


@dataclass(frozen=True)
class PasswordAuthenticationBrowserE2ETestCase:
    description: str
    username: str
    password: str
    expected_session_ttl_seconds: int


@dataclass(frozen=True)
class TrustedProxyBrowserE2ETestCase:
    description: str
    spoofed_username: str
    expected_username: str
    expected_denied_reason: str


@dataclass(frozen=True)
class PlanSourceRecreationBrowserE2ETestCase:
    description: str
    source_recreations: tuple[dict[str, object], ...]
    expected_section_count: int
    expected_section_fragments: tuple[str, ...]
    expected_summary_fragment: str
    expected_warning_count: int


@dataclass(frozen=True)
class DevServerConcurrencyE2ETestCase:
    description: str
    slow_seconds: float
    fast_requests: int
    expected_result: tuple[int, bool, int, bool, frozenset[int], bool, bool]
    expected_fast_budget_seconds: float
    expected_minimum_busy_ratio: float
