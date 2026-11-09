import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterManifestSnapshot,
    AdapterReplayRequest,
    CatalogSnapshot,
)
from sqltide.adapter.types import AdapterResourceRenderer
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    CompiledProject,
    CompileProjectInputs,
    CompilerAdapterProfile,
    DesiredState,
    ExternalSourceReplayConfig,
    LogicalResourceKey,
    ObjectKey,
)
from sqltide.compiler.discovery.models import (
    AuditDefaults,
    DiscoveredProjectFile,
    EffectiveProjectConfiguration,
    ExecutionSettings,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaRetentionPolicy,
    LoadedPipeline,
    LoadedProject,
    LoadedProjectConfiguration,
    ModelRetentionPolicy,
    PipelineNaming,
    PipelineProtection,
    PostgresRefreshSourceStep,
    ReplayOnChangePolicy,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import BoundedReplayFallback
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import (
    ActualState,
    DeploymentPlan,
    DirectPlan,
    DirectRowErrorPolicyChange,
    DirectSourceDriftSelection,
    DirectWarehouseSnapshot,
    PlannedObjectChange,
    PlanningWarehouseSnapshot,
    RebuildSubtree,
)
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import SqlReferenceRewriter
from sqltide.compiler.sql_analysis.models import (
    SqlAggregateFacts,
    SqlModelAnalysis,
    SqlOutputColumn,
    SqlStorageExpression,
)
from sqltide.diagnostics.models import CompilerDiagnostic
from sqltide.executor.auditing.models import AuditWarmupState
from sqltide.executor.destruction.models import DestructionPlan
from sqltide.executor.population.models import PopulationSourceRecreation

type _Span = tuple[int, int, int, int, int, int]

class SqlScanError(ValueError):
    args: tuple[str, str, _Span | None]

def parse_model_header(header: str) -> dict[str, object]: ...
def load_toml(text: str) -> dict[str, Any]: ...
def declares_virtual_pipeline(pipelines_root: Path) -> bool: ...
def compile_access_policy(
    contents: str, source_path: Path, pipeline_names: frozenset[str]
) -> CompiledAccessPolicy: ...

ACCESS_PIPELINE_PERMISSIONS: list[str]
ACCESS_PROJECT_PERMISSIONS: list[str]
ACCESS_TARGET_PERMISSIONS: list[str]

def render_clickhouse_resource(resource: object, database: str, if_not_exists: bool) -> str: ...
def render_clickhouse_set_difference_comparison(targets: tuple[object, ...]) -> str: ...
def render_clickhouse_ensure_database(database: str) -> str: ...
def render_clickhouse_replay_coverage_query(request: object) -> str: ...
def render_clickhouse_replay_from_capture(request: object) -> str: ...
def render_clickhouse_replay_from_deployment(request: object) -> list[str]: ...
def database_scoped_consumer_group(consumer_group: str, database: str) -> str: ...
def render_clickhouse_metadata_migration_statements(database: str) -> list[str]: ...
def render_clickhouse_metadata_migration_workflow(database: str) -> list[str]: ...
def render_clickhouse_metadata_state(database: str, state: object) -> list[str]: ...
def render_clickhouse_latest_fingerprints_query(
    database: str, logical_model_identities: list[str]
) -> str: ...
def render_clickhouse_object_state_records_query(database: str) -> str: ...
def render_clickhouse_run_statement_verification_query(
    database: str, invocation_id: str
) -> str: ...
def render_clickhouse_audit_count_query(query: str) -> str: ...
def render_clickhouse_audit_sample_query(query: str, limit: int) -> str: ...
def redact_clickhouse_workflow_statements(
    sqls: list[str], workflow_sha256: str
) -> tuple[list[str], str]: ...
def render_clickhouse_direct_fingerprint_observations(
    database: str, fingerprints: tuple[object, ...]
) -> list[str]: ...
def render_clickhouse_manifest_history_query(
    database: str, project_identity: str, target_name: str, target_database: str
) -> str: ...
def render_clickhouse_manifest_publication(database: str, manifest: object) -> str: ...
def render_clickhouse_sensor_state_inserts(
    database: str, state: object, include_migration: bool
) -> list[str]: ...
def render_clickhouse_sensor_retention_cleanup(database: str, retention_days: int) -> list[str]: ...
def render_clickhouse_run_event_inserts(
    database: str, events: tuple[object, ...], include_migration: bool
) -> list[str]: ...
def render_clickhouse_run_statement_inserts(
    database: str, statements: tuple[object, ...], include_migration: bool
) -> list[str]: ...
def render_clickhouse_latest_node_status_query(
    database: str, project_identity: str, target_identity: str, nodes: tuple[object, ...]
) -> str: ...
def skip_trivia(sql: str, start: int) -> int: ...
def find_matching_parenthesis(sql: str, open_index: int, context: str) -> int: ...
def find_next_unquoted_character(sql: str, character: str, start: int) -> int | None: ...
def try_consume_keyword(sql: str, start: int, keyword: str) -> int | None: ...
def normalized_statement_sql(sql: str) -> str: ...
def extract_references(sql: str) -> list[tuple[str, str, str | None, _Span]]: ...
def split_header_blocks(sql: str, keyword: str) -> list[tuple[int, int, str, str]]: ...
def extract_top_level_ctes(
    sql: str, context: str
) -> tuple[list[tuple[str, str, _Span]], str, _Span]: ...
def outer_projection_spans(sql: str) -> list[_Span]: ...
def outer_star_span(sql: str, start: int, end: int) -> _Span | None: ...
def outer_double_colon_type(sql: str, start: int, end: int) -> str | None: ...
def expand_macro_calls(
    sql: str,
    macro_names: list[str],
    file_path: str,
    call: Callable[..., object],
    fail: Callable[..., BaseException],
    locate: Callable[..., BaseException],
    declarations: Declarations | None,
    capture: Callable[[tuple[str, list[tuple[int, int, int, int]]]], None] | None = None,
) -> str: ...

class RulesConfig:
    @property
    def select(self) -> list[str] | None: ...
    def selected_rules(self) -> list[str]: ...
    def ignore_selectors(self) -> list[list[str]]: ...
    def with_selector_paths(self, resolved: list[list[str]]) -> RulesConfig: ...

class RuleFinding:
    @property
    def diagnostic(self) -> CompilerDiagnostic: ...
    @property
    def path(self) -> Path: ...
    @property
    def start(self) -> int: ...
    @property
    def end(self) -> int: ...
    @property
    def replacement(self) -> str | None: ...
    @property
    def fix_start(self) -> int | None: ...
    @property
    def fix_end(self) -> int | None: ...

def rules_config(
    *, loaded_project: LoadedProject | None, selection: tuple[str, ...] | None = None
) -> RulesConfig: ...
def evaluate_rules(
    *,
    inputs: CompileProjectInputs,
    config: RulesConfig,
    dialect: str,
    project_dir: Path,
    only_path: Path | None = None,
    check_stale: bool = True,
) -> tuple[RuleFinding, ...]: ...
def formatted_file(*, source: DiscoveredProjectFile, keyword: str, dialect: str) -> str: ...

class Declarations:
    def render_constant(self, name: str, file_path: str) -> str: ...
    def render_enum_member(self, name: str, member: str, file_path: str) -> str: ...

def load_declarations(
    constant_files: list[tuple[str, str]], enum_files: list[tuple[str, str]]
) -> Declarations: ...
def find_macro_call_names(sql: str) -> tuple[list[str], str | None]: ...
def find_next_macro_start(sql: str, start: int) -> int | None: ...
def rewrite_template_relations(
    template: str, rewrites: list[tuple[str, str, bool]], database_placeholder: str
) -> str: ...
def append_template_predicate(template: str, predicate: str) -> str: ...
def merge_template_ctes(template: str, named_queries: list[tuple[str, str]]) -> str: ...

class PolyglotError(ValueError): ...

def sql_normalize_data_type(sql: str, dialect_name: str) -> str: ...
def polyglot_analyze_query(sql: str, dialect_name: str) -> dict[str, Any]: ...

class SqlTreeError(ValueError):
    args: tuple[Any, ...]

class SqlModelTree:
    @property
    def function_names(self) -> list[str]: ...
    @property
    def has_group_by(self) -> bool: ...
    @property
    def projections(self) -> list[tuple[int, str, str, _Span]]: ...
    def uses_prewhere(self) -> bool: ...

class SqlTreeAnalyzer:
    def __init__(self, dialect: str) -> None: ...
    def analyze_model(self, sql: str) -> SqlModelTree: ...
    def resolve_model(
        self,
        model: SqlModelTree,
        authored_sql: str,
        references: list[tuple[str, str, str | None, int, int]],
        resolver: Mapping[str, str],
        database_placeholder: str,
    ) -> tuple[str, str]: ...
    def rewrite_relations(
        self,
        sql: str,
        references: list[tuple[str, str, str | None, int, int]],
        resolver: Mapping[str, str],
    ) -> dict[str, str]: ...
    def canonicalize_query(self, sql: str, database_placeholder: str) -> tuple[str, str]: ...
    def storage_column_names(self, expression: str) -> list[str]: ...

def sql_rewrite_query(
    sql: str,
    dialect_name: str,
    rewrites: list[tuple[str, str, list[str | None] | None, bool]],
    predicate: str | None,
    ctes: list[tuple[str, str]],
) -> tuple[str, list[str], bool]: ...
def sql_quarantine_ineligibility(sql: str, dialect_name: str) -> tuple[str, str] | None: ...
def sql_quarantine_split(sql: str, model_name: str, dialect_name: str) -> tuple[str, str]: ...
def sql_select_is_row_level(sql: str, dialect_name: str) -> bool: ...
def sql_select_aggregate_facts(sql: str, dialect_name: str) -> tuple[list[str], bool]: ...
def sql_analyze_catalog(
    sql: str, dialect_name: str
) -> tuple[
    str,
    str | None,
    list[tuple[str | None, str]],
    tuple[str | None, str] | None,
    tuple[str | None, str] | None,
    str | None,
    str | None,
    list[tuple[str, str]],
]: ...
def sql_expression_list(sql: str, dialect_name: str) -> list[str]: ...
def sql_validate_query(sql: str, dialect_name: str) -> None: ...
def sql_canonicalize(sql: str, dialect_name: str) -> str: ...
def sql_render_string_literal(value: str, dialect_name: str) -> str: ...
def json_dumps(value: object, indent: int | None = None, sort_keys: bool = False) -> str: ...
def stage_artifact_files(
    staging_root: os.PathLike[str] | str, files: list[tuple[str, str]]
) -> None: ...
def artifact_model_query_path(pipeline: str, model: str) -> str: ...
def artifact_model_resource_path(pipeline: str, model: str, resource_kind: str) -> str: ...
def artifact_source_resource_path(source: str, resource: str) -> str: ...
def artifact_audit_path(relative_file: str, name: str | None = None) -> str: ...
def artifact_test_path(name: str, target_names: list[str]) -> str: ...
def build_project_graph(
    sources: list[str],
    models: list[tuple[str, str, str, str | None, list[tuple[str, str | None]]]],
    pipeline_modes: list[tuple[str, str]],
    pipeline_reference_scope: bool,
    allowed_cross_pipeline_references: list[tuple[str, str]],
) -> tuple[list[int], list[list[tuple[int, str]]], list[list[tuple[int, int]]], list[int]]: ...
def destruction_available_pipelines(
    current: list[str], snapshot: AdapterManifestSnapshot | None
) -> list[str]: ...
def resolve_destruction_selection(
    project_argument: tuple[
        list[tuple[str, bool, str | None, list[tuple[str, str]]]],
        list[list[int]],
        list[list[int]],
        list[tuple[str, int | None]],
        list[int],
        list[int],
    ],
    request_argument: tuple[str, str, list[str], list[str], bool],
    available: list[str],
) -> tuple[list[str], list[str], list[str]]: ...
def plan_destruction_scope(
    project_argument: tuple[
        list[tuple[str, bool, str | None, list[tuple[str, str]]]],
        list[list[int]],
        list[list[int]],
        list[tuple[str, int | None]],
        list[int],
        list[int],
    ],
    request_argument: tuple[str, str, list[str], list[str], bool],
    affected: list[str],
    current: list[str],
    snapshot: AdapterManifestSnapshot | None,
    catalog_snapshot: CatalogSnapshot,
    deployment_inventory: AdapterDeploymentInventory,
) -> tuple[list[str], list[str], list[tuple[str, str, list[str], list[str], list[str]]]]: ...
def build_destruction_evidence(
    database: str,
    owned: list[tuple[str, str, list[str], list[str], list[str]]],
    catalog_snapshot: CatalogSnapshot,
    stats: dict[str, tuple[int, int]],
    limit: int | None,
) -> list[
    tuple[
        str,
        str,
        str,
        bool,
        int | None,
        int | None,
        str | None,
        list[str],
        list[str],
        list[str],
        list[str],
    ]
]: ...
def build_destruction_challenges(
    pipeline_names: list[str], production_reset: bool
) -> list[str]: ...
def destruction_plan_fingerprint(
    *,
    operation: str,
    target: str,
    database: str,
    metadata_database: str,
    requested_pipeline_names: list[str],
    included_dependent_pipeline_names: list[str],
    affected_pipeline_names: list[str],
    affected_model_names: list[str],
    affected_source_names: list[str],
    relations: list[
        tuple[
            str,
            str,
            str,
            bool,
            int | None,
            int | None,
            str | None,
            list[str],
            list[str],
            list[str],
            list[str],
        ]
    ],
    challenges: list[str],
    manifest_fingerprint: str,
    relation_drop_size_limit: int | None,
    relation_drop_size_server_limit: int | None,
    relation_drop_size_override: int | None,
    relation_drop_size_policy_observed: bool,
    include_orphans: bool,
) -> str: ...
def normalize_cli_start_time(raw_value: str) -> str: ...
def validate_declared_external_sources(
    *,
    catalog: CatalogSnapshot,
    external_source_replay_configs: tuple[ExternalSourceReplayConfig, ...],
    database: str,
) -> None: ...
def resolve_audit_warmup_states(
    *,
    audits: tuple[LoadedSqlAudit, ...],
    anchors_by_model: dict[str, str],
    materialized_model_names: frozenset[str],
    warehouse_now: str,
) -> dict[str, AuditWarmupState]: ...
def audit_schedule_entry(
    *,
    audit: LoadedSqlAudit,
    warmup: AuditWarmupState,
    status: dict[str, object] | None,
    warehouse_now: str,
    anchors: dict[str, str],
) -> dict[str, object]: ...
def serialize_destruction_plan(plan: DestructionPlan) -> str: ...
def deserialize_destruction_plan(payload_json: str) -> DestructionPlan: ...
def list_destruction_inactive_pipelines(
    current: list[str], snapshot_object: AdapterManifestSnapshot
) -> list[tuple[str, int, int, str]]: ...
def plan_population_replays(
    *,
    roots: list[tuple[int, int, str, str, str | None, int | None]],
    tables: list[tuple[int, str, str | None, list[str], list[tuple[str, str]]]],
    views: list[tuple[str, str]],
    key_names: list[str],
    physical_names: list[tuple[int, str]],
    external_columns: list[
        tuple[int, tuple[str | None, str | None, str | None, str | None, str | None]]
    ],
    replay_anchor_keys: set[int],
    default_database: str,
    boundary_time: str,
    watermarks: list[tuple[int, str, str]] | None,
) -> tuple[
    list[tuple[str, str]],
    list[
        tuple[
            int,
            str,
            str,
            tuple[str, str, str, str],
            int,
            list[tuple[str, str, str | None]],
            list[str],
            tuple[str, str, str | None, int | None],
            bool,
            list[str],
            list[tuple[str, str]],
            bool,
        ]
    ],
]: ...
def bounded_replay_outcome(
    mode: str | None,
    forced_start_time: bool,
    live_columns: set[str] | None,
    fallback_without_history: bool,
) -> str: ...
def render_plan_text(
    *,
    plan: DeploymentPlan,
    desired_state: DesiredState,
    database: str,
    verbose: bool,
    use_color: bool,
) -> str: ...
def render_plan_json(*, plan: DeploymentPlan, adapter_name: str) -> str: ...
def render_direct_plan_text(
    *,
    plan: DirectPlan,
    adapter_name: str,
    use_color: bool,
    verbose: bool = False,
    source_recreations: tuple[PopulationSourceRecreation, ...] | None = None,
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] | None = None,
) -> str: ...
def render_direct_plan_json(
    *,
    plan: DirectPlan,
    adapter_name: str,
    source_recreations: tuple[PopulationSourceRecreation, ...] | None = None,
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] | None = None,
) -> str: ...
def render_source_recreation_notice(recreation: PopulationSourceRecreation) -> str: ...
def render_row_error_policy_line(change: DirectRowErrorPolicyChange) -> str: ...
def plan_deployment(
    *,
    desired_state: DesiredState,
    actual_state: ActualState,
    default_database: str,
    render_resource: AdapterResourceRenderer,
    deployment_id: str | None,
    full_refresh_keys: frozenset[ObjectKey],
    start_time_keys: frozenset[ObjectKey],
    start_time: str | None,
) -> DeploymentPlan: ...
def plan_direct_build(
    *,
    graph: ProjectGraph,
    realized_project: RealizedProject,
    snapshot: DirectWarehouseSnapshot,
    database: str,
    selected_model_keys: frozenset[LogicalResourceKey],
    selection_mode: DirectSelectionMode | None,
    include_missing_upstream: bool,
    effective_start_time: str | None,
    source_drift_selections: tuple[DirectSourceDriftSelection, ...],
) -> DirectPlan: ...
def find_direct_out_of_scope_consumers(
    *,
    realized_project: RealizedProject,
    catalog: CatalogSnapshot,
    prerequisite_execution_scope: tuple[LogicalResourceKey, ...],
    execution_scope: tuple[LogicalResourceKey, ...],
) -> tuple[str, ...]: ...
def resolve_variable_values(
    values: Mapping[str, object],
    environment: Mapping[str, str],
    field_path_prefix: str,
    defer_missing_environment: bool,
) -> dict[str, object]: ...
def interpolate_config_value(
    value: object, variables: Mapping[str, object], environment: Mapping[str, str], field_path: str
) -> object: ...
def model_header_block(contents: str) -> tuple[str, str, int, int] | None: ...
def sql_test_blocks(contents: str) -> list[tuple[int, tuple[str, str, int] | None]]: ...
def sql_test_header_fields(
    file_path: str, header: list[tuple[str, object]]
) -> tuple[str | None, str]: ...
def unsupported_audit_header_keys(
    file_path: str, header: list[tuple[str, object]]
) -> str | None: ...
def audit_header_fields(
    file_path: str, header: list[tuple[str, object]]
) -> tuple[str, str | None, str | None, bool, int | None, int | None, bool | None]: ...
def sql_test_wrap_with_ctes(query: str, ctes: list[tuple[str, str]]) -> str: ...
def sql_test_chain_step_queries(
    expected_column_names: list[str],
    output_columns: list[tuple[str, str]],
    actual_cte_name: str,
    expected_query: str,
) -> tuple[str, str]: ...
def generic_audit_instances(
    file_path: str,
    raw_audits: object,
    implicit_arguments: list[tuple[str, object]],
    default_name_prefix: str,
) -> list[
    tuple[
        str,
        list[tuple[str, object]],
        str,
        str,
        str | None,
        bool,
        int | None,
        int | None,
        bool | None,
    ]
]: ...
def generic_audit_parameter_names(query: str) -> tuple[list[str], list[str]]: ...
def render_generic_audit(
    file_path: str,
    definition_name: str,
    query: str,
    raw_parameter_names: list[str],
    quoted_parameter_names: list[str],
    arguments: list[tuple[str, object]],
) -> str: ...
def parse_duration_seconds(value: object, field_path: str, allow_zero: bool) -> int: ...
def parse_freshness_policy(
    payload: object, label: str, file_path: str
) -> SourceFreshnessPolicy | None: ...
def parse_pipeline_mode(
    payload: Mapping[str, object], file_path: str, default_mode: str
) -> str: ...
def parse_pipeline_settings(
    payload: Mapping[str, object], file_path: str, pipeline_name: str, mode: str
) -> tuple[
    ReplayOnChangePolicy | None,
    BoundedReplayFallback | None,
    PipelineNaming,
    PipelineProtection | None,
    AuditDefaults,
    ExecutionSettings,
    ModelRetentionPolicy | Literal[False] | None,
    str | None,
]: ...
def parse_source_registry(
    files: list[tuple[str, object, str | None]],
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    default_ttl: str | None,
    default_retention: KafkaRetentionPolicy | Literal[False] | None,
    default_broker_list: str | None,
    default_freshness: SourceFreshnessPolicy | None,
    derive_name: Callable[..., tuple[str, str, str]],
) -> list[tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...]]: ...

