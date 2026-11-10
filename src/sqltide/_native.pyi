import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterAdoptedSourceRealizationRequest,
    AdapterBindingReplacementRequest,
    AdapterConnectionConfig,
    AdapterCurrentQualityNode,
    AdapterDeploymentInventory,
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterManagedSource,
    AdapterManagedSourceRealizationRequest,
    AdapterManifest,
    AdapterManifestSnapshot,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterModelRealization,
    AdapterModelRealizationRequest,
    AdapterPhysicalRelationMapping,
    AdapterQueryCancellation,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterRefreshState,
    AdapterRelationCleanupRequest,
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterReplayQuery,
    AdapterReplayRequest,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
    AdapterSensorState,
    AdapterSourceRealization,
    AdapterStableView,
    AdapterStatementProgress,
    AdapterTable,
    AdapterTargetMutationLock,
    AdapterView,
    AdapterViewRealizationRequest,
    AdapterWarehouseHealth,
    CatalogRelation,
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.adapter.types import AdapterResourceRenderer, AdapterSetDifferenceComparisonRenderer
from sqltide.cli.build.models import (
    BuildCommandOptions,
    BuildProtectionRequirement,
    DirectBuildPreviewContext,
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualBuildPreviewContext,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.cli.selection.models import SelectionResolution
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.audit_discovery.models import LoadedGenericSqlAuditInstance, LoadedSqlAudit
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledProject,
    CompileProjectInputs,
    CompilerAdapterProfile,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ExternalSourceReplayConfig,
    LogicalResourceKey,
    ObjectKey,
    ParsedRef,
)
from sqltide.compiler.discovery.models import (
    AuditDefaults,
    DiscoveredPipelineDirectory,
    DiscoveredProjectFile,
    DiscoveredProjectInputs,
    DiscoveredSourceFile,
    EffectiveProjectConfiguration,
    ExecutionSettings,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaRetentionPolicy,
    LoadedPipeline,
    LoadedProject,
    LoadedProjectConfiguration,
    ModelRetentionPolicy,
    Pipeline,
    PipelineNaming,
    PipelineProtection,
    PostgresRefreshSourceStep,
    Project,
    ProjectNaming,
    ReplayOnChangePolicy,
    SourceFreshnessPolicy,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    PipelineMode,
    ReplayLineageMode,
)
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.graph.types import DependencyEdgeType, GraphTraversalDirection
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.pipeline.models import CompileAnalysis, RealizedProject
from sqltide.compiler.planner.models import (
    ActualState,
    DeploymentPlan,
    DeploymentRecord,
    DeploymentWatermarkRecord,
    DirectPlan,
    DirectRowErrorPolicyChange,
    DirectSourceDriftSelection,
    DirectWarehouseSnapshot,
    MetadataState,
    ObjectStateMetadataRow,
    ObjectStateRecord,
    PlannedObjectChange,
    PlanningWarehouseSnapshot,
    PublishEventRecord,
    RebuildSubtree,
    RootDeploymentInspection,
)
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.compiler.rules.models import FormattedSource
from sqltide.compiler.sql_analysis.models import (
    SqlCatalogAnalysis,
    SqlModelAnalysis,
    SqlNamedQuery,
    SqlOutputColumn,
    SqlQueryRewriteResult,
    SqlReference,
    SqlRelationRewrite,
    SqlResolvedQuery,
    SqlStorageExpression,
)
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase
from sqltide.diagnostics.models import CompilerDiagnostic
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditRunResult
from sqltide.executor.backfill.models import (
    BackfillBootstrapRequest,
    BackfillDeploymentIdentity,
    BackfillExecutionResult,
    RootBackfillReport,
)
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionExecutionResult,
    DestructionPlan,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.destruction.types import DestructionPlanningConnection, DestructionPlanStore
from sqltide.executor.direct.models import (
    DirectBuildAudit,
    DirectBuildExecutionResult,
    DirectBuildRequest,
    DirectBuildResult,
    DirectBuildWorkflow,
    DirectReplayCapture,
    DirectRuntimeExecution,
    DriftedManagedSource,
)
from sqltide.executor.observability.models import RunStartupTimings
from sqltide.executor.population.models import (
    PopulationPlan,
    PopulationRealization,
    PopulationSourcePreparation,
    PopulationSourceRecreation,
    PopulationWatermark,
)
from sqltide.executor.promotion.models import (
    DeploymentPromotionPreview,
    RollbackPlan,
    RollbackRequest,
)
from sqltide.executor.readiness.models import (
    AuditDeploymentCandidate,
    DeploymentAuditRequest,
    DeploymentAuditResult,
    DeploymentReadinessThresholds,
    LoadedAuditDeployment,
    RootAuditResult,
)
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    WarehouseStatement,
    WorkflowExecutionResult,
)
from sqltide.executor.workflow.types import NativeStatement, WorkflowEventEmitter

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

