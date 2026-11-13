from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
)


class CliManagedSourceResources(NamedTuple):
    kafka_table: DesiredKafkaTable
    raw_table: DesiredTable
    materialized_view: DesiredMaterializedView


@dataclass(frozen=True)
class CliVirtualBuildIntegrationTestCase:
    description: str
    pipelines_root: Path
    selectors: tuple[str, ...]
    full_refresh: bool
    start_time: str | None
    json_output: bool
    verbose: bool
    auto_approve: bool
    prompt_response: str
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    expected_error_fragments: tuple[str, ...]
    expected_deployment_status_rows: tuple[tuple[str, ...], ...]
    expected_selected_root_names: tuple[str, ...] = ()
    expected_absent_output_fragments: tuple[str, ...] = ()
    deployment_id: str | None = None


@dataclass(frozen=True)
class CliVirtualManualFanInIntegrationTestCase:
    description: str
    deployment_ids: tuple[str, str, str]
    expected_exit_code: int
    expected_delta_rows: tuple[tuple[str, str], ...]
    expected_watermark_rows: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class CliVirtualConfirmationRaceIntegrationTestCase:
    description: str
    deployment_id: str
    expected_exit_code: int
    expected_error_fragment: str
    expected_metadata_row_count: int
    expected_absent_error_fragment: str


