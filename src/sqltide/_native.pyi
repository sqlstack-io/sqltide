import argparse
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, Self, TextIO, final
from uuid import UUID

from sqltide.adapter.classes.adapter import Adapter
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
    AdapterInvocationRecord,
    AdapterManagedSource,
    AdapterManagedSourceRealizationRequest,
    AdapterManifest,
    AdapterManifestSnapshot,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterModelRealization,
    AdapterModelRealizationRequest,
    AdapterNodeResultRecord,
    AdapterPhysicalRelationMapping,
    AdapterQueryCancellation,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterRefreshState,
    AdapterRelationCleanupRequest,
    AdapterReplayColumns,
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterReplayQuery,
    AdapterReplayRequest,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
    AdapterSensorState,
    AdapterSensorStepRecord,
    AdapterSensorTickRecord,
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
from sqltide.auth.models import (
    AccountAuditRecord,
    AuthSettings,
    ProjectRoleAssignment,
    ResolvedSession,
    SessionCredentials,
    UserAccount,
)
from sqltide.auth.types import AuthenticationSource
from sqltide.authorization.models import (
    AuthorizationDecision,
    AuthorizationRequest,
    CapabilityRequest,
    EffectiveCapabilities,
)
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
from sqltide.cli.destruction.models import DestructionCommandOptions
from sqltide.cli.dev.models import DevCommandOptions
from sqltide.cli.entry.models import CliEntrypointHandlers
from sqltide.cli.plan.models import PlanCommandOptions
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
    KafkaSettings,
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
from sqltide.dev_server.models import (
    BuildCancellationOutcome,
    CompileErrorInfo,
    CompileOutcome,
    DevExecutionContext,
    KafkaLagSnapshot,
    KafkaTopicsSnapshot,
    MessageFacetsRequest,
    MessagesQueryRequest,
    ReplayOffsetProgress,
)
from sqltide.dev_server.types import (
    ActivityTone,
    CompileAuthorizationGuard,
    DevServerReporter,
    RunPresentationStatus,
)
from sqltide.diagnostics.models import CompilerDiagnostic, SourceLocation
from sqltide.diagnostics.types import DiagnosticPhase
from sqltide.events.models import (
    AuditCompleted,
    InvocationObservation,
    NodeResultObservation,
    RunCompleted,
)
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult, SqlAuditRunResult
from sqltide.executor.auditing.types import QualityResultStatus
from sqltide.executor.backfill.models import (
    BackfillBootstrapRequest,
    BackfillDeploymentIdentity,
    BackfillExecutionResult,
    RootBackfillReport,
)
from sqltide.executor.deployment.models import (
    DeploymentDiffRequest,
    DeploymentDiffResult,
    DeploymentInventory,
    DeploymentSummary,
)
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionExecutionResult,
    DestructionPlan,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.destruction.types import (
    DestructionClock,
    DestructionPlanningConnection,
    DestructionPlanStore,
)
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
from sqltide.executor.doctor.models import DoctorRequest, DoctorResult
from sqltide.executor.janitor.models import (
    JanitorApplyResult,
    JanitorPreviewResult,
    JanitorRequest,
)
from sqltide.executor.observability.models import (
    QualityResultContext,
    RunStartupTimings,
    TerminalInvocation,
)
from sqltide.executor.population.models import (
    PopulationPlan,
    PopulationRealization,
    PopulationSourcePreparation,
    PopulationSourceRecreation,
    PopulationWatermark,
)
from sqltide.executor.promotion.models import (
    DeploymentPromotionPreview,
    PublishRequest,
    PublishResult,
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
from sqltide.executor.reconcile.models import ReconcilePreview, ReconcileResult
from sqltide.executor.repair.models import RepairActiveViewRequest, RepairActiveViewResult
from sqltide.executor.testing.models import SqlTestExecutionResult
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    PublishedBuildWorkflow,
    WarehouseStatement,
    WorkflowExecutionResult,
)
from sqltide.executor.workflow.types import NativeStatement, WorkflowEventEmitter
from sqltide.provider.models import DiscoveredProvider
from sqltide.sensors.models import (
    CompiledSensors,
    EventSensorDeclaration,
    LoadedSensor,
    PollingSensorDeclaration,
    PollingTickState,
    SensorDispatchSummary,
    SensorRegistry,
    SensorRetryPolicy,
    SensorStreamPosition,
    SensorTickView,
    StepMarker,
    TickAttemptState,
)
from sqltide.sensors.types import (
    DefaultSensorStatus,
    SensorFunction,
    SensorOverrideStatus,
    SensorStepStore,
    SensorTickStatus,
    StepPolicy,
)

type _Span = tuple[int, int, int, int, int, int]

class SqlScanError(ValueError):
    args: tuple[str, str, _Span | None]