DEFAULT_ADAPTER_NAME: str
DEFAULT_PIPELINE_PREFIX: str
DEFAULT_TABLE_PREFIX: str
DEFAULT_VIEW_PREFIX: str
RUN_UNRESPONSIVE_AFTER_SECONDS: int
DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS: int
DEFAULT_POSTGRES_PORT: int
DEFAULT_KAFKA_FORMAT: str
DEFAULT_SQL_MODEL_ENGINE: str

def check_model_header_keys(header: Mapping[str, object], file_path: str) -> None: ...
def parse_model_spec(
    header: Mapping[str, object], file_path: str, check_query: Callable[..., str | None]
) -> tuple[str, dict[str, Any]]: ...
def resolve_effective_project_configuration(
    *,
    loaded: LoadedProjectConfiguration,
    selected_target: str | None,
    cli_variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> EffectiveProjectConfiguration: ...
def load_project_configuration(*, project_dir: Path) -> LoadedProjectConfiguration: ...
def find_project_configuration_dir(path: Path) -> Path | None: ...
def infer_pipeline_sources(
    pipelines: list[tuple[str, list[tuple[str, str]]]],
    inputs_by_transform: dict[str, str],
    sources: set[str],
) -> tuple[list[str | None], str | None]: ...
def audit_readiness(
    observation: tuple[
        str,
        bool,
        bool,
        int | None,
        int | None,
        bool,
        tuple[int, int, int, int, bool, float | None] | None,
        float | None,
        str | None,
        int | None,
    ],
    thresholds: tuple[float, float],
) -> tuple[str, int | None, float | None, list[str]]: ...
def select_rollback_publications(
    publications: list[tuple[str, tuple[int, str] | None, list[tuple[str, str, str]]]],
    active: list[tuple[str, str, str]],
    deployment_id: str | None,
) -> tuple[int, int]: ...

type _RetainedDeployment = tuple[str, str, bool, list[tuple[str | None, bool, str, str, str]]]
type _RetainedPublication = tuple[str, tuple[int, str], int, list[str], list[tuple[str, str, str]]]

def janitor_binding_activity(
    deployments_argument: list[_RetainedDeployment],
    publications: list[_RetainedPublication],
    active: list[tuple[str, str, str]],
) -> tuple[set[str], list[tuple[str, str]]]: ...
def janitor_preview(
    deployments_argument: list[_RetainedDeployment],
    publications: list[_RetainedPublication],
    active: list[tuple[str, str, str]],
    physical_relations: set[tuple[str, str]],
    retention: tuple[str, int, int, int],
) -> list[tuple[int, bool, str]]: ...
def inspected_publish_candidates(
    candidates: list[tuple[str, str]], deployment_id: str
) -> list[int]: ...
def publish_stable_bindings(
    relations: list[tuple[str | None, str, str, str]],
    existing: set[str],
    default_database: str,
    deployment_id: str,
) -> list[tuple[str, str, str]]: ...
def obsolete_binding_removals(
    current: list[tuple[str | None, bool, str, str, str]],
    deployments_argument: list[_RetainedDeployment],
    publications: list[tuple[str, list[str]]],
    active: list[tuple[str, str, str]],
    default_database: str,
) -> list[tuple[str, str]]: ...
def binding_replacement_preview(
    bindings: list[tuple[str, str, str]],
    removals: list[tuple[str, str]],
    active: list[tuple[str, str, str]],
) -> tuple[
    bool, list[int], list[tuple[int, str]], list[tuple[int, str]], list[tuple[str, str]]
]: ...

type _ReconcileKey = tuple[str | None, str, str]
type _ReconcileObject = tuple[
    str,
    _ReconcileKey,
    tuple[str, list[str], str | None, list[tuple[str, str, str | None]]] | None,
    tuple[str, str] | None,
]

def classify_reconcile_targets(
    desired: list[_ReconcileObject],
    actual: list[_ReconcileObject],
    selected: list[_ReconcileKey],
) -> list[tuple[_ReconcileKey, str, tuple[int, int | None] | None, list[str]]]: ...
def parse_deployment_diff(comparison: str, identifiers: list[str]) -> tuple[str, str]: ...
def deployment_diff_relations(
    from_: str,
    to: str,
    deployments_argument: list[_RetainedDeployment],
    active: list[tuple[str, str, str]],
    default_database: str,
    /,
) -> tuple[list[tuple[str, str, str | None, str | None]], list[str]]: ...
def deployment_diff_count_sql(database: str, relation: str) -> str: ...

type _DiffSide = tuple[bool, tuple[list[tuple[str, str, str | None]], int | None] | None]

def deployment_diff_status(from_: _DiffSide, to: _DiffSide, /) -> str: ...

type _SourceResource = tuple[str, str, list[str], list[tuple[str, str]]]
type _SourceProject = tuple[
    list[str],
    list[tuple[str, str, bool, int | None, list[int], list[str]]],
    list[int],
    list[list[_SourceResource]],
]
type _SourceRelation = tuple[str, list[tuple[str, str]], list[tuple[str, str]], str | None]

def plan_preserved_managed_sources(
    plan: tuple[list[int], list[int], list[tuple[str, int]]],
    project_argument: _SourceProject,
    relations_argument: list[_SourceRelation],
    database: str,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[tuple[int, int]],
    list[tuple[tuple[int, int], tuple[int, int], str, list[str]]],
    list[tuple[int, int]],
]: ...
def drifted_managed_sources(
    project_argument: _SourceProject, relations_argument: list[_SourceRelation]
) -> list[tuple[int, list[str], list[str]]]: ...

type _WorkflowStatement = tuple[str, str, str, str, bool]

def virtual_workflow_statements(
    context: tuple[str, str, str, tuple[str, str, str, str, str], str | None, bool, bool],
    rendered: tuple[
        list[tuple[str, str | None]],
        str,
        list[str],
        list[tuple[str, str]],
        list[str],
        list[tuple[str, str]],
        list[tuple[str, str]],
        float,
    ],
    roots: list[
        tuple[str, AdapterReplayRequest, bool, tuple[str | None, str, str], bool, list[str]]
    ],
) -> list[_WorkflowStatement]: ...
def direct_workflow_statements(
    preparation: tuple[
        str,
        str,
        list[tuple[str, str]],
        list[tuple[str, str, str, str, str]],
        list[str],
        list[tuple[str, str]],
        list[tuple[str, str]],
    ],
    execution: tuple[
        float,
        list[tuple[str, bool]],
        list[tuple[str, str]],
        list[tuple[str, str, str, str]],
        list[tuple[str, str]],
        int,
    ],
) -> tuple[list[_WorkflowStatement], list[_WorkflowStatement]]: ...
def direct_fingerprint_record(
    identity: tuple[
        str,
        str,
        str,
        list[tuple[str, str]],
        tuple[str, list[str], str | None, str | None, list[tuple[str, str]] | None] | None,
    ],
    query: str,
    database: str,
    workflow_id: str,
) -> tuple[str, str, str, str]: ...
def direct_model_drift_reasons(
    identity: tuple[
        str,
        str,
        str,
        list[tuple[str, str]],
        tuple[str, list[str], str | None, str | None, list[tuple[str, str]] | None] | None,
    ],
    query: str,
    baseline: tuple[str, str] | None,
) -> list[str]: ...
def direct_model_identity_metadata(
    identity: tuple[
        str,
        str,
        str,
        list[tuple[str, str]],
        tuple[str, list[str], str | None, str | None, list[tuple[str, str]] | None] | None,
    ],
) -> str: ...
def population_creation_order(
    objects: list[tuple[int, list[int]]], planned: set[int]
) -> list[int]: ...
def population_fan_in_roots(
    planned: set[int],
    roots: list[tuple[int, list[int]]],
    tables: list[tuple[str, int]],
    views: list[tuple[int, str, str, list[int]]],
) -> list[tuple[int, int, tuple[int, int], int]]: ...
def population_source_actions(
    objects: list[tuple[str, str, str | None]],
    raw_prefix: str,
    existing_names: set[str],
) -> list[str]: ...
def direct_population_scope(
    desired: list[tuple[str, int]],
    resources: list[list[tuple[str, str]]],
    execution_scope: list[int],
    roots: list[tuple[int, list[int], str]],
    external_tables: list[tuple[str, int]],
) -> tuple[list[int], list[tuple[int, list[int], int]]]: ...
def destruction_drop_statements(
    relations: list[tuple[str, str, str]], size_override: int | None
) -> list[tuple[str, str, str]]: ...
def realize_clickhouse_managed_source(
    logical_name: str,
    source_kind: str,
    format: str,
    consumer_group: str | None,
    project_name: str | None,
    target_name: str | None,
    setting_keys: list[str],
) -> tuple[str, str, str, str, str]: ...
def clickhouse_raw_landing_columns() -> list[tuple[str, str]]: ...
def clickhouse_external_source_relation_sql(table_name: str, columns: list[str | None]) -> str: ...
def clickhouse_postgres_source_relation_sql(
    host: str, port: str, database: str, table: str, user: str, password_env: str | None
) -> str: ...
def workflow_step_segment(value: str) -> str: ...
def workflow_terminate_sql(sql: str) -> str: ...
def resolve_direct_replays(
    entries: list[tuple[str, list[str]]],
    replays: list[tuple[str, str, str, str, str, str]],
    forced_start_time: bool,
    desired_tables: list[tuple[str, list[tuple[str, str]]]],
    catalog: list[tuple[str, list[tuple[str, str]]]],
) -> list[tuple[str, str, str, str | None]]: ...
def realize_direct_replay_capture(
    mode: str,
    ranges: list[tuple[str | None, str, str, bool]],
) -> tuple[
    list[tuple[str, str, bool, str | None]],
    list[tuple[str, str | None]],
    list[tuple[int, int, int]],
]: ...
def is_deployment_id(text: str) -> bool: ...
def is_deployment_physical_name(physical_name: str) -> bool: ...
def deployment_inventory_summaries(
    records: list[tuple[str, str, str, list[str], list[tuple[str, str]]]],
    physical_candidate_names: list[str],
    active_bindings: list[tuple[str, str]],
    publications: list[tuple[str, str]],
    existing_names: list[str],
) -> list[
    tuple[
        str,
        str,
        str | None,
        str | None,
        list[str],
        list[str],
        list[str],
        list[str],
        str | None,
    ]
]: ...
def order_relations_for_drop(relations: list[tuple[str, list[str]]]) -> list[int]: ...

class DesiredGraph:
    def __init__(self, desired_state: DesiredState) -> None: ...
    def reverse_deps(self) -> dict[ObjectKey, tuple[ObjectKey, ...]]: ...
    def order(self, included_keys: Iterable[ObjectKey]) -> tuple[ObjectKey, ...]: ...
    def descendants(self, root_key: ObjectKey) -> tuple[ObjectKey, ...]: ...
    def nearest_replay_anchor(self, root_key: ObjectKey, allow_root_key: bool) -> ObjectKey: ...
    def rebuild_subtrees(
        self, object_changes: Iterable[PlannedObjectChange]
    ) -> tuple[RebuildSubtree, ...]: ...

def validate_replay_policies(
    project_policy: tuple[str, bool, bool, bool] | None,
    pipelines: list[tuple[str, str, bool, bool, bool, list[tuple[str, bool, bool, bool]]]],
) -> None: ...
def validate_project_inputs(
    sources: list[tuple[str, str]],
    pipelines: list[tuple[str, str, bool, bool, bool, list[tuple[str, bool, bool, bool]]]],
    project_references: tuple[str, list[tuple[str, str]]] | None,
    project_policy: tuple[str, bool, bool, bool] | None,
    tests: list[tuple[str, str]],
    audits: list[tuple[str, str]],
) -> None: ...
def quality_test_identity(
    node_name: str,
    mode: str,
    assertion_names: list[str],
    expected_targets: list[str],
    authored_ctes: list[tuple[str, str]],
    execution_sql: str,
) -> tuple[str, str, str]: ...
def quality_audit_identity(
    node_name: str,
    attached_column: str | None,
    attached_model: str | None,
    attachment_kind: str,
    referenced_models: list[str],
    severity: str,
    definition_sql: str,
    execution_sql: str,
) -> tuple[str, str, str]: ...
def build_manifest_contents(
    project_identity: str,
    target_name: str,
    target_database: str,
    is_production: bool,
    pipeline_names: list[str],
    logical_resources: list[tuple[str, str, list[str], list[tuple[str, str]]]],
) -> tuple[list[str], list[list[str]], str]: ...
def classify_sql_test_ctes(names: list[str], macro_mode: bool) -> list[str]: ...

class SqlTestChainRegistry:
    def __init__(self, refs_by_model: dict[str, list[str]]) -> None: ...
    def resolve(
        self, logical_name: str, mocks: set[str], assembled: set[str]
    ) -> tuple[list[str], list[str]]: ...

def managed_catalog_relations(
    relations: list[tuple[str, bool, str | None]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, bool]]]: ...