@dataclass(frozen=True)
class CliTestCommandIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    sql_test_content: str
    expected_line_total: str
    extra_sql_test_files: tuple[tuple[str, str], ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    expected_node_result_count: int
    expected_invocation_outcome: str


@dataclass(frozen=True)
class CliAuditCommandIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    project_writer_name: str
    order_items_columns: str
    order_items_order_by: str
    order_items_rows: tuple[tuple[str | None, float], ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    expected_node_result_count: int
    expected_node_result_statuses: tuple[str, ...]
    expected_invocation_outcome: str


@dataclass(frozen=True)
class CliAuditBackfillCommandIntegrationTestCase:
    description: str
    expected_exit_code: int
    expected_quality_check_count: int
    expected_assessment: str
    expected_failing_row_count: int


@dataclass(frozen=True)
class CliPlanSnapshotIntegrationTestCase:
    description: str
    source_contents: str
    model_contents: str
    existing_table_ddl_statements: tuple[str, ...]
    expected_replay_lineage_mode: str
    expected_exit_code: int
    expected_output_fragment: str


@dataclass(frozen=True)
class CliBoundedPlanSnapshotIntegrationTestCase:
    description: str
    start_time: str
    expected_execution_mode: str
    expected_warning_count: int
    expected_catalog_load_count: int
    expected_query_count: int
    expected_point_in_time_query_count: int


@dataclass(frozen=True)
class CliTestSemanticsIntegrationTestCase:
    description: str
    sql_test_content: str
    macro_file_contents: str
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectPlanIntegrationTestCase:
    description: str
    expected_execution_scope: tuple[str, ...]
    expected_replay_root_models: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectBuildIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    late_landing_rows: tuple[tuple[str, int, int], ...]
    expected_created_relations: tuple[str, ...]
    expected_fingerprinted_relations: tuple[str, ...]
    expected_replayed_order_ids: tuple[str, ...]
    expected_final_order_ids: tuple[str, ...]
    expected_deployment_row_count: int
    expected_stable_view_count: int
    expected_replay_coverage_ranges: tuple[tuple[str, str, str], ...]
    expected_warehouse_written_rows: tuple[int | None, ...]
    expected_manifest_count: int
    expected_manifest_fingerprint_count: int


@dataclass(frozen=True)
class CliDirectStartTimeIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    expected_source_rows: tuple[tuple[str, int], ...]
    expected_target_rows: tuple[str, ...]
    expected_coverage: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class CliDirectTimezoneStartTimeIntegrationTestCase:
    description: str
    cli_start_time: str
    expected_warehouse_timezone: str
    expected_effective_start_time: str
    expected_sql_fragment: str
    unexpected_local_sql_fragment: str


@dataclass(frozen=True)
class CliDirectLandedAtStartTimeIntegrationTestCase:
    description: str
    expected_target_rows: tuple[str, ...]
    expected_boundary_keys: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectManualWorkflowIntegrationTestCase:
    description: str
    expected_exit_code: int
    expected_order_ids: tuple[str, ...]
    expected_fingerprinted_relations: tuple[str, ...]
    expected_replay_coverage_ranges: tuple[tuple[str, str, str], ...]
    expected_reexecuted_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectLandedAtClockIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    expected_order_ids: tuple[str, ...]
    expected_warehouse_written_rows: tuple[int | None, ...]


@dataclass(frozen=True)
class CliDirectViewBuildIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    expected_rows: tuple[tuple[str, str], ...]
    expected_relation_name: str
    expected_resource_kind: str
    expected_observed_fingerprint_count: int


@dataclass(frozen=True)
class CliDirectOptionalMetadataIntegrationTestCase:
    description: str
    expected_exit_code: int
    expected_order_ids: tuple[str, ...]
    expected_warning_fragment: str
    expected_absent_sql_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectBuildBoundaryIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    pre_capture_statements: tuple[str, ...]
    expected_boundary_keys: tuple[str, ...]
    expected_cutoff_values: tuple[str, ...]
    expected_cutoff_inclusive: tuple[bool, ...]


@dataclass(frozen=True)
class CliDirectBuildRejectionIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    rebuilt_topic: str
    expected_error_fragment: str
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectBuildGuardIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    rebuilt_topic: str
    pre_rebuild_statements: tuple[str, ...]
    expected_exit_code: int
    expected_error_fragment: str
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectSourceRecreationIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    rebuilt_topic: str
    rebuilt_settings: tuple[tuple[str, str], ...]
    expected_drifted_settings: tuple[str, ...]
    expected_kafka_ddl_fragments: tuple[str, ...]
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectChangedSourceRecreationIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    rebuilt_topic: str
    rebuilt_settings: tuple[tuple[str, str], ...]
    expected_execution_scope: tuple[str, ...]
    expected_drifted_settings: tuple[str, ...]
    expected_kafka_ddl_fragments: tuple[str, ...]
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectSourceRejectionIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    rebuilt_settings: tuple[tuple[str, str], ...]
    expected_error_fragment: str
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliCrossModeBuildIntegrationTestCase:
    description: str
    expected_exit_code: int
    expected_error_fragment: str


@dataclass(frozen=True)
class CliLegacyLandingViewIntegrationTestCase:
    description: str
    virtual_environments: bool
    run_build: Callable[[Path, str, AdapterConnection], int]
    landing_rows: tuple[tuple[str, int, int], ...]
    expected_landing_projection: str
    expected_landing_row_count: int


@dataclass(frozen=True)
class CliDirectBuildAuditIntegrationTestCase:
    description: str
    audit_sql_by_name: tuple[tuple[str, str], ...]
    landing_rows: tuple[tuple[str, int, int], ...]
    late_landing_rows: tuple[tuple[str, int, int], ...]
    expected_exit_code: int
    expected_stdout_fragment: str
    expected_final_order_ids: tuple[str, ...]
    expected_audit_observation_rows: tuple[tuple[str, int, str], ...]
    expected_final_coverage: tuple[tuple[str, str, str], ...]
    expected_sample_query_fragment: str
    expected_error_message_count: int
    expected_fingerprint_count_delta: int
    expected_execution_status: str
    expected_artifact_audit_error_count: int


@dataclass(frozen=True)
class CliDirectBuildRerunIntegrationTestCase:
    description: str
    landing_rows: tuple[tuple[str, int, int], ...]
    late_landing_rows: tuple[tuple[str, int, int], ...]
    expected_failed_exit_code: int
    expected_incomplete_target_count: int
    expected_rerun_exit_code: int
    expected_final_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectSelectedBuildIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    landing_rows: tuple[tuple[str, int, int], ...]
    boundary_landing_rows: tuple[tuple[str, int, int], ...]
    expected_order_ids: tuple[str, ...]
    expected_delta_rows: tuple[tuple[str, str], ...]
    expected_drop_statements: tuple[str, ...]
    expected_realized_relation_names: tuple[str, ...]
    expected_replay_targets: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectAggregateBuildIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    landing_rows: tuple[tuple[str, int, int], ...]
    expected_aggregate_rows: tuple[tuple[str, int], ...]
    expected_replay_targets: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectSelectedFailureIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    landing_rows: tuple[tuple[str, int, int], ...]
    expected_order_ids: tuple[str, ...]
    expected_delta_rows: tuple[tuple[str, str], ...]
    expected_failure_fragment: str
    expected_replay_targets: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectExecutionStepFailureIntegrationTestCase:
    description: str
    connection_factory: Callable[[AdapterConnection], AdapterConnection]
    expected_failure_fragment: str
    expected_failed_invocation_count: int
    expected_failed_mode: str
    expected_min_selected_node_count: int


@dataclass(frozen=True)
class CliDirectSelectionMatrixIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    expected_drop_relation_names: tuple[str, ...]
    expected_replay_targets: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectSelectedAuditIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    audit_sql_by_name: tuple[tuple[str, str], ...]
    expected_query_markers: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class CliDirectAdoptedSourceIntegrationTestCase:
    description: str
    source_yml: str
    model_sql: str
    source_columns_sql: str
    initial_values_sql: str
    live_values_sql: str
    source_projection_sql: str
    expected_source_rows: tuple[tuple[str, ...], ...]
    expected_order_ids: tuple[str, ...]
    expected_replay_mode: str
    expected_replay_columns: tuple[str, str, str, str, str]


@dataclass(frozen=True)
class CliDirectAdoptedStartTimeIntegrationTestCase:
    description: str
    source_yml: str
    model_sql: str
    source_columns_sql: str
    values_sql: str
    expected_order_ids: tuple[str, ...]
    expected_coverage: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class CliDirectFutureSourceStartTimeIntegrationTestCase:
    description: str
    source_yml: str
    model_sql: str
    source_columns_sql: str
    insert_sql: str
    start_time_sql: str
    expected_target_count: int


@dataclass(frozen=True)
class CliDirectAdoptedSourceFailureIntegrationTestCase:
    description: str
    source_table_name: str
    source_yml: str
    model_sql: str
    source_columns_sql: str
    expected_error_fragment: str


@dataclass(frozen=True)
class CliVirtualEnvironmentViewIntegrationTestCase:
    description: str
    expected_initial_rows: tuple[tuple[str, str], ...]
    expected_revised_rows: tuple[tuple[str, str], ...]
    expected_final_rows: tuple[tuple[str, str], ...]
    expected_initial_mapping_types: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectRelationRenameIntegrationTestCase:
    description: str
    initial_relation_name: str
    renamed_relation_name: str
    expected_rows: tuple[str, ...]
    expected_fingerprinted_relations: tuple[str, ...]
    expected_dropped_relations: tuple[str, ...]


@dataclass(frozen=True)
class CliVirtualEnvironmentRenameIntegrationTestCase:
    description: str
    initial_table_name: str
    initial_view_name: str
    renamed_table_name: str
    renamed_view_name: str
    expected_rows: tuple[tuple[str, str], ...]
    expected_logical_model_names: tuple[str, ...]


@dataclass(frozen=True)
class CliDirectPostgresRefreshIntegrationTestCase:
    description: str
    refresh: str
    expected_rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class QuarantineLiveIntegrationTestCase:
    description: str
    values_sql: str
    expected_order_ids: tuple[str, ...]
    expected_failed_columns: tuple[tuple[str, ...], ...]
    expected_offsets: tuple[int, ...]


@dataclass(frozen=True)
class QuarantineFailModeIntegrationTestCase:
    description: str
    values_sql: str
    expected_error_fragment: str


@dataclass(frozen=True)
class QuarantineReplayIntegrationTestCase:
    description: str
    history_values_sql: str
    expected_order_ids: tuple[str, ...]
    expected_dead_letter_count: int
    expected_dedupe: bool


@dataclass(frozen=True)
class QuarantineInPlaceIntegrationTestCase:
    description: str
    history_values_sql: str
    bad_row_values_sql: str
    good_row_values_sql: str
    expected_after_enable: tuple[str, ...]
    expected_failed_columns: tuple[tuple[str, ...], ...]
    expected_after_disable: tuple[str, ...]


@dataclass(frozen=True)
class QuarantineLiveConsumerIntegrationTestCase:
    description: str
    history_rows: tuple[tuple[str, int, int], ...]
    late_rows: tuple[tuple[str, int, int], ...]
    expected_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class QuarantineMetadataDatabaseIntegrationTestCase:
    description: str
    metadata_database_suffix: str
    values_sql: str
    expected_failed_columns: tuple[tuple[str, ...], ...]
    expected_health_row_count: int


@dataclass(frozen=True)
class QuarantineCheckAgreementTestCase:
    description: str
    target_type: str
    source_type: str
    value_sql: str
    expected_check_agrees_with_cast: bool


@dataclass(frozen=True)
class QuarantineRepairIntegrationTestCase:
    description: str
    desired_model: str
    live_model: str
    break_statements: tuple[str, ...]
    expected_dead_letter_view_count: int


@dataclass(frozen=True)
class FanOutDedupeIntegrationTestCase:
    description: str
    expected_row_count: int
    expected_dedupe: bool


@dataclass(frozen=True)
class VirtualQuarantineIntegrationTestCase:
    description: str
    history_values_sql: str
    live_values_sql: str
    expected_order_ids: tuple[str, ...]
    expected_failed_columns: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class VirtualDedupeIntegrationTestCase:
    description: str
    history_rows: tuple[tuple[str, int, int], ...]
    expected_order_ids: tuple[str, ...]
    expected_dedupe: bool


@dataclass(frozen=True)
class VirtualPolicySwitchIntegrationTestCase:
    description: str
    bad_values_sql: str
    expected_order_ids: tuple[str, ...]
    expected_failed_columns: tuple[tuple[str, ...], ...]
    expected_deployment_count: int


@dataclass(frozen=True)
class VirtualPolicyRepairIntegrationTestCase:
    description: str
    desired_model: str
    live_model: str
    dropped_view_template: str
    expected_dead_letter_view_count: int


@dataclass(frozen=True)
class PolicySwitchFailureIntegrationTestCase:
    description: str
    live_header: str
    desired_header: str
    broken_view: str
    expected_live_quarantined_after_failure: bool
    expected_live_quarantined_after_rerun: bool
    expected_dead_letter_view_count_after_failure: int


@dataclass(frozen=True)
class MissingLandingRepairIntegrationTestCase:
    description: str
    model_header: str
    expected_landing_view_count: int
    expected_operation: str


@dataclass(frozen=True)
class VirtualSelectedPolicyIntegrationTestCase:
    description: str
    selectors: tuple[str, ...]
    expected_selected_quarantined: bool
    expected_unselected_quarantined: bool


@dataclass(frozen=True)
class InPlaceDeadLetterCleanupIntegrationTestCase:
    description: str
    expected_retired_view_count: int
    expected_active_view_count: int


@dataclass(frozen=True)
class VirtualAdoptedChainIntegrationTestCase:
    description: str
    expected_logical_view_count: int


@dataclass(frozen=True)
class CancelledPausedSourceIntegrationTestCase:
    description: str
    expected_note_fragment: str


@dataclass(frozen=True)
class VirtualDownstreamOnlyChangeIntegrationTestCase:
    description: str
    expected_after_downstream_promotion: tuple[str, ...]
    expected_after_upstream_promotion: tuple[str, ...]