def parse_model_header(header: str) -> dict[str, object]: ...
def parse_cli_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace: ...
def run_cli(
    argv: Sequence[str] | None = None,
    *,
    handlers: CliEntrypointHandlers | None = None,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
    adapter_connection: AdapterConnection | None = None,
    observation_adapter_connection: AdapterConnection | None = None,
) -> int: ...
def build_compiler_adapter_profile(adapter: Adapter) -> CompilerAdapterProfile: ...
def resolve_default_database(
    *, loaded_pipelines: Iterable[LoadedPipeline], override: str | None
) -> str: ...
def render_diagnostic(
    *, diagnostic: CompilerDiagnostic, source_by_path: Mapping[Path, str] | None = None
) -> str: ...
def render_diagnostic_json(*, diagnostic: CompilerDiagnostic) -> str: ...
def render_error(error: BaseException) -> str: ...
def authorize_operation(
    *, store: ControlStore, request: AuthorizationRequest
) -> AuthorizationDecision: ...
def effective_capabilities(
    *, store: ControlStore, request: CapabilityRequest
) -> EffectiveCapabilities: ...
def events_from_node_result(
    *, row: NodeResultObservation, previous_status: QualityResultStatus | None, target: str
) -> tuple[AuditCompleted, ...]: ...
def events_from_invocation(
    *, row: InvocationObservation, target: str
) -> tuple[RunCompleted, ...]: ...
def attach_error_diagnostic[ErrorT: Exception](
    *,
    error: ErrorT,
    phase: DiagnosticPhase,
    code: str,
    resource_name: str | None = None,
    location: SourceLocation | None = None,
) -> ErrorT: ...
def render_expected_warehouse_error(
    *, command_name: str, database: str, error: AdapterWarehouseError
) -> str | None: ...
def load_toml(text: str) -> dict[str, Any]: ...
def declares_virtual_pipeline(pipelines_root: Path) -> bool: ...
def compile_sensors(
    *, project_dir: Path, reserved_names: frozenset[str]
) -> CompiledSensors | None: ...
def event_sensor(
    *,
    on: type,
    name: str | None = None,
    targets: Iterable[str] | None = None,
    triggers: Iterable[str] | None = None,
    maximum_event_age_seconds: float | None = None,
    default_status: DefaultSensorStatus = ...,
    retry_policy: SensorRetryPolicy | None = None,
    timeout_seconds: float = 60.0,
) -> Callable[[SensorFunction], EventSensorDeclaration]: ...
def polling_sensor(
    *,
    minimum_interval_seconds: float,
    name: str | None = None,
    default_status: DefaultSensorStatus = ...,
    retry_policy: SensorRetryPolicy | None = None,
    timeout_seconds: float = 60.0,
) -> Callable[[SensorFunction], PollingSensorDeclaration]: ...
def effective_sensor_status(
    *, sensor: LoadedSensor, overrides: Mapping[str, SensorOverrideStatus]
) -> SensorOverrideStatus: ...
def start_invocation(*, invocation_id: str | None = None) -> tuple[str, str, int]: ...
def logical_project_identity(*, project_dir: Path) -> str: ...
def build_invocation_record(
    *, started: tuple[str, str, int], terminal: TerminalInvocation
) -> AdapterInvocationRecord: ...
def build_node_result_record(
    *,
    invocation: AdapterInvocationRecord,
    identity: QualityNodeIdentity,
    context: QualityResultContext,
    status: str,
    severity: str | None,
    failure_count: int,
    payload: dict[str, object],
    error_message: str | None,
) -> AdapterNodeResultRecord: ...
def persist_terminal_observations(
    *,
    client: AdapterConnection,
    database: str,
    invocation: AdapterInvocationRecord,
    node_results: tuple[AdapterNodeResultRecord, ...],
) -> str | None: ...
def persist_run_events(
    *, connection: AdapterConnection, database: str, events: tuple[AdapterRunEventRecord, ...]
) -> None: ...

@final
class RunEventSink:
    """Best-effort run narration: JSONL for pipes plus durable rows for readers."""

    def __init__(
        self,
        *,
        connection: AdapterConnection,
        database: str,
        invocation_id: str,
        project_identity: str | None = None,
        jsonl_stream: TextIO | None = None,
        strict_persistence: bool = False,
    ) -> None: ...
    def set_operation_evidence(self, evidence: dict[str, object] | None) -> None: ...
    def run_started(
        self,
        *,
        command: str,
        mode: str,
        total_statements: int,
        selected_node_count: int,
        display_command: str | None = None,
        selectors: tuple[str, ...] = (),
        start_time: str | None = None,
        executed_logical_ids: tuple[str, ...] = (),
        context_logical_ids: tuple[str, ...] = (),
        startup_timings: RunStartupTimings | None = None,
    ) -> None: ...
    def run_completed(self, *, outcome: str, exit_code: int, error_message: str | None) -> None: ...
    def stop(self) -> None: ...
    def statement_started(self, statement: WarehouseStatement) -> str: ...
    def workflow_prepared(
        self, *, statements: tuple[WarehouseStatement, ...], workflow_sha256: str
    ) -> None: ...
    def audit_started(self, *, name: str) -> None: ...
    def audit_completed(
        self, *, name: str, status: str, failure_count: int, error_message: str | None
    ) -> None: ...
    def statement_completed(
        self,
        *,
        statement: WarehouseStatement,
        error_message: str | None,
        written_rows: int | None,
        elapsed_ms: int,
    ) -> None: ...

def canonical_username(value: str) -> str: ...
def default_control_store_url(*, project_dir: Path) -> str: ...
def rename_streambuild_tables(*, url: str, sqlite_timeout_seconds: float | None = None) -> None: ...

@final
class RelationalDestructionPlanStore:
    """SQLite/PostgreSQL store with durable review and atomic consumption state."""

    def __init__(self, *, url: str, clock: DestructionClock | None = None) -> None: ...
    def close(self) -> None: ...
    def bootstrap(self) -> None: ...
    def save(self, *, plan: DestructionPlan, actor: str) -> None: ...
    def get(self, *, plan_id: str, actor: str) -> DestructionPlan: ...
    def mark_reviewed(self, *, plan_id: str, actor: str) -> datetime: ...
    def reviewed_at(self, *, plan_id: str, actor: str) -> datetime: ...
    def consume(
        self, *, plan_id: str, challenge_responses: tuple[str, ...], actor: str
    ) -> DestructionPlan: ...