def render_clickhouse_set_difference_comparison(targets: tuple[object, ...]) -> str: ...
def render_clickhouse_ensure_database(database: str) -> str: ...
def render_clickhouse_replay_coverage_query(request: object) -> str: ...
def render_clickhouse_replay_from_capture(request: object) -> str: ...
def render_clickhouse_replay_from_deployment(request: object) -> tuple[str, ...]: ...
def database_scoped_consumer_group(*, consumer_group: str, database: str) -> str: ...
def render_clickhouse_metadata_migration_statements(database: str) -> tuple[str, ...]: ...
def render_clickhouse_metadata_migration_workflow(database: str) -> tuple[str, ...]: ...
def render_clickhouse_metadata_state(
    *, database: str, state: AdapterMetadataState
) -> tuple[str, ...]: ...
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
    *, database: str, fingerprints: tuple[AdapterDirectFingerprintRecord, ...]
) -> tuple[str, ...]: ...
def render_clickhouse_manifest_history_query(
    database: str, project_identity: str, target_name: str, target_database: str
) -> str: ...
def render_clickhouse_manifest_publication(
    *, database: str, manifest: AdapterManifest
) -> tuple[str, ...]: ...
def render_clickhouse_sensor_state_inserts(
    *, database: str, state: AdapterSensorState, include_migration: bool = False
) -> tuple[str, ...]: ...
def render_clickhouse_sensor_retention_cleanup(
    *, database: str, retention_days: int
) -> tuple[str, ...]: ...
def render_clickhouse_run_event_inserts(
    *, database: str, events: tuple[AdapterRunEventRecord, ...], include_migration: bool = False
) -> tuple[str, ...]: ...
def render_clickhouse_run_statement_inserts(
    *,
    database: str,
    statements: tuple[AdapterRunStatementRecord, ...],
    include_migration: bool = False,
) -> tuple[str, ...]: ...
def render_clickhouse_latest_node_status_query(
    *,
    database: str,
    project_identity: str,
    target_identity: str,
    nodes: tuple[AdapterCurrentQualityNode, ...],
) -> str: ...
def extract_references(sql: str) -> tuple[SqlReference, ...]: ...
def expand_sql_macros(
    *,
    sql: str,
    file_path: Path,
    registry: MacroRegistry,
    context: MacroContext,
    source_line: int = 1,
    source_column: int = 1,
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

def load_macro_registry(*, macro_files: Iterable[DiscoveredProjectFile]) -> MacroRegistry: ...
def load_declarations(
    *,
    constant_files: tuple[DiscoveredProjectFile, ...],
    enum_files: tuple[DiscoveredProjectFile, ...],
) -> Declarations: ...
def find_macro_call_names(sql: str) -> tuple[str, ...]: ...
def discover_sql_audits(
    *,
    root: Path,
    contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
    generic_audit_instances: tuple[LoadedGenericSqlAuditInstance, ...] = (),
) -> list[LoadedSqlAudit]: ...
def discover_sql_tests(
    *,
    root: Path,
    contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> list[LoadedSqlTest]: ...
def sql_test_target_names(*, loaded_test: LoadedSqlTest) -> tuple[str, ...]: ...
def replace_refs(*, sql: str, resolver: dict[str, str], rewriter: SqlReferenceRewriter) -> str: ...
def build_sql_test_cases(
    *,
    loaded_tests: tuple[LoadedSqlTest, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    reference_rewriter: SqlReferenceRewriter,
    comparison_renderer: AdapterSetDifferenceComparisonRenderer,
    dialect: str,
) -> tuple[SqlTestCase, ...]: ...
def build_test_quality_identity(
    *, loaded_test: LoadedSqlTest, test_case: SqlTestCase, dialect: str
) -> QualityNodeIdentity: ...
def build_audit_quality_identity(
    *, audit: LoadedSqlAudit, resolved_query: str, dialect: str
) -> QualityNodeIdentity: ...
def require_quality_identity(identity: QualityNodeIdentity | None) -> QualityNodeIdentity: ...
def build_model_audit_instances(
    *, models: tuple[TransformStep | ViewStep, ...]
) -> tuple[LoadedGenericSqlAuditInstance, ...]: ...
def rewrite_template_query(
    *,
    template: str,
    dialect: str,
    database_placeholder: str,
    relation_rewrites: tuple[SqlRelationRewrite, ...] = (),
    predicate: str | None = None,
    prepend_ctes: tuple[SqlNamedQuery, ...] = (),
) -> SqlQueryRewriteResult: ...

class PolyglotError(ValueError): ...

def sql_infer_projection_names(sql: str, dialect_name: str) -> tuple[str, ...]: ...

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
        references: list[SqlReference],
        resolver: Mapping[str, str],
        database_placeholder: str,
    ) -> tuple[str, str]: ...
    def rewrite_references(self, sql: str, resolver: Mapping[str, str]) -> str: ...
    def canonicalize_query(self, sql: str, database_placeholder: str) -> tuple[str, str]: ...
    def storage_column_names(self, expression: str) -> list[str]: ...

class SqlModelAnalyzer:
    def __init__(self, *, dialect: str) -> None: ...
    def analyze(
        self,
        *,
        sql: str,
        engine: str,
        order_by: tuple[str, ...],
        partition_by: str | None,
        ttl: str | None,
    ) -> SqlModelAnalysis: ...
    def resolve(
        self, *, analysis: SqlModelAnalysis, resolver: Mapping[str, str]
    ) -> SqlResolvedQuery: ...
    def uses_prewhere(self, *, analysis: SqlModelAnalysis) -> bool: ...
    def canonicalize_query(self, *, sql: str) -> SqlResolvedQuery: ...
    def analyze_storage(
        self,
        *,
        order_by: tuple[str, ...],
        partition_by: str | None,
        ttl: str | None,
        output_columns: tuple[SqlOutputColumn, ...],
    ) -> tuple[SqlStorageExpression, ...]: ...
    def is_row_level(self, *, analysis: SqlModelAnalysis) -> bool: ...
    def quarantine_ineligibility(self, *, analysis: SqlModelAnalysis) -> tuple[str, str] | None: ...
    def quarantine_split(self, *, sql: str, model_name: str) -> tuple[str, str]: ...

class SqlReferenceRewriter:
    def __init__(self, *, dialect: str) -> None: ...
    def rewrite(self, *, sql: str, resolver: Mapping[str, str]) -> str: ...

def rewrite_query(
    *,
    sql: str,
    dialect: str,
    relation_rewrites: tuple[SqlRelationRewrite, ...] = (),
    predicate: str | None = None,
    prepend_ctes: tuple[SqlNamedQuery, ...] = (),
) -> SqlQueryRewriteResult: ...
def sql_quarantine_ineligibility(sql: str, dialect_name: str) -> tuple[str, str] | None: ...
def sql_quarantine_split(sql: str, model_name: str, dialect_name: str) -> tuple[str, str]: ...
def sql_select_is_row_level(sql: str, dialect_name: str) -> bool: ...
def analyze_catalog_sql(*, sql: str, dialect: str) -> SqlCatalogAnalysis: ...
def parse_expression_list(*, sql: str, dialect: str) -> tuple[str, ...]: ...
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
def build_project_graph_from_compiled_project(*, project: CompiledProject) -> ProjectGraph: ...
def collect_reachable_keys(
    *,
    graph: ProjectGraph,
    root_keys: frozenset[LogicalResourceKey],
    direction: GraphTraversalDirection,
    edge_types: frozenset[DependencyEdgeType],
) -> tuple[LogicalResourceKey, ...]: ...
def resolve_named_model_keys(
    *, compiled_pipelines: tuple[CompiledPipeline, ...], selectors: tuple[str, ...]
) -> frozenset[LogicalResourceKey]: ...
def build_destruction_challenges(
    *, pipeline_names: tuple[str, ...], production_reset: bool = False
) -> tuple[str, ...]: ...
def plan_destruction(
    *,
    request: DestructionRequest,
    analysis: CompileAnalysis,
    connection: DestructionPlanningConnection,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    plan_id: str | None = None,
) -> DestructionPlan: ...
def list_inactive_pipelines(
    *,
    request: DestructionRequest,
    analysis: CompileAnalysis,
    connection: DestructionPlanningConnection,
) -> tuple[InactivePipeline, ...]: ...
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
def expand_population_plan(
    *, plan: PopulationPlan, desired_state: DesiredState
) -> PopulationPlan: ...
def plan_population_objects(
    *, plan: PopulationPlan, desired_state: DesiredState, default_database: str
) -> tuple[PopulationRealization, ...]: ...
def build_population_replay_templates(
    *, plan: PopulationPlan, desired_state: DesiredState, default_database: str
) -> tuple[tuple[ObjectKey, AdapterReplayRequest], ...]: ...
def build_population_replay_requests(
    *,
    plan: PopulationPlan,
    desired_state: DesiredState,
    default_database: str,
    watermarks: tuple[PopulationWatermark, ...],
    boundary_time: str,
) -> tuple[tuple[ObjectKey, AdapterReplayRequest], ...]: ...
def plan_population_sources(
    *,
    desired_state: DesiredState,
    default_database: str,
    existing_relation_names: frozenset[str],
) -> tuple[PopulationSourcePreparation, tuple[PopulationRealization, ...]]: ...
def row_error_policy_view_steps(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    database: str,
    render_view: Callable[[str], str | None],
) -> tuple[list[tuple[str, bool]], list[tuple[str, str]]]: ...
def row_error_policy_restore_statements(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    render_restore: Callable[[str], str],
    database: str,
) -> list[NativeStatement]: ...
def row_error_policy_failure_message(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    failed_step_id: str,
    cause: BaseException,
) -> str | None: ...
def with_metadata_dead_letter_target[ResourceT](
    *, resource: ResourceT, database: str, metadata_database: str
) -> ResourceT | AdapterMaterializedView: ...
def build_direct_population_plan(
    *, plan: DirectPlan, realized_project: RealizedProject
) -> PopulationPlan: ...
def build_direct_execution_result(
    *,
    request: DirectBuildRequest,
    execution: WorkflowExecutionResult,
    captures: tuple[DirectReplayCapture, ...],
) -> DirectBuildExecutionResult: ...
def build_virtual_execution_result(
    *,
    request: BackfillBootstrapRequest,
    root_reports: tuple[RootBackfillReport, ...],
    existing_relation_names: frozenset[str],
    execution: WorkflowExecutionResult,
) -> BackfillExecutionResult: ...
def build_root_backfill_reports(
    *, catalog: CatalogSnapshot, desired_state: DesiredState
) -> tuple[RootBackfillReport, ...]: ...
def build_audit_deployment_candidates(
    *, client: AdapterConnection, metadata_database: str, default_database: str
) -> tuple[AuditDeploymentCandidate, ...]: ...
def build_publish_deployment_candidates(
    *, client: AdapterConnection, metadata_database: str, default_database: str
) -> tuple[AuditDeploymentCandidate, ...]: ...
def resolve_publish_deployment_id(
    *,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
    deployment_id: str | None,
) -> str: ...
def load_audit_deployment(
    *, client: AdapterConnection, metadata_database: str, deployment_id: str
) -> LoadedAuditDeployment: ...
def build_root_audit_results(
    *,
    client: AdapterConnection,
    default_database: str,
    inspected_state: InspectedManagedTableState,
    root_keys: tuple[ObjectKey, ...],
    prepared_object_mappings: tuple[tuple[ObjectKey, str], ...],
    thresholds: DeploymentReadinessThresholds,
) -> tuple[RootAuditResult, ...]: ...
def execute_deployment_audit(
    *, request: DeploymentAuditRequest, client: AdapterConnection
) -> DeploymentAuditResult: ...
def build_publish_binding_request(
    *,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
    deployment_id: str,
    inspected_state: InspectedManagedTableState | None = None,
) -> AdapterBindingReplacementRequest: ...
def build_binding_replacement_preview(
    *,
    binding_request: AdapterBindingReplacementRequest,
    active_bindings: tuple[InspectedActiveTableBinding, ...],
) -> DeploymentPromotionPreview: ...
def build_deployment_promotion_preview(
    *,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
    deployment_id: str,
    inspected_state: InspectedManagedTableState,
) -> DeploymentPromotionPreview: ...
def resolve_deployment_rollback(
    *, request: RollbackRequest, client: AdapterConnection
) -> RollbackPlan: ...
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
    deployment_id: str | None = None,
    full_refresh_keys: frozenset[ObjectKey] = frozenset(),
    start_time_keys: frozenset[ObjectKey] = frozenset(),
    start_time: str | None = None,
) -> DeploymentPlan: ...
def plan_direct_build(
    *,
    graph: ProjectGraph,
    realized_project: RealizedProject,
    snapshot: DirectWarehouseSnapshot,
    database: str,
    selected_model_keys: frozenset[LogicalResourceKey],
    selection_mode: DirectSelectionMode | None = None,
    include_missing_upstream: bool = False,
    effective_start_time: str | None = None,
    source_drift_selections: tuple[DirectSourceDriftSelection, ...] = (),
) -> DirectPlan: ...
def find_direct_out_of_scope_consumers(
    *,
    realized_project: RealizedProject,
    catalog: CatalogSnapshot,
    prerequisite_execution_scope: tuple[LogicalResourceKey, ...],
    execution_scope: tuple[LogicalResourceKey, ...],
) -> tuple[str, ...]: ...
def resolve_variable_values(
    *,
    values: Mapping[str, object],
    environment: Mapping[str, str],
    field_path_prefix: str,
    defer_missing_environment: bool = False,
) -> dict[str, object]: ...
def interpolate_config_value(
    *,
    value: object,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    field_path: str,
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

DEFAULT_ADAPTER_NAME: str
DEFAULT_PIPELINE_PREFIX: str
DEFAULT_TABLE_PREFIX: str
DEFAULT_VIEW_PREFIX: str
MANIFEST_VERSION: int
RUN_UNRESPONSIVE_AFTER_SECONDS: int
DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS: int
DEFAULT_POSTGRES_PORT: int
DEFAULT_KAFKA_FORMAT: str
DEFAULT_SQL_MODEL_ENGINE: str

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
def translate_clickhouse_driver_error(error: Exception) -> AdapterWarehouseError: ...
def render_clickhouse_resource(
    *,
    resource: AdapterManagedSource
    | AdapterTable
    | AdapterMaterializedView
    | AdapterView
    | AdapterStableView,
    database: str,
    if_not_exists: bool = False,
) -> str: ...
def realize_clickhouse_source(
    *, request: AdapterManagedSourceRealizationRequest | AdapterAdoptedSourceRealizationRequest
) -> AdapterSourceRealization: ...
def realize_clickhouse_model(
    *, request: AdapterModelRealizationRequest | AdapterViewRealizationRequest
) -> AdapterModelRealization: ...
def build_clickhouse_connection_config(
    *, values: Mapping[str, object], database: str | None
) -> AdapterConnectionConfig: ...
def clickhouse_external_source_relation_sql(table_name: str, columns: list[str | None]) -> str: ...
def clickhouse_postgres_source_relation_sql(
    host: str, port: str, database: str, table: str, user: str, password_env: str | None
) -> str: ...
def is_deployment_id(text: str) -> bool: ...
def is_deployment_physical_name(physical_name: str) -> bool: ...
def quote_clickhouse_sql_string(value: str) -> str: ...
def load_clickhouse_catalog(
    *, connection: AdapterConnection, adapter_identity: AdapterIdentity, database: str
) -> CatalogSnapshot: ...
def load_clickhouse_external_dependants(
    *, connection: AdapterConnection, database: str, relation_names: tuple[str, ...]
) -> tuple[str, ...]: ...
def clickhouse_catalog_resource_matches(
    *,
    resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
    relation: CatalogRelation,
    database: str,
) -> bool: ...
def acquire_clickhouse_target_mutation_lock(
    *, connection: AdapterConnection, database: str, owner_id: str
) -> AdapterTargetMutationLock: ...
def release_clickhouse_target_mutation_lock(
    *, connection: AdapterConnection, lock: AdapterTargetMutationLock
) -> None: ...
def rename_clickhouse_streambuild_metadata(
    *, connection: AdapterConnection, database: str
) -> None: ...
def load_clickhouse_statement_progress(
    *, connection: AdapterConnection, query_id: str
) -> AdapterStatementProgress | None: ...
def cancel_clickhouse_workflow_query(
    *, connection: AdapterConnection, query_id: str
) -> AdapterQueryCancellation: ...
def observe_clickhouse_replay_offset_frontiers(
    *,
    connection: AdapterConnection,
    request: AdapterReplayOffsetProgressRequest,
    previous: tuple[AdapterReplayOffsetFrontier, ...],
) -> tuple[AdapterReplayOffsetFrontier, ...]: ...
def load_clickhouse_warehouse_health(
    *,
    connection: AdapterConnection,
    database: str,
    managed_source_names: tuple[str, ...] = (),
    include_dead_letters: bool = False,
) -> AdapterWarehouseHealth: ...
def load_clickhouse_refresh_states(
    *, connection: AdapterConnection, database: str
) -> tuple[AdapterRefreshState, ...]: ...
def build_inspected_managed_table_state(
    *, client: AdapterConnection, database: str
) -> InspectedManagedTableState: ...
def load_clickhouse_deployment_inventory(
    *, connection: AdapterConnection, database: str
) -> AdapterDeploymentInventory: ...
def render_clickhouse_relation_cleanup(
    *, connection: AdapterConnection, request: AdapterRelationCleanupRequest
) -> tuple[str, ...]: ...
def render_clickhouse_stable_binding_replacement(
    *, connection: AdapterConnection, request: AdapterBindingReplacementRequest
) -> tuple[str, ...]: ...
def load_clickhouse_direct_fingerprints(
    *, connection: AdapterConnection, database: str, logical_model_identities: tuple[str, ...]
) -> AdapterDirectFingerprintSnapshot: ...
def load_clickhouse_manifests(
    *,
    connection: AdapterConnection,
    database: str,
    project_identity: str,
    target_name: str,
    target_database: str,
) -> AdapterManifestSnapshot: ...
def compare_clickhouse_readiness(
    *, connection: AdapterConnection, request: AdapterReadinessRequest
) -> tuple[AdapterReadinessRootObservation, ...]: ...
def build_adapter_resource(
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
) -> (
    AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView | AdapterStableView
): ...
def build_shadow_adapter_resource(
    *,
    desired_object: DesiredTable | DesiredMaterializedView | DesiredView,
    physical_name: str,
    physical_name_by_key: Mapping[ObjectKey, str],
) -> (
    AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView | AdapterStableView
): ...
def build_adapter_replay_query(
    *,
    query: str,
    database_template: str | None = None,
    source_relation_name: str,
    database: str,
    physical_relation_mappings: tuple[AdapterPhysicalRelationMapping, ...],
) -> AdapterReplayQuery: ...
def build_metadata_state(
    *,
    object_states: tuple[ObjectStateRecord, ...],
    deployments: tuple[DeploymentRecord, ...],
    deployment_watermarks: tuple[DeploymentWatermarkRecord, ...],
    publish_events: tuple[PublishEventRecord, ...],
) -> MetadataState: ...
def build_adapter_metadata_state(state: MetadataState) -> AdapterMetadataState: ...
def decode_object_state_metadata_row(row: Mapping[str, object]) -> ObjectStateMetadataRow: ...
def load_planning_warehouse_snapshot(
    *, client: AdapterConnection, database: str
) -> PlanningWarehouseSnapshot: ...
def load_actual_state(
    *, client: AdapterConnection, desired_state: DesiredState, database: str
) -> ActualState: ...
def load_direct_warehouse_snapshot(
    *,
    client: AdapterConnection,
    database: str,
    metadata_database: str | None = None,
    logical_model_identities: tuple[str, ...] = (),
) -> DirectWarehouseSnapshot: ...
def inspect_root_deployment_state(
    *, inspected_state: InspectedManagedTableState, root_key: ObjectKey
) -> RootDeploymentInspection: ...
def build_inspected_managed_table_state_from_catalog(
    *, catalog: CatalogSnapshot
) -> InspectedManagedTableState: ...
def build_deployment_physical_name(*, logical_name: str, deployment_id: str) -> str: ...
def deployment_id_from_physical_name(physical_name: str) -> str: ...
def logical_name_from_physical_name(physical_name: str) -> str: ...
def build_normalized_fingerprint(value: object) -> str: ...

class DirectModelFingerprint:
    @staticmethod
    def identity(*, model: CompiledModel) -> dict[str, object]: ...
    @staticmethod
    def query_hash(query_sql: str) -> str: ...
    @staticmethod
    def record(
        *, model: CompiledModel, database: str, workflow_id: str, tool_version: str
    ) -> AdapterDirectFingerprintRecord: ...
    @staticmethod
    def drift_reasons(
        *, model: CompiledModel, baseline: AdapterDirectFingerprintRecord | None
    ) -> tuple[str, ...]: ...

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

class DesiredGraph:
    def __init__(self, desired_state: DesiredState) -> None: ...
    def reverse_deps(self) -> dict[ObjectKey, tuple[ObjectKey, ...]]: ...
    def order(self, included_keys: Iterable[ObjectKey]) -> tuple[ObjectKey, ...]: ...
    def descendants(self, root_key: ObjectKey) -> tuple[ObjectKey, ...]: ...
    def nearest_replay_anchor(self, root_key: ObjectKey, allow_root_key: bool) -> ObjectKey: ...
    def rebuild_subtrees(
        self, object_changes: Iterable[PlannedObjectChange]
    ) -> tuple[RebuildSubtree, ...]: ...

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
def plan_row_error_policy_changes(
    *, analysis: CompileAnalysis, plan: DirectPlan, relations: tuple[CatalogRelation, ...]
) -> tuple[DirectRowErrorPolicyChange, ...]: ...
def plan_virtual_row_error_policy_changes(
    *,
    analysis: CompileAnalysis,
    planned_model_names: frozenset[str],
    selected_model_keys: frozenset[LogicalResourceKey],
    relations: tuple[CatalogRelation, ...],
) -> tuple[DirectRowErrorPolicyChange, ...]: ...
def build_dag_json(*, graph: ProjectGraph, macro_registry: MacroRegistry) -> str: ...
def resolve_manifest_project_identity(*, analysis: CompileAnalysis) -> str: ...
def build_manifest(
    *,
    analysis: CompileAnalysis,
    invocation_id: str,
    project_identity: str,
    target_database: str,
    tool_version: str,
    project_revision: str | None = None,
    published_at: str | None = None,
) -> AdapterManifest: ...
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
def extract_refs(
    *, sql: str, source_path: Path | None = None, source_line: int = 1, source_column: int = 1
) -> list[ParsedRef]: ...
def infer_transform_source(
    *, query: str, file_path: Path, source_line: int, source_column: int
) -> str: ...
def parse_model_sql(
    *,
    contents: str,
    file_path: Path,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> tuple[dict[str, Any], str]: ...
def read_discovered_files(
    *, file_paths: Iterable[Path], project_dir: Path
) -> tuple[DiscoveredProjectFile, ...]: ...
def discover_macro_files(*, project_dir: Path) -> tuple[DiscoveredProjectFile, ...]: ...
def discover_pipeline_directories(
    *, pipelines_root: Path, project_dir: Path
) -> tuple[DiscoveredPipelineDirectory, ...]: ...
def load_discovered_pipelines(
    *,
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...],
    model_files: tuple[DiscoveredProjectFile, ...],
    macro_registry: MacroRegistry | None,
    macro_context: MacroContext | None,
    sources_by_name: Mapping[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ],
    project: Project | None,
    project_naming: ProjectNaming,
    default_mode: PipelineMode | str,
) -> tuple[LoadedPipeline, ...]: ...
def discover_project_inputs(
    *, pipelines_root: Path, loaded_project: LoadedProject | None
) -> DiscoveredProjectInputs: ...
def discover_pipelines(root: Path) -> list[LoadedPipeline]: ...
def discover_source_registry(
    *,
    project_dir: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    default_managed_source_ttl: str | None = None,
    default_managed_source_retention: KafkaRetentionPolicy | Literal[False] | None = None,
    default_kafka_broker_list: str | None = None,
    default_freshness: SourceFreshnessPolicy | None = None,
    default_kafka_naming_macro: str | None = None,
    macro_registry: MacroRegistry | None = None,
) -> tuple[DiscoveredSourceFile, ...]: ...
def source_registry_by_name(
    source_files: Iterable[DiscoveredSourceFile],
) -> dict[str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep]: ...
def load_project_input_for_path(
    *,
    path: Path,
    selected_target: str | None = None,
    cli_variables: Mapping[str, object] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LoadedProject | None: ...
def validate_replay_policies_for_mode(
    *,
    default_pipeline_mode: PipelineMode,
    project: Project | None,
    project_file_path: Path | None,
    loaded_pipelines: tuple[LoadedPipeline, ...],
) -> None: ...
def validate_attached_project_inputs(
    *,
    source_files: tuple[DiscoveredSourceFile, ...],
    loaded_project: LoadedProject | None,
    loaded_pipelines: tuple[LoadedPipeline, ...],
    loaded_tests: tuple[LoadedSqlTest, ...],
    loaded_audits: tuple[LoadedSqlAudit, ...],
) -> None: ...
def immutable_config_pairs(
    values: tuple[tuple[str, object], ...],
) -> tuple[tuple[str, object], ...]: ...
def parse_sql_header(*, header: str) -> dict[str, object]: ...
def load_pipeline_directories(
    *,
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...],
    sources_by_name: Mapping[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ],
    model_contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
    project_naming: ProjectNaming | None = None,
    default_mode: PipelineMode | str | None = None,
) -> tuple[Pipeline, ...]: ...
def load_pipeline_directory(pipeline_dir: Path) -> LoadedPipeline: ...
def load_transform_from_sql_file(
    *,
    file_path: Path,
    contents: str | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> TransformStep | ViewStep: ...
def load_actual_state_from_snapshot(
    *, snapshot: PlanningWarehouseSnapshot, desired_state: DesiredState, database: str
) -> ActualState: ...
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
def build_compile_inputs(
    *, discovered_inputs: DiscoveredProjectInputs, adapter_profile: CompilerAdapterProfile
) -> CompileProjectInputs: ...
def analyze_project(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    enforce_rules: bool = True,
    source_overrides: Mapping[Path, str] | None = None,
    rule_selection: tuple[str, ...] | None = None,
) -> CompileAnalysis: ...
def format_project_sql(
    *,
    root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    selection: tuple[str, ...] | None,
    paths: tuple[Path, ...],
    fix: bool,
) -> tuple[FormattedSource, ...]: ...
def build_direct_reconcile_actual_state(
    *, desired_state: DesiredState, catalog: CatalogSnapshot
) -> ActualState: ...
def plan_preserved_managed_sources(
    *,
    plan: DirectPlan,
    realized_project: RealizedProject,
    catalog: CatalogSnapshot,
    database: str,
) -> tuple[PopulationSourcePreparation, tuple[PopulationRealization, ...]]: ...
def drifted_managed_sources(
    *, realized_project: RealizedProject, catalog: CatalogSnapshot
) -> tuple[DriftedManagedSource, ...]: ...
def build_direct_build_preview(
    *,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    analysis: CompileAnalysis,
    effective_start_time: str | None = None,
) -> DirectBuildPreviewContext: ...
def build_virtual_build_preview(
    *,
    options: WorkflowPreparationOptions,
    start_time_utc: str | None,
    client: AdapterConnection,
    analysis: CompileAnalysis,
) -> VirtualBuildPreviewContext: ...
def render_direct_build_json(
    *, result: DirectBuildResult, adapter_name: str, audit_result: SqlAuditRunResult
) -> str: ...
def render_direct_build_text(
    *, result: DirectBuildResult, adapter_name: str, audit_result: SqlAuditRunResult
) -> str: ...
def render_interrupted_build_message() -> str: ...
def render_direct_execution_json(
    *,
    request: DirectBuildRequest,
    status: str,
    captures: tuple[DirectReplayCapture, ...],
    execution: WorkflowExecutionResult,
    failed_step_id: str | None,
    error_message: str | None,
    audit_result: SqlAuditRunResult | None = None,
) -> str: ...
def select_direct_build_audits(
    *,
    audits: tuple[LoadedSqlAudit, ...],
    execution_model_names: frozenset[str],
    full_build: bool,
) -> tuple[LoadedSqlAudit, ...]: ...
def prepare_direct_build_audits(
    *, preview: DirectBuildPreviewContext, adapter_profile: CompilerAdapterProfile
) -> tuple[DirectBuildAudit, ...]: ...
def build_protection_requirements(
    *, compiled_project: CompiledProject, execution_model_keys: frozenset[LogicalResourceKey]
) -> tuple[BuildProtectionRequirement, ...]: ...
def confirm_build(
    *,
    options: BuildCommandOptions,
    plan_text: str,
    protection_requirements: tuple[BuildProtectionRequirement, ...] = (),
) -> bool: ...
def validate_build_pipeline_limit(
    *,
    analysis: CompileAnalysis,
    selectors: tuple[str, ...] = (),
    preparation: (
        DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation | None
    ) = None,
) -> None: ...
def render_virtual_build_result(
    *, result: BackfillExecutionResult, database: str, json_output: bool
) -> str: ...
def prepare_build_workflow(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
) -> DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation: ...
def prepare_validated_build(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
) -> DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation: ...
def resolve_prepared_build_scope(
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
) -> tuple[frozenset[str], frozenset[str]]: ...
def prepared_pipeline_scope(
    *,
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
    analysis: CompileAnalysis,
) -> frozenset[str]: ...
def validate_expected_build_scope(
    *,
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
    analysis: CompileAnalysis,
) -> None: ...
def execute_direct_build_command(
    *,
    preparation: DirectWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    confirmation_required: bool = True,
    startup_timings: RunStartupTimings | None = None,
    _acquire_target_mutation_lock: bool = True,
) -> int: ...
def execute_virtual_build_command(
    *,
    preparation: VirtualWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    confirmation_required: bool = True,
    startup_timings: RunStartupTimings | None = None,
    _acquire_target_mutation_lock: bool = True,
) -> int: ...
def execute_mixed_build_command(
    *,
    preparation: MixedWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    startup_timings: RunStartupTimings,
) -> int: ...
def add_empty_replay_source_warnings(
    *,
    client: AdapterConnection,
    catalog: CatalogSnapshot,
    database: str,
    desired_state: DesiredState,
    plan: DeploymentPlan,
) -> DeploymentPlan: ...
def resolve_selected_logical_model_keys(
    *, compiled_pipelines: tuple[CompiledPipeline, ...], selectors: tuple[str, ...]
) -> frozenset[LogicalResourceKey]: ...
def resolve_selection(
    *, realized_project: RealizedProject, graph: ProjectGraph, selectors: tuple[str, ...]
) -> SelectionResolution: ...
def build_backfill_deployment_identity(
    *, deployment_id: str | None
) -> BackfillDeploymentIdentity: ...
def resolve_unsupported_bounded_replay_behavior(
    *,
    catalog: CatalogSnapshot,
    deployment_plan: DeploymentPlan,
    desired_state: DesiredState,
    default_database: str,
    replay_lineage_mode: ReplayLineageMode,
) -> DeploymentPlan: ...
def assemble_virtual_build_workflow(
    *, request: BackfillBootstrapRequest, client: AdapterConnection, plan_json: str
) -> BuildWorkflow: ...
def build_virtual_bootstrap_workflow(*, workflow: BuildWorkflow) -> BuildWorkflow: ...
def assemble_direct_build_workflow(
    *,
    request: DirectBuildRequest,
    client: AdapterConnection,
    snapshot: DirectWarehouseSnapshot,
    plan_json: str,
) -> DirectBuildWorkflow: ...
def execute_direct_build_workflow(
    *,
    workflow: DirectBuildWorkflow,
    connection: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
) -> DirectRuntimeExecution: ...
def persist_direct_fingerprints(
    *,
    request: DirectBuildRequest,
    connection: AdapterConnection,
    applied_model_names: frozenset[str] | None = None,
) -> str | None: ...
def persist_reconciled_direct_fingerprints(
    *,
    models: tuple[CompiledModel, ...],
    target_database: str,
    metadata_database: str,
    workflow_id: str,
    tool_version: str,
    connection: AdapterConnection,
) -> None: ...
def assemble_destruction_workflow(
    *, plan: DestructionPlan, connection: AdapterConnection | None = None
) -> tuple[WarehouseStatement, ...]: ...
def verify_destruction_drift(
    *, frozen_plan: DestructionPlan, replan: Callable[[], DestructionPlan]
) -> DestructionPlan: ...
def execute_destruction(
    *,
    frozen_plan: DestructionPlan,
    actor: DestructionActor,
    challenge_responses: tuple[str, ...],
    reviewed_at: datetime,
    store: DestructionPlanStore,
    connection: AdapterConnection,
    observation_connection: AdapterConnection,
    project_dir: Path,
    replan: Callable[[], DestructionPlan],
    invocation_id: str | None = None,
) -> DestructionExecutionResult: ...

class InMemoryDestructionPlanStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None: ...
    def close(self) -> None: ...
    def save(self, *, plan: DestructionPlan, actor: str) -> None: ...
    def get(self, *, plan_id: str, actor: str) -> DestructionPlan: ...
    def mark_reviewed(self, *, plan_id: str, actor: str) -> datetime: ...
    def reviewed_at(self, *, plan_id: str, actor: str) -> datetime: ...
    def consume(
        self, *, plan_id: str, challenge_responses: tuple[str, ...], actor: str
    ) -> DestructionPlan: ...