def root_deployment_state(
    bindings: list[tuple[str, str, str]],
    candidates: list[tuple[str, str, str]],
    root_database: str | None,
    root_name: str,
) -> tuple[str, str | None]: ...
def deployment_candidate_logical_name(physical_name: str) -> str | None: ...
def load_actual_state_from_snapshot(
    *, snapshot: PlanningWarehouseSnapshot, desired_state: DesiredState, database: str
) -> ActualState: ...
def analyze_model_sql(
    *,
    sql: str,
    engine: str,
    order_by: tuple[str, ...],
    partition_by: str | None,
    ttl: str | None,
    analyzer: SqlTreeAnalyzer,
) -> tuple[SqlModelAnalysis, SqlModelTree]: ...
def analyze_storage_expressions(
    *,
    order_by: tuple[str, ...],
    partition_by: str | None,
    ttl: str | None,
    output_columns: tuple[SqlOutputColumn, ...],
    analyzer: SqlTreeAnalyzer,
) -> tuple[SqlStorageExpression, ...]: ...
def build_aggregate_facts(
    *, function_names: list[str], has_group_by: bool, engine: str
) -> SqlAggregateFacts: ...
def compile_pipeline(
    *, loaded_pipeline: LoadedPipeline, sql_analyzer: SqlModelAnalyzer
) -> CompiledPipeline: ...
def assemble_project(
    *,
    inputs: CompileProjectInputs,
    reference_rewriter: SqlReferenceRewriter,
    sql_analyzer: SqlModelAnalyzer,
) -> CompiledProject: ...
def realize_project(
    *,
    project: CompiledProject,
    adapter_profile: CompilerAdapterProfile,
    sql_analyzer: SqlModelAnalyzer,
) -> RealizedProject: ...
def build_direct_reconcile_actual_state(
    *, desired_state: DesiredState, catalog: CatalogSnapshot
) -> ActualState: ...