class ControlStore:
    """Synchronous account repository shared by dev server worker threads and the CLI."""

    def __init__(self, *, url: str) -> None: ...
    def close(self) -> None: ...
    @property
    def url(self) -> str: ...
    @property
    def authentication_revision(self) -> int: ...
    def bootstrap(self) -> None: ...
    def list_users(self) -> tuple[UserAccount, ...]: ...
    def get_user_by_id(self, *, user_id: UUID) -> UserAccount | None: ...
    def get_user_by_username(self, *, username: str) -> UserAccount | None: ...
    def create_user(
        self,
        *,
        username: str,
        display_name: str | None = None,
        email: str | None = None,
        password: str | None = None,
        authentication_source: AuthenticationSource | None = None,
        external_subject: str | None = None,
        roles: tuple[str, ...] = ("viewer",),
        actor_user_id: UUID | None = None,
    ) -> UserAccount: ...
    def resolve_external_identity(
        self, *, source: AuthenticationSource, subject: str
    ) -> UserAccount | None: ...
    def provision_proxy_user(
        self,
        *,
        subject: str,
        username: str,
        display_name: str | None,
        email: str | None,
        default_role: str,
    ) -> UserAccount: ...
    def authenticate_password(
        self, *, username: str, password: str, session_ttl_seconds: int
    ) -> tuple[UserAccount, SessionCredentials] | None: ...
    def resolve_session(self, *, token: str) -> ResolvedSession | None: ...
    def revoke_session(self, *, token: str, actor_user_id: UUID | None = None) -> None: ...
    def set_user_active(
        self, *, user_id: UUID, is_active: bool, actor_user_id: UUID | None
    ) -> UserAccount: ...
    def update_profile(
        self,
        *,
        user_id: UUID,
        display_name: str | None,
        email: str | None,
        actor_user_id: UUID | None,
    ) -> UserAccount: ...
    def update_account(
        self,
        *,
        user_id: UUID,
        display_name: str | None,
        email: str | None,
        is_active: bool,
        actor_user_id: UUID | None,
    ) -> UserAccount: ...
    def reset_password(
        self, *, user_id: UUID, password: str, actor_user_id: UUID | None
    ) -> None: ...
    def grant_role(
        self, *, user_id: UUID, role_name: str, actor_user_id: UUID | None
    ) -> UserAccount: ...
    def revoke_role(
        self, *, user_id: UUID, role_name: str, actor_user_id: UUID | None
    ) -> UserAccount: ...
    def grant_project_role(
        self,
        *,
        user_id: UUID,
        project_name: str,
        role_name: str,
        target_name: str | None,
        actor_user_id: UUID | None,
    ) -> ProjectRoleAssignment: ...
    def revoke_project_role(
        self, *, assignment_id: UUID, actor_user_id: UUID | None
    ) -> ProjectRoleAssignment: ...
    def list_project_role_assignments(
        self, *, user_id: UUID, project_name: str, include_revoked: bool = False
    ) -> tuple[ProjectRoleAssignment, ...]: ...
    def list_audit_records(self) -> tuple[AccountAuditRecord, ...]: ...
    def prune_sessions(self) -> int: ...

class SensorStateRepository:
    def __new__(cls, *, connection: AdapterConnection, database: str) -> Self: ...
    def ensure_ready(self) -> None: ...
    def warehouse_now(self) -> str: ...
    def read_checkpoint(self, *, sensor_name: str, source: str) -> SensorStreamPosition | None: ...
    def advance_checkpoint(
        self, *, sensor_name: str, source: str, position: SensorStreamPosition
    ) -> None: ...
    def newest_node_result_position(self, *, target: str) -> SensorStreamPosition | None: ...
    def newest_invocation_position(self, *, target: str) -> SensorStreamPosition | None: ...
    def fetch_node_results_after(
        self, *, position: SensorStreamPosition, target: str, limit: int
    ) -> tuple[NodeResultObservation, ...]: ...
    def fetch_node_result(self, *, result_id: str) -> NodeResultObservation | None: ...
    def fetch_invocations_after(
        self, *, position: SensorStreamPosition, target: str, limit: int
    ) -> tuple[InvocationObservation, ...]: ...
    def fetch_invocation(self, *, invocation_id: str) -> InvocationObservation | None: ...
    def previous_node_status(
        self, *, binding_key: str, target: str, position: SensorStreamPosition
    ) -> QualityResultStatus | None: ...
    def record_ticks(self, *, ticks: tuple[AdapterSensorTickRecord, ...]) -> None: ...
    def tick_attempt_state(self, *, sensor_name: str, event_id: str) -> TickAttemptState: ...
    def list_ticks(
        self, *, sensor_name: str, limit: int, after: str | None = None, before: str | None = None
    ) -> tuple[SensorTickView, ...]: ...
    def list_dead_letters(self) -> tuple[SensorTickView, ...]: ...
    def polling_tick_state(self, *, sensor_name: str) -> PollingTickState: ...
    def read_step(self, *, sensor_name: str, event_id: str, step_key: str) -> StepMarker | None: ...
    def record_step(self, *, step: AdapterSensorStepRecord) -> None: ...
    def override_statuses(self) -> dict[str, SensorOverrideStatus]: ...
    def record_override(
        self, *, sensor_name: str, status: SensorOverrideStatus, actor: str | None
    ) -> None: ...
    def apply_tick_retention(self, *, retention_days: int) -> None: ...
    def acquire_dispatch_lease(self, *, owner_id: str, ttl_seconds: float) -> bool: ...

class SensorDispatcher:
    def __init__(
        self,
        *,
        repository: SensorStateRepository,
        event_target: str,
        providers: tuple[DiscoveredProvider, ...] = (),
        dispatcher_id: str | None = None,
        batch_limit: int = 50,
        lease_ttl_seconds: float = 60.0,
        maximum_event_age_seconds: float | None = None,
    ) -> None: ...
    def dispatch_once(self, *, registry: SensorRegistry, target: str) -> SensorDispatchSummary: ...
    def initialize_event_checkpoints(self, *, registry: SensorRegistry, target: str) -> None: ...
    def retry_dead_letter(
        self, *, registry: SensorRegistry, sensor_name: str, event_id: str
    ) -> SensorTickStatus: ...
    def skip_dead_letter(
        self, *, registry: SensorRegistry, sensor_name: str, event_id: str, reason: str
    ) -> None: ...

class DurableStepRunner:
    def __init__(self, *, store: SensorStepStore) -> None: ...
    def run(self, key: str, fn: Callable[[], object], policy: StepPolicy = ...) -> object: ...

class InMemoryStepStore:
    def __init__(self) -> None: ...
    def read_step(self, *, step_key: str) -> StepMarker | None: ...
    def record_step(
        self,
        *,
        step_key: str,
        policy: StepPolicy,
        status: SensorTickStatus,
        attempt: int,
        result_json: str | None,
        error_message: str | None,
    ) -> None: ...

class RepositoryStepStore:
    def __init__(
        self, *, repository: SensorStateRepository, sensor_name: str, event_id: str
    ) -> None: ...
    def read_step(self, *, step_key: str) -> StepMarker | None: ...
    def record_step(
        self,
        *,
        step_key: str,
        policy: StepPolicy,
        status: SensorTickStatus,
        attempt: int,
        result_json: str | None,
        error_message: str | None,
    ) -> None: ...

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
def execute_publish(
    *,
    request: PublishRequest,
    client: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
) -> PublishResult: ...
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
def render_plan_result(
    *,
    plan: DeploymentPlan,
    desired_state: DesiredState,
    database: str,
    adapter_name: str,
    json_output: bool,
    verbose: bool = False,
) -> str: ...
def render_direct_plan_text(
    *,
    plan: DirectPlan,
    adapter_name: str,
    use_color: bool | None = None,
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
def execute_janitor(
    *, request: JanitorRequest, client: AdapterConnection
) -> JanitorPreviewResult | JanitorApplyResult: ...
def execute_recovery_statements(
    *,
    statements: tuple[WarehouseStatement, ...],
    connection: AdapterConnection,
    emitter: WorkflowEventEmitter | None,
    cause: BaseException,
    start_sequence: int,
    completed_step_ids: frozenset[str] | None,
    retry_command: str,
) -> tuple[WarehouseStatement, ...]: ...
def execute_sql_audits(
    *,
    loaded_audits: tuple[LoadedSqlAudit, ...],
    resolver: Mapping[str, str],
    client: AdapterConnection,
    dialect: str,
) -> SqlAuditRunResult: ...
def load_model_anchors(
    *,
    client: AdapterConnection,
    metadata_database: str,
    target_database: str,
    model_names: tuple[str, ...],
    virtual_environments: bool,
) -> dict[str, str]: ...
def load_materialized_model_names(
    *, client: AdapterConnection, database: str, relation_name_by_model: Mapping[str, str]
) -> frozenset[str]: ...
def deferred_audit_result(*, audit: LoadedSqlAudit, state: AuditWarmupState) -> SqlAuditResult: ...
def execute_sql_tests(
    *, test_cases: tuple[SqlTestCase, ...], client: AdapterConnection
) -> tuple[SqlTestExecutionResult, ...]: ...
def execute_doctor(*, request: DoctorRequest, client: AdapterConnection) -> DoctorResult: ...
def execute_repair_active_view(
    *, request: RepairActiveViewRequest, client: AdapterConnection
) -> RepairActiveViewResult: ...
def execute_reconcile(
    *,
    client: AdapterConnection,
    target_database: str,
    metadata_database: str,
    desired_state: DesiredState,
    actual_state: ActualState,
    selected_model_keys: frozenset[ObjectKey],
    apply: bool = False,
) -> ReconcilePreview | ReconcileResult: ...
def execute_direct_reconcile(
    *,
    client: AdapterConnection,
    target_database: str,
    metadata_database: str,
    desired_state: DesiredState,
    catalog: CatalogSnapshot,
    models: tuple[CompiledModel, ...],
    selected_model_keys: frozenset[ObjectKey],
    tool_version: str,
    apply: bool = False,
) -> ReconcilePreview | ReconcileResult: ...
def execute_deployment_diff(
    *, request: DeploymentDiffRequest, client: AdapterConnection
) -> DeploymentDiffResult: ...
def load_deployments(
    *, client: AdapterConnection, metadata_database: str, default_database: str
) -> DeploymentInventory: ...
def load_deployment(
    *,
    deployment_id: str,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
) -> DeploymentSummary: ...
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
def initialize_observability(*, connection: AdapterConnection, database: str) -> None: ...
def publish_manifest(
    *, connection: AdapterConnection, database: str, manifest: AdapterManifest
) -> None: ...
def run_compile(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    target_dir: Path | None = None,
) -> int: ...
def run_destruction(
    *,
    options: DestructionCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
def render_destruction_plan(plan: DestructionPlan) -> str: ...
def read_destruction_challenges(plan: DestructionPlan) -> tuple[str, ...]: ...
def run_audit(
    *,
    pipelines_root: Path,
    project_dir: Path,
    database: str,
    selectors: tuple[str, ...],
    json_output: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    force: bool = False,
) -> int: ...
def select_loaded_sql_audits(
    *,
    loaded_audits: tuple[LoadedSqlAudit, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selectors: tuple[str, ...],
) -> tuple[LoadedSqlAudit, ...]: ...
def run_test(
    *,
    pipelines_root: Path,
    project_dir: Path | None,
    selectors: tuple[str, ...],
    paths: tuple[Path, ...],
    verbose: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    target_dir: Path | None = None,
    database: str = "",
) -> int: ...
def select_loaded_sql_tests(
    *,
    loaded_tests: tuple[LoadedSqlTest, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selectors: tuple[str, ...],
    paths: tuple[Path, ...],
    project_dir: Path,
) -> tuple[LoadedSqlTest, ...]: ...
def render_sql_test_results(
    *, results: tuple[SqlTestExecutionResult, ...], project_dir: Path, verbose: bool
) -> str: ...
def write_test_runtime_target(
    *,
    target_dir: Path,
    test_cases: tuple[SqlTestCase, ...],
    results: tuple[SqlTestExecutionResult, ...],
) -> None: ...
def static_audit_path(*, audit: LoadedSqlAudit, project_dir: Path) -> str: ...
def static_source_resource_path(*, source_name: str, resource_name: str) -> str: ...
def static_test_path(*, test_case: SqlTestCase) -> str: ...
def runtime_test_path(*, test_case: SqlTestCase) -> Path: ...
def stage_static_compile_artifacts(
    *,
    staging_root: Path,
    compiled_files: list[tuple[str, str]],
    manifest_json: str,
    dag_json: str,
) -> None: ...
def validate_unique_artifact_paths(paths: list[str]) -> None: ...
def run_build(
    *,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
def run_plan(
    *,
    options: PlanCommandOptions,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
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

class DevApplication:
    @property
    def state(self) -> DevServerState: ...
    def request(
        self,
        method: str,
        path: str,
        *,
        query_string: str = "",
        headers: list[tuple[str, str]] = ...,
        body: bytes = b"",
        client: str | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]: ...
    def routes(self) -> list[tuple[list[str], str]]: ...
    def mount_static_assets(self, assets_root: Path) -> None: ...
    def startup(self) -> None: ...
    def shutdown(self) -> None: ...
    def serve(self, host: str, port: int) -> None: ...

def create_dev_app(
    *,
    state: DevServerState,
    connection: AdapterConnection | None = None,
    observation_connection: AdapterConnection | None = None,
    database: str | None = None,
    project_dir: Path | None = None,
    reporter: DevServerReporter | None = None,
    execution_context: DevExecutionContext | None = None,
    auth_settings: AuthSettings | None = None,
    control_store: ControlStore | None = None,
) -> DevApplication: ...
def static_assets_root() -> Path: ...
def static_assets_present(*, assets_root: Path) -> bool: ...
def startup_lines(
    *,
    style: CliStyle,
    outcome: CompileOutcome,
    project_dir: Path,
    database: str | None,
    warehouse_connected: bool = True,
    host: str,
    port: int,
    tool_version: str,
) -> tuple[str, ...]: ...
def reload_summary(*, outcome: CompileOutcome) -> tuple[str, ActivityTone, str]: ...
def activity_line(
    *,
    style: CliStyle,
    timestamp: str,
    category: str,
    status: str,
    tone: ActivityTone,
    detail: str,
) -> str: ...

@final
class DevTerminalReporter:
    """Prints the startup banner and one aligned activity line per server event."""

    def __init__(self, *, style: CliStyle) -> None: ...
    def report_startup(
        self,
        *,
        outcome: CompileOutcome,
        project_dir: Path,
        database: str | None,
        warehouse_connected: bool,
        host: str,
        port: int,
    ) -> None: ...
    def report_reload(self, *, outcome: CompileOutcome) -> None: ...
    def report_activity(
        self, *, category: str, status: str, tone: ActivityTone, detail: str
    ) -> None: ...
    def report_shutdown(self) -> None: ...

def run_dev(
    *,
    options: DevCommandOptions,
    client: AdapterConnection | None,
    observation_client: AdapterConnection | None = None,
    connection_factory: Callable[[], AdapterConnection] | None = None,
    observation_connection_factory: Callable[[], AdapterConnection] | None = None,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
def run_dev_server(
    *,
    run_compile: Callable[[], CompileAnalysis],
    connection: AdapterConnection | None,
    observation_connection: AdapterConnection | None,
    database: str | None,
    project_dir: Path,
    host: str,
    port: int,
    reporter: DevServerReporter,
    execution_context: DevExecutionContext | None = None,
    auth_settings: AuthSettings | None = None,
) -> int: ...
def build_sensors_payload(
    *,
    analysis: CompileAnalysis,
    repository: SensorStateRepository | None,
    health: dict[str, object],
) -> dict[str, object]: ...
def build_sensor_ticks_payload(
    *,
    repository: SensorStateRepository | None,
    sensor_name: str,
    limit: int,
    after: str | None = None,
    before: str | None = None,
) -> dict[str, object]: ...
def build_dead_letters_payload(
    *, repository: SensorStateRepository | None
) -> dict[str, object]: ...
def build_deployments_payload(
    *, connection: AdapterConnection, database: str, metadata_database: str
) -> dict[str, object]: ...
def build_deployment_detail_payload(
    *, connection: AdapterConnection, database: str, metadata_database: str, deployment_id: str
) -> dict[str, object] | None: ...
def build_definitions_payload(
    *, analysis: CompileAnalysis, version_key: str
) -> dict[str, object]: ...
def build_mode_aware_plan_payload(
    *,
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
    analysis: CompileAnalysis,
    selectors: tuple[str, ...],
    planned_at: str,
    replay_row_counts: dict[str, int | None],
    command: str,
) -> dict[str, object]: ...
def count_replay_rows(
    *, connection: AdapterConnection, database: str, plan: DirectPlan, start_time: str | None
) -> dict[str, int | None]: ...
def replay_time_column(*, boundary_mode: str, columns: AdapterReplayColumns) -> str: ...
def build_replay_count_query(
    *, database: str, relation_name: str, time_column: str, start_time: str | None
) -> str: ...
def build_activity_capabilities_query() -> str: ...
def build_query_views_activity_query(*, database: str, window_seconds: int) -> str: ...
def build_part_log_activity_query(*, database: str, window_seconds: int) -> str: ...
def build_parts_activity_query(*, database: str) -> str: ...
def read_model_activity(
    *,
    connection: AdapterConnection,
    database: str,
    relation_names: tuple[str, ...],
    captured_at: str,
    active_bindings: tuple[tuple[str, str], ...] = (),
    window_seconds: int = 120,
) -> dict[str, dict[str, object]]: ...
def model_drift_payload(
    *, analysis: CompileAnalysis, model: CompiledModel, catalog: CatalogSnapshot
) -> dict[str, object]: ...
def build_relation_stats_query(*, database: str) -> str: ...
def build_parts_query(*, database: str) -> str: ...
def build_extents_query(*, database: str, relation_names: tuple[str, ...]) -> str: ...
def build_throughputs_query(*, database: str, windows: tuple[tuple[str, int, int], ...]) -> str: ...
def build_partitions_query(*, database: str, relation_names: tuple[str, ...]) -> str: ...
def choose_throughput_window(*, newest_age_seconds: float | None) -> tuple[int, int]: ...
def apply_bound_relation_stats(
    *, stats: dict[str, dict[str, int]], bindings: tuple[tuple[str, str], ...]
) -> dict[str, dict[str, int]]: ...
def build_state_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    kafka_lag_reader: KafkaLagSource | None = None,
    warehouse_health_reader: WarehouseHealthReader | None = None,
) -> dict[str, object]: ...
def build_topics_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection | None,
    database: str | None,
    topic_reader: KafkaTopicSource,
    kafka_lag_reader: KafkaLagSource,
) -> dict[str, object]: ...
def derive_run_status(
    *,
    terminal_outcome: str | None,
    completed_event_outcome: str | None,
    last_signal_at: datetime,
    warehouse_now: datetime,
    presumed_failed_after_seconds: int = 600,
) -> RunPresentationStatus: ...
def derive_run_duration_ms(
    *, started_at: str, completed_at: str | None, warehouse_now: datetime
) -> int: ...
def terminal_audit_summary(
    *, command: str, mode: str, summary_json: object
) -> dict[str, int] | None: ...
def assemble_runs(
    *,
    terminal_by_id: dict[str, dict[str, object]],
    streams: dict[str, list[dict[str, object]]],
    warehouse_now: datetime,
    limit: int | None = None,
    presumed_failed_after_seconds: int = 600,
) -> list[dict[str, object]]: ...
def statement_progress(
    *, connection: AdapterConnection, events: list[dict[str, object]], observed_at: str
) -> dict[str, object] | None: ...
def read_runs(
    *,
    connection: AdapterConnection,
    database: str,
    limit: int | None = 100,
    presumed_failed_after_seconds: int = 600,
) -> list[dict[str, object]]: ...
def read_active_runs(
    *, connection: AdapterConnection, database: str, presumed_failed_after_seconds: int = 600
) -> list[dict[str, object]]: ...
def read_run_events(
    *,
    connection: AdapterConnection,
    database: str,
    invocation_id: str,
    after: int = 0,
    presumed_failed_after_seconds: int = 600,
) -> dict[str, object]: ...
def read_run_statement(
    *, connection: AdapterConnection, database: str, invocation_id: str, statement_sequence: int
) -> dict[str, object]: ...
def read_terminal_run(
    *, connection: AdapterConnection, database: str, invocation_id: str
) -> dict[str, object] | None: ...
def read_destruction_recovery_run(
    *, connection: AdapterConnection, database: str, invocation_id: str
) -> dict[str, object] | None: ...
def read_latest_direct_build_materialization(
    *, connection: AdapterConnection, database: str, project_identity: str
) -> str | None: ...
def read_latest_applied_direct_build_at(
    *, connection: AdapterConnection, database: str, project_identity: str
) -> str | None: ...
def decode_replay_offset_progress_request(
    value: object,
) -> AdapterReplayOffsetProgressRequest | None: ...
def calculate_replay_offset_progress(
    *,
    request: AdapterReplayOffsetProgressRequest,
    frontiers: tuple[AdapterReplayOffsetFrontier, ...],
    elapsed_seconds: float,
    completed: bool = False,
) -> ReplayOffsetProgress | None: ...

@final
class MessagesQueryDocument:
    """One validated message browsing request compiled to SQL fragments."""

def parse_messages_document(request: MessagesQueryRequest) -> MessagesQueryDocument: ...
def build_messages_sql(
    *,
    database: str,
    relation_name: str,
    document: MessagesQueryDocument,
    window_seconds: int | None,
) -> str: ...
def read_source_messages(
    *,
    connection: AdapterConnection,
    database: str,
    relation_name: str,
    request: MessagesQueryRequest,
) -> dict[str, object]: ...
def build_record_sql(*, database: str, relation_name: str, partition: int, offset: int) -> str: ...
def read_source_message_record(
    *,
    connection: AdapterConnection,
    database: str,
    relation_name: str,
    partition: int,
    offset: int,
) -> dict[str, object] | None: ...
def build_facets_sql(
    *,
    database: str,
    relation_name: str,
    document: MessagesQueryDocument,
    facet_path: tuple[str | int, ...],
) -> tuple[str, str]: ...
def read_source_message_facets(
    *,
    connection: AdapterConnection,
    database: str,
    relation_name: str,
    request: MessageFacetsRequest,
) -> dict[str, object]: ...
def ensure_header_columns(
    *, connection: AdapterConnection, database: str, relation_name: str
) -> None: ...

class KafkaLagSource(Protocol):
    """Cached consumer-group lag for one managed Kafka source."""

    def read(self, *, kafka: KafkaSettings, database: str) -> KafkaLagSnapshot | None: ...

class KafkaTopicSource(Protocol):
    """Cached topic inventory for one broker configuration."""

    def read(self, *, kafka: KafkaSettings) -> KafkaTopicsSnapshot | None: ...

def build_kafka_client_config(*, kafka: KafkaSettings) -> dict[str, object]: ...
def kafka_client_key(*, kafka: KafkaSettings) -> tuple[str, tuple[tuple[str, str], ...]]: ...
def build_kafka_lag_snapshot(
    *,
    partition_ids: frozenset[int],
    committed_offsets: Mapping[int, int],
    end_offsets: Mapping[int, int],
) -> KafkaLagSnapshot: ...
def build_kafka_topics_snapshot(*, metadata: object) -> KafkaTopicsSnapshot: ...

@final
class KafkaLagReader:
    """Return cached lag immediately and refresh stale values on a worker thread."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        collector: Callable[..., object] | None = None,
    ) -> None: ...
    def read(self, *, kafka: KafkaSettings, database: str) -> KafkaLagSnapshot | None: ...
    def close(self) -> None: ...

@final
class KafkaTopicReader:
    """Return cached topic inventories immediately and refresh stale entries."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        collector: Callable[..., object] | None = None,
    ) -> None: ...
    def read(self, *, kafka: KafkaSettings) -> KafkaTopicsSnapshot | None: ...
    def close(self) -> None: ...

@final
class WarehouseHealthReader:
    """Preserve the last usable snapshot while marking failed refreshes stale."""

    def __init__(
        self, *, clock: Callable[[], float] | None = None, cache_seconds: float | None = None
    ) -> None: ...
    def read(
        self,
        *,
        connection: AdapterConnection,
        database: str,
        measured_at: str,
        managed_source_names: tuple[str, ...] = (),
        include_dead_letters: bool = False,
    ) -> AdapterWarehouseHealth: ...

@final
class OverlayReader:
    """Own one private connection so overlay refreshes never take the shared query lock."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[], AdapterConnection],
        kafka_lag_reader: KafkaLagSource,
    ) -> None: ...
    def close(self) -> None: ...
    def read(self, *, analysis: CompileAnalysis, database: str) -> dict[str, object]: ...

def build_compile_outcome(*, run_compile: Callable[[], CompileAnalysis]) -> CompileOutcome: ...
def describe_compile_error(*, error: Exception) -> CompileErrorInfo: ...
def build_status_payload(
    *, outcome: CompileOutcome, warehouse_status: dict[str, object]
) -> dict[str, object]: ...

@final
class StateSnapshot:
    """Hold one warehouse overlay so requests never wait on per-relation reads."""

    def __init__(
        self,
        *,
        build: Callable[[], dict[str, object]],
        refresh_seconds: float | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None: ...
    def start(self) -> None: ...
    def close(self) -> None: ...
    def current(self) -> dict[str, object]: ...
    def held(self) -> dict[str, object] | None: ...
    def refresh(self) -> dict[str, object]: ...
    def invalidate(self) -> None: ...

@final
class DevServerState:
    """One long-running server's source of truth between requests."""

    def __init__(self, *, run_compile: Callable[[], CompileAnalysis]) -> None: ...
    @property
    def snapshot(self) -> StateSnapshot: ...
    def attach_snapshot(self, snapshot: StateSnapshot) -> None: ...
    @property
    def query_lock(self) -> threading.Lock: ...
    def reload(self) -> CompileOutcome: ...
    def reload_guarded(self, *, guard: CompileAuthorizationGuard) -> CompileOutcome: ...
    def current(self) -> CompileOutcome: ...
    def current_analysis(self) -> CompileAnalysis: ...
    def current_servable_outcome(self) -> CompileOutcome: ...

@final
class SilentDevServerReporter:
    """A DevServerReporter that says nothing; the default for embedded use."""

    def report_startup(
        self,
        *,
        outcome: CompileOutcome,
        project_dir: Path,
        database: str | None,
        warehouse_connected: bool,
        host: str,
        port: int,
    ) -> None: ...
    def report_reload(self, *, outcome: CompileOutcome) -> None: ...
    def report_activity(
        self, *, category: str, status: str, tone: ActivityTone, detail: str
    ) -> None: ...
    def report_shutdown(self) -> None: ...

def scheduler_enabled(analysis: CompileAnalysis) -> bool: ...
def sensors_enabled(analysis: CompileAnalysis) -> bool: ...
def build_audit_scheduler_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    project_dir: Path,
) -> dict[str, object]: ...
def execute_due_audits(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    observation_connection: AdapterConnection | None = None,
    database: str,
    project_dir: Path,
    due: tuple[dict[str, object], ...],
) -> int: ...
def run_one_audit(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    name: str,
    project_dir: Path,
    database: str,
) -> dict[str, object]: ...
def run_audit_batch(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    names: tuple[str, ...],
    project_dir: Path,
    database: str,
) -> list[dict[str, object]]: ...
def run_one_test(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    name: str,
    project_dir: Path,
    database: str,
) -> dict[str, object]: ...
def build_checks_status_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    project_dir: Path,
) -> list[dict[str, object]]: ...
def promotion_executed_logical_ids(
    *, connection: AdapterConnection, metadata_database: str, deployment_id: str
) -> tuple[str, ...]: ...
def run_dev_deployment_promotion(
    *,
    connection: AdapterConnection,
    database: str,
    metadata_database: str,
    deployment_id: str,
    project_dir: Path,
) -> dict[str, object]: ...
def run_dev_deployment_cleanup(
    *,
    connection: AdapterConnection,
    database: str,
    metadata_database: str,
    retention_days: int,
    project_dir: Path,
) -> dict[str, object]: ...
def build_deployment_diff_payload(
    *,
    connection: AdapterConnection,
    database: str,
    metadata_database: str,
    comparison: str,
) -> dict[str, object]: ...
def deployment_diff_payload(*, result: DeploymentDiffResult) -> dict[str, object]: ...
def build_invocation(
    *,
    selectors: tuple[str, ...],
    start_time: str | None,
    execution_context: DevExecutionContext | None,
    changed: bool = False,
    include_missing_upstream: bool = False,
    deployment_id: str | None = None,
    confirmations: tuple[str, ...] | None = None,
) -> tuple[list[str], str]: ...
def build_child_environment(
    *,
    execution_context: DevExecutionContext | None,
    display_command: str | None = None,
    invocation_id: str | None = None,
    expected_write_scope: frozenset[str] | None = None,
    expected_read_scope: frozenset[str] | None = None,
    expected_pipeline_scope: frozenset[str] | None = None,
) -> dict[str, str]: ...

@final
class BuildProcessManager:
    """Owns at most one running `sqt build` child and its live event feed."""

    def __init__(
        self,
        *,
        reporter: DevServerReporter,
        execution_context: DevExecutionContext | None = None,
        query_canceller: Callable[[str], AdapterQueryCancellation] | None = None,
        cancellation_reporter: Callable[[BuildCancellationOutcome], None] | None = None,
    ) -> None: ...
    def start(
        self,
        *,
        project_dir: Path,
        selectors: tuple[str, ...],
        start_time: str | None,
        changed: bool = False,
        include_missing_upstream: bool = False,
        deployment_id: str | None = None,
        confirmations: tuple[str, ...] | None = None,
        expected_write_scope: frozenset[str] | None = None,
        expected_read_scope: frozenset[str] | None = None,
        expected_pipeline_scope: frozenset[str] | None = None,
    ) -> dict[str, object]: ...
    def feed(self, *, after: int) -> dict[str, object]: ...
    def cancel(self, *, invocation_id: str) -> dict[str, object]: ...
    def kill(self, *, invocation_id: str) -> dict[str, object]: ...
    def close(self) -> None: ...

@final
class AuditScheduler:
    """Poll current compile and metadata state, then run one due batch at a time."""

    def __init__(
        self,
        *,
        state: DevServerState,
        warehouse: WarehouseRuntime | None = None,
        connection: AdapterConnection | None = None,
        observation_connection: AdapterConnection | None = None,
        database: str | None,
        project_dir: Path,
        builds: BuildProcessManager,
        presumed_failed_after_seconds: int | None = None,
    ) -> None: ...
    def start(self) -> None: ...
    def close(self) -> None: ...
    def tick(self) -> int: ...
    def health(self) -> dict[str, object]: ...

@final
class ReadConnection:
    """The context manager WarehouseRuntime.read_connection returns."""

    def __enter__(self) -> AdapterConnection | None: ...
    def __exit__(self, *exc_info: object) -> bool: ...

@final
class WarehouseRuntime:
    """Hold primary and observation connections, reconnecting without stopping the UI."""

    def __init__(
        self,
        *,
        connection: AdapterConnection | None,
        observation_connection: AdapterConnection | None,
        connection_factory: Callable[[], AdapterConnection] | None,
        observation_connection_factory: Callable[[], AdapterConnection] | None,
        database: str | None,
        query_lock: threading.Lock | threading.RLock | None = None,
    ) -> None: ...
    @property
    def connection(self) -> AdapterConnection | None: ...
    @property
    def observation_connection(self) -> AdapterConnection | None: ...
    def start(self) -> None: ...
    def close(self) -> None: ...
    def connect_now(self) -> bool: ...
    def request_health_check(self) -> None: ...
    def status(self) -> dict[str, object]: ...
    def read_connection(self) -> ReadConnection: ...

@final
class BuildCancellationCoordinator:
    """Cancel an exact workflow query and retain the confirmed outcome."""

    def __init__(self, *, warehouse: WarehouseRuntime, database: str | None) -> None: ...
    def cancel_query(self, query_id: str) -> AdapterQueryCancellation: ...
    def record_outcome(self, outcome: BuildCancellationOutcome) -> None: ...

@final
class SensorScheduler:
    """Poll current compile state and dispatch running sensors one pass at a time."""

    def __init__(
        self, *, state: DevServerState, warehouse: WarehouseRuntime, database: str | None
    ) -> None: ...
    @property
    def repository(self) -> SensorStateRepository | None: ...
    def build_dispatcher(self, *, analysis: CompileAnalysis) -> SensorDispatcher | None: ...
    def start(self) -> None: ...
    def initialize_event_checkpoints(self) -> None: ...
    def close(self) -> None: ...
    def tick(self) -> SensorDispatchSummary | None: ...
    def health(self) -> dict[str, object]: ...

@final
class CliStyle:
    """Semantic CLI styling facade used by human-output formatters."""

    use_color: bool
    def __init__(self, *, use_color: bool) -> None: ...
    def title(self, text: str) -> str: ...
    def section(self, text: str) -> str: ...
    def subsection(self, text: str) -> str: ...
    def label(self, text: str) -> str: ...
    def label_value(self, *, label: str, value: object) -> str: ...
    def object_name(self, *, text: str, assessment: str | None = None) -> str: ...
    def warning(self, text: str) -> str: ...
    def assessment(self, text: str) -> str: ...
    def assessment_value(self, *, text: str, assessment: str, bold: bool = False) -> str: ...
    def diff_lines(self, diff_lines: Iterable[str]) -> list[str]: ...
    def muted(self, text: str) -> str: ...
    def muted_strong(self, text: str) -> str: ...
    def passed(self, text: str) -> str: ...
    def failed(self, text: str) -> str: ...
    def outcome(self, *, text: str, passed: bool) -> str: ...

def cli_style() -> CliStyle: ...
def publish_build_workflow(
    *, target_dir: Path, workflow: BuildWorkflow, execution_json: str | None = None
) -> PublishedBuildWorkflow: ...
def publish_plan_workflow(
    *, target_dir: Path, workflow: BuildWorkflow, is_template: bool = False
) -> None: ...
def render_deployment_audit_result(
    *,
    result: DeploymentAuditResult,
    database: str,
    json_output: bool,
    project_dir: Path | None = None,
) -> str: ...
def render_ambiguous_deployment_message(
    *,
    command_name: str,
    database: str,
    root_names: Sequence[str],
    candidates: Iterable[AuditDeploymentCandidate],
) -> str: ...
def render_promotion_result(*, result: PublishResult, database: str, json_output: bool) -> str: ...
def run_discover(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
def run_doctor(*, database: str, client: AdapterConnection) -> int: ...
def run_repair_active_view(
    *, database: str, table: str, deployment_id: str, client: AdapterConnection
) -> int: ...
def run_janitor(
    *,
    database: str,
    retention_days: int,
    minimum_rollback_deployments: int,
    apply: bool,
    json_output: bool,
    client: AdapterConnection,
) -> int: ...
def run_deployment_list(
    *, database: str, metadata_database: str | None, json_output: bool, client: AdapterConnection
) -> int: ...
def run_deployment_show(
    *,
    deployment_id: str,
    database: str,
    metadata_database: str | None,
    json_output: bool,
    client: AdapterConnection,
) -> int: ...
def run_deployment_diff(
    *,
    database: str,
    metadata_database: str | None,
    comparison: str,
    json_output: bool,
    client: AdapterConnection,
) -> int: ...
def run_deployment_rollback(
    *,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    previous: bool,
    auto_approve: bool,
    json_output: bool,
    client: AdapterConnection,
) -> int: ...
def run_deployment_audit(
    *,
    pipelines_root: Path | None,
    project_dir: Path | None,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    json_output: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
def run_deployment_promotion(
    *,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    json_output: bool,
    client: AdapterConnection,
) -> int: ...
def run_reconcile(
    *,
    pipelines_root: Path,
    database: str | None,
    metadata_database: str | None,
    selectors: tuple[str, ...],
    json_output: bool,
    apply: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int: ...
