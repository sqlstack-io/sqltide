import argparse
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, Self, TextIO, final
from uuid import UUID

from sqltide.adapter.classes.adapter import Adapter
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.types import (
    AdapterModelRealizer,
    AdapterModelRelationNamer,
    AdapterOptionalStateStatus,
    AdapterReplayBoundaryMode,
    AdapterReplayLowerBoundMode,
    AdapterReplaySeedMode,
    AdapterResourceRenderer,
    AdapterSetDifferenceComparisonRenderer,
    AdapterSourceRealizer,
    AdapterWarehouseHealthAvailability,
    AdapterWarehouseHealthStatus,
)
from sqltide.adapters.clickhouse.classes.clickhouse_connection import ClickHouseConnection
from sqltide.adapters.clickhouse.types import RawClickHouseClient
from sqltide.compiler.macros.models import MacroContext
from sqltide.compiler.macros.types import MacroFunction
from sqltide.dev_server.models import (
    MessageFacetsRequest,
    MessagesQueryRequest,
)
from sqltide.dev_server.types import CompileAuthorizationGuard, DevServerReporter
from sqltide.events.models import (
    AuditCompleted,
    RunCompleted,
)
from sqltide.events.types import SensorEvent
from sqltide.executor.destruction.types import (
    DestructionClock,
    DestructionPlanningConnection,
    DestructionPlanStore,
)
from sqltide.executor.kafka_admin.models import ConsumerGroupOffsetReset
from sqltide.executor.kafka_admin.types import KafkaAdminClientFactory
from sqltide.executor.workflow.types import (
    NativeStatement,
    StatementIntent,
    WorkflowEventEmitter,
    WorkflowMode,
    WorkflowPhase,
)
from sqltide.provider.models import DiscoveredProvider
from sqltide.providers import Provider
from sqltide.sensors.models import (
    EventSensorDeclaration,
    PollingSensorDeclaration,
    SensorRetryPolicy,
)
from sqltide.sensors.types import (
    SensorDeclaration,
    SensorFunction,
    SensorStepStore,
)

# Compiler analysis shapes: SQL analysis, graph, Rules, quality, audit and test discovery.

class RefType(StrEnum):
    REFERENCE = "reference"
    MUTABLE = "mutable"

class SqlRelationType(StrEnum):
    SOURCE = "source"
    REF = "ref"

class SqlQueryShape(StrEnum):
    SELECT = "select"

class SqlStorageExpressionKind(StrEnum):
    ORDER_BY = "order_by"
    PARTITION_BY = "partition_by"
    TTL = "ttl"

class DependencyEdgeType(StrEnum):
    DRIVING_INPUT = "driving_input"
    REFERENCE = "reference"
    MUTABLE_REFERENCE = "mutable_reference"

class GraphTraversalDirection(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"

class QualityNodeKind(StrEnum):
    AUDIT = "audit"
    TEST = "test"

class AuditAttachmentKind(StrEnum):
    STANDALONE = "standalone"
    MODEL = "model"
    COLUMN = "column"

class SqlTestMode(StrEnum):
    MODEL = "model"
    MACRO = "macro"

class DiagnosticPhase(StrEnum):
    DISCOVERY = "discovery"
    COMPILATION = "compilation"
    GRAPH = "graph"
    REALIZATION = "realization"
    RUNTIME = "runtime"

class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"

@final
@dataclass(frozen=True)
class SqlSourceSpan:
    start: int
    end: int
    line: int
    column: int
    end_line: int
    end_column: int

@final
@dataclass(frozen=True)
class SqlReference:
    name: str
    relation_type: SqlRelationType | str
    span: SqlSourceSpan
    ref_type: RefType | None = None

@final
@dataclass(frozen=True)
class SqlOutputColumn:
    name: str
    type: str

@final
@dataclass(frozen=True)
class SqlProjection:
    index: int
    output: SqlOutputColumn
    span: SqlSourceSpan

@final
@dataclass(frozen=True)
class SqlStorageExpression:
    kind: SqlStorageExpressionKind | str
    sql: str
    referenced_column_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class SqlAggregateFacts:
    has_group_by: bool
    function_names: tuple[str, ...]
    engine_name: str
    engine_has_aggregate_semantics: bool
    @property
    def has_semantics(self) -> bool: ...

@final
@dataclass(frozen=True)
class SqlModelAnalysis:
    authored_sql: str
    shape: SqlQueryShape | str
    projections: tuple[SqlProjection, ...]
    references: tuple[SqlReference, ...]
    storage_expressions: tuple[SqlStorageExpression, ...]
    aggregate_facts: SqlAggregateFacts
    @property
    def output_columns(self) -> tuple[SqlOutputColumn, ...]: ...

@final
@dataclass(frozen=True)
class SqlResolvedQuery:
    canonical_sql: str
    database_template: str

@final
@dataclass(frozen=True)
class SqlRelationRewrite:
    source_name: str
    target_relation: str
    source_databases: tuple[str | None, ...] | None = None
    preserve_source_database: bool = False

@final
@dataclass(frozen=True)
class SqlNamedQuery:
    name: str
    query: str

@final
@dataclass(frozen=True)
class SqlQueryRewriteResult:
    query: str
    has_aggregate_semantics: bool

@final
@dataclass(frozen=True)
class SqlRelationIdentity:
    database: str | None
    name: str

@final
@dataclass(frozen=True)
class SqlCatalogAnalysis:
    canonical_sql: str
    query_sql: str | None
    source_relations: tuple[SqlRelationIdentity, ...]
    direct_source: SqlRelationIdentity | None
    target_relation: SqlRelationIdentity | None
    engine: str | None
    ttl: str | None
    settings: tuple[tuple[str, str], ...]
    @property
    def first_source(self) -> SqlRelationIdentity | None: ...

@final
@dataclass(frozen=True)
class DependencyEdge:
    upstream_key: LogicalResourceKey
    downstream_key: LogicalResourceKey
    edge_type: DependencyEdgeType

@final
@dataclass(frozen=True)
class ProjectGraph:
    project: CompiledProject
    upstream_edges_by_key: Mapping[LogicalResourceKey, tuple[DependencyEdge, ...]]
    downstream_edges_by_key: Mapping[LogicalResourceKey, tuple[DependencyEdge, ...]]
    ordered_keys: tuple[LogicalResourceKey, ...]

@final
@dataclass(frozen=True)
class FormattedSource:
    path: Path
    original: str
    formatted: str
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    error: str | None = None

@final
@dataclass(frozen=True)
class QualityNodeIdentity:
    node_kind: str
    node_name: str
    binding_key: str
    definition_fingerprint: str
    execution_fingerprint: str

@final
@dataclass(frozen=True)
class LoadedSqlAudit:
    file_path: Path
    query: str
    referenced_model_names: tuple[str, ...]
    severity: str = "error"
    description: str | None = None
    name: str | None = None
    audit_index: int = 1
    generic_definition_name: str | None = None
    attachment_kind: AuditAttachmentKind | str = AuditAttachmentKind.STANDALONE
    attached_model: str | None = None
    attached_column: str | None = None
    quality_identity: QualityNodeIdentity | None = None
    severity_is_explicit: bool = False
    cadence_seconds: int | None = None
    warmup_seconds: int = 0
    scheduled: bool = False
    cadence_seconds_override: int | None = None
    warmup_seconds_override: int | None = None
    scheduled_override: bool | None = None

@final
@dataclass(frozen=True)
class LoadedGenericSqlAuditDefinition:
    file_path: Path
    query: str
    raw_parameter_names: tuple[str, ...]
    quoted_parameter_names: tuple[str, ...]
    name: str

@final
@dataclass(frozen=True)
class LoadedGenericSqlAuditInstance:
    file_path: Path
    definition_name: str
    arguments: dict[str, object]
    name: str
    severity: str = "error"
    description: str | None = None
    severity_is_explicit: bool = False
    cadence_seconds_override: int | None = None
    warmup_seconds_override: int | None = None
    scheduled_override: bool | None = None

@final
@dataclass(frozen=True)
class SqlTestMock:
    cte_name: str
    name: str
    relation_type: DiscoverySqlRelationType | str
    query: str

@final
@dataclass(frozen=True)
class SqlTestCte:
    name: str
    query: str

@final
@dataclass(frozen=True)
class SqlTestModelPayload:
    mocks: tuple[SqlTestMock, ...]
    expected_targets: tuple[SqlTestCte, ...]
    assertions: tuple[SqlTestCte, ...]
    assertion_reference_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class SqlTestMacroPayload:
    actual: SqlTestCte
    expected: SqlTestCte

@final
@dataclass(frozen=True)
class LoadedSqlTest:
    file_path: Path
    mode: SqlTestMode
    authored_ctes: tuple[SqlTestCte, ...]
    payload: SqlTestModelPayload | SqlTestMacroPayload
    name: str | None = None
    test_index: int = 1

@final
@dataclass(frozen=True)
class SqlTestChainStep:
    target_model_name: str
    expected_column_names: tuple[str, ...]
    ctes: tuple[tuple[str, str], ...]
    actual_query: str
    expected_query: str

@final
@dataclass(frozen=True)
class SqlTestAssertionStep:
    name: str
    column_names: tuple[str, ...]
    ctes: tuple[tuple[str, str], ...]
    query: str

@final
@dataclass(frozen=True)
class SqlTestCase:
    file_path: Path
    query: str
    target_cases: tuple[SqlTestChainStep, ...]
    assertion_cases: tuple[SqlTestAssertionStep, ...] = ()
    warnings: tuple[str, ...] = ()
    name: str | None = None
    test_index: int = 1
    quality_identity: QualityNodeIdentity | None = None

class SqlAnalysisError(ValueError):
    span: SqlSourceSpan | None
    def __init__(self, message: str, *, span: SqlSourceSpan | None = None) -> None: ...

class SqlMissingWithClauseError(SqlAnalysisError):
    context: str
    def __init__(self, *, context: str, span: SqlSourceSpan) -> None: ...

class SqlStatementCountError(SqlAnalysisError):
    statement_count: int
    def __init__(self, statement_count: int) -> None: ...

class SqlQueryShapeError(SqlAnalysisError):
    statement_type: str
    statement_sql: str
    is_set_operation: bool
    def __init__(
        self, *, statement_type: str, statement_sql: str, is_set_operation: bool
    ) -> None: ...

class SqlStarProjectionError(SqlAnalysisError):
    column_index: int
    def __init__(self, *, column_index: int, span: SqlSourceSpan) -> None: ...

class SqlUntypedProjectionError(SqlAnalysisError):
    column_index: int
    projection_sql: str
    def __init__(self, *, column_index: int, projection_sql: str, span: SqlSourceSpan) -> None: ...

class SqlDuplicateAliasError(SqlAnalysisError):
    alias: str
    def __init__(self, *, alias: str, span: SqlSourceSpan) -> None: ...

class SqlStorageReferenceError(SqlAnalysisError):
    kind: str
    expression: str
    unknown_column_names: tuple[str, ...]
    available_column_names: tuple[str, ...]
    def __init__(
        self,
        *,
        kind: str,
        expression: str,
        unknown_column_names: tuple[str, ...],
        available_column_names: tuple[str, ...],
    ) -> None: ...

class GraphInputError(ValueError):
    location: SourceLocation | None
    diagnostic: CompilerDiagnostic | None
    def __init__(self, message: str, *, location: SourceLocation | None = None) -> None: ...

class SqlRulesError(ValueError): ...

class RulesError(ValueError):
    findings: tuple[RuleFinding, ...]
    def __init__(self, *, findings: tuple[RuleFinding, ...], defaults: bool = False) -> None: ...

class QualityIdentityError(ValueError): ...
class SqlAuditParseError(ValueError): ...
class SqlTestParseError(ValueError): ...
class SqlTestAssemblyError(ValueError): ...

UPSTREAM_SELECTOR_PREFIX: str
SELECTOR_NAMESPACE_SEPARATOR: str
PIPELINE_SELECTOR_NAMESPACE: str
MODEL_SELECTOR_NAMESPACE: str
ALL_DEPENDENCY_EDGE_TYPES: frozenset[DependencyEdgeType]
DRIVING_DEPENDENCY_EDGE_TYPES: frozenset[DependencyEdgeType]

# Compiled project shapes: compile models, macros, the compilation lifecycle and access policy.

class PipelineMode(StrEnum):
    DIRECT = "direct"
    VIRTUAL = "virtual"

class RetentionMissingBehavior(StrEnum):
    ERROR = "error"
    SKIP = "skip"

class KafkaRetentionReference(StrEnum):
    BROKER = "broker"
    LANDED = "landed"

class KafkaRetentionOrigin(StrEnum):
    SOURCE = "source"
    PROJECT = "project"

class ModelReferenceScope(StrEnum):
    PROJECT = "project"
    PIPELINE = "pipeline"

class RowErrorPolicy(StrEnum):
    FAIL = "fail"
    QUARANTINE = "quarantine"

class ReplayAnchorMode(StrEnum):
    AUTO = "auto"
    NEVER = "never"

class ReplayLineageMode(StrEnum):
    OFFSETS = "offsets"
    TIMESTAMP = "timestamp"
    LANDED_AT = "landed_at"
    CURSOR = "cursor"

class SourceKind(StrEnum):
    KAFKA = "kafka"
    STREAM_TABLE = "stream_table"
    POSTGRES = "postgres"

class SourceNameOrigin(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"

class ModelKind(StrEnum):
    TABLE = "table"
    VIEW = "view"

class ReplayBoundaryMode(StrEnum):
    OFFSETS = "offsets"
    TIMESTAMP = "timestamp"
    LANDED_AT = "landed_at"
    CURSOR = "cursor"

class BoundedReplayFallback(StrEnum):
    FULL = "full"
    BOUNDED_WITHOUT_HISTORY = "bounded_without_history"

class DiscoveryRefType(StrEnum):
    REFERENCE = "reference"
    MUTABLE = "mutable"

class DiscoverySqlRelationType(StrEnum):
    SOURCE = "source"
    REF = "ref"

class ReplayOnChangeMode(StrEnum):
    FULL = "full"
    BOUNDED = "bounded"

class SchemaChangeKind(StrEnum):
    BREAKING = "breaking"
    NON_BREAKING = "non_breaking"

class DesiredObjectType(StrEnum):
    KAFKA_TABLE = "kafka_table"
    TABLE = "table"
    MATERIALIZED_VIEW = "materialized_view"
    VIEW = "view"

class LogicalResourceType(StrEnum):
    SOURCE = "source"
    MODEL = "model"

class RetentionOrigin(StrEnum):
    MODEL = "model"
    PIPELINE = "pipeline"
    PROJECT = "project"

class Permission(StrEnum):
    PROJECT_RELOAD = "project.reload"
    SOURCE_MESSAGES_READ = "source.messages.read"
    QUALITY_TEST_RUN = "quality.test.run"
    QUALITY_AUDIT_RUN = "quality.audit.run"
    BUILD_DIRECT_RUN = "build.direct.run"
    DEPLOYMENT_CREATE = "deployment.create"
    BUILD_CANCEL = "build.cancel"
    BUILD_KILL = "build.kill"
    DEPLOYMENT_PROMOTE = "deployment.promote"
    DEPLOYMENT_CLEANUP = "deployment.cleanup"
    PIPELINE_DESTROY = "pipeline.destroy"
    TARGET_RESET = "target.reset"
    AUTOMATION_MANAGE = "automation.manage"
    ACCOUNT_MANAGE = "account.manage"
    ROLE_ASSIGN = "role.assign"

class GrantScope(StrEnum):
    PROJECT = "project"
    TARGET = "target"

@final
@dataclass(frozen=True)
class LoadedMacro:
    name: str
    file_path: Path
    relative_path: Path
    source: str
    definition_line: int
    function: MacroFunction
    description: str | None = None
    @property
    def identity_fingerprint(self) -> str: ...

@final
@dataclass(frozen=True, repr=False)
class MacroRegistry:
    macros: Mapping[str, LoadedMacro] = field(default_factory=dict)
    declarations: Declarations | None = None
    expansions: dict[tuple[Path, str, int, int], tuple[str, list[tuple[int, int, int, int]]]] = (
        field(default_factory=dict, compare=False, repr=False)
    )

@final
@dataclass(frozen=True)
class ObjectKey:
    database: str | None
    object_type: DesiredObjectType | str
    name: str

@final
@dataclass(frozen=True)
class Column:
    name: str
    type: str
    default: str | None = None

@final
@dataclass(frozen=True)
class KafkaSettings:
    broker_list: str
    topic: str
    consumer_group: str
    format: str
    settings: Mapping[str, str] | None = None

@final
@dataclass(frozen=True)
class KafkaTableSpec:
    columns: tuple[Column, ...]
    kafka: KafkaSettings
    naming_macro_fingerprint: str | None = field(default=None, compare=False)

@final
@dataclass(frozen=True)
class TableStorage:
    engine: str
    order_by: tuple[str, ...]
    partition_by: str | None = None
    ttl: str | None = None
    settings: Mapping[str, str] | None = None

@final
@dataclass(frozen=True)
class TableSpec:
    columns: tuple[Column, ...]
    storage: TableStorage

@final
@dataclass(frozen=True)
class MaterializedViewSpec:
    source_table_name: str
    target_table_name: str
    query: str
    database_template: str | None = field(default=None, compare=False)

@final
@dataclass(frozen=True)
class ViewSpec:
    query: str
    database_template: str | None = field(default=None, compare=False)

@final
@dataclass(frozen=True)
class DesiredKafkaTable:
    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: KafkaTableSpec
    @property
    def name(self) -> str: ...
    @property
    def columns(self) -> tuple[Column, ...]: ...
    @property
    def kafka(self) -> KafkaSettings: ...

@final
@dataclass(frozen=True)
class DesiredTable:
    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: TableSpec
    logical_model_name: str | None = field(default=None, compare=False)
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback = BoundedReplayFallback.FULL
    replay_settings: Mapping[str, str] | None = field(default=None, compare=False)
    @property
    def name(self) -> str: ...
    @property
    def columns(self) -> tuple[Column, ...]: ...
    @property
    def engine(self) -> str: ...
    @property
    def order_by(self) -> tuple[str, ...]: ...
    @property
    def partition_by(self) -> str | None: ...
    @property
    def ttl(self) -> str | None: ...
    @property
    def settings(self) -> Mapping[str, str] | None: ...

@final
@dataclass(frozen=True)
class DesiredMaterializedView:
    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: MaterializedViewSpec
    logical_model_name: str | None = field(default=None, compare=False)
    refresh: str | None = field(default=None, compare=False)
    append: bool = field(default=True, compare=False)
    filters_row_errors: bool = field(default=False, compare=False)
    dead_letters: bool = field(default=False, compare=False)
    authored_query: str | None = field(default=None, compare=False)
    authored_database_template: str | None = field(default=None, compare=False)
    @property
    def comparison_spec(self) -> MaterializedViewSpec: ...
    @property
    def name(self) -> str: ...
    @property
    def source_table_name(self) -> str: ...
    @property
    def target_table_name(self) -> str: ...
    @property
    def query(self) -> str: ...
    @property
    def database_template(self) -> str | None: ...

@final
@dataclass(frozen=True)
class DesiredView:
    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: ViewSpec
    logical_model_name: str | None = field(default=None, compare=False)
    @property
    def name(self) -> str: ...
    @property
    def query(self) -> str: ...
    @property
    def database_template(self) -> str | None: ...

@final
@dataclass(frozen=True)
class ParsedRef:
    name: str
    relation_type: DiscoverySqlRelationType | str
    ref_type: DiscoveryRefType | None = None
    span: SqlSourceSpan | None = None

@final
@dataclass(frozen=True)
class LogicalResourceKey:
    resource_type: LogicalResourceType | str
    name: str

@final
@dataclass(frozen=True)
class CompiledSource:
    key: LogicalResourceKey
    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    effective_replay_lineage_mode: ReplayLineageMode

@final
@dataclass(frozen=True)
class ModelRetentionResolution:
    value: ModelRetentionPolicy | Literal[False] | None = None
    origin: RetentionOrigin | str | None = None

@dataclass(frozen=True)
class CompiledModel:
    key: LogicalResourceKey
    pipeline_name: str
    relation_name: str
    kind: ModelKind | str
    sql_analysis: SqlModelAnalysis
    @property
    def query(self) -> str: ...
    @property
    def output_columns(self) -> tuple[Column, ...]: ...
    @property
    def parsed_refs(self) -> tuple[ParsedRef, ...]: ...
    @property
    def refs(self) -> tuple[str, ...]: ...
    @property
    def has_aggregate_semantics(self) -> bool: ...

@final
@dataclass(frozen=True)
class CompiledTableModel(CompiledModel):
    transform: TransformStep
    preserves_required_lineage: bool
    replay_anchor_eligible: bool
    effective_bounded_replay_fallback: BoundedReplayFallback
    replay_on_change: ReplayOnChangePolicy | None = None
    retention: ModelRetentionResolution = field(default_factory=ModelRetentionResolution)
    retention_applied: bool = False
    on_row_error: RowErrorPolicy = RowErrorPolicy.FAIL
    @property
    def has_mutable_refs(self) -> bool: ...

@final
@dataclass(frozen=True)
class CompiledViewModel(CompiledModel):
    view: ViewStep

@final
@dataclass(frozen=True)
class ExternalSourceReplayConfig:
    key: ObjectKey
    table_name: str
    source_kind: SourceKind | str
    replay_boundary_mode: ReplayBoundaryMode | str
    partition_column_name: str | None = None
    offset_column_name: str | None = None
    timestamp_column_name: str | None = None
    landed_at_column_name: str | None = None
    cursor_column_name: str | None = None

@final
@dataclass(frozen=True)
class CompiledPipeline:
    pipeline: Pipeline
    project: Project | None
    file_path: Path
    effective_replay_lineage_mode: ReplayLineageMode | None
    source: CompiledSource | None
    models: tuple[CompiledModel, ...]

@final
@dataclass(frozen=True)
class DesiredState:
    objects: tuple[DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView, ...]
    replay_anchor_keys: frozenset[ObjectKey]
    mutable_ref_warning_keys: frozenset[ObjectKey]
    external_source_replay_configs: tuple[ExternalSourceReplayConfig, ...] = ()
    def is_managed_landing_table(self, name: str) -> bool: ...

@final
@dataclass(frozen=True)
class CompilerExpressionInferenceProfile:
    sql_analysis_dialect: str

@final
@dataclass(frozen=True)
class CompilerTargetMetadata:
    default_database: str | None
    default_schema: str | None
    production_target: bool = False
    destruction_relation_drop_size_limit: int | None = None

@final
@dataclass(frozen=True)
class CompilerAdapterProfile:
    identity: AdapterIdentity
    sql_analysis_dialect: str
    type_inference_profile: CompilerExpressionInferenceProfile
    target_metadata: CompilerTargetMetadata
    realize_source: AdapterSourceRealizer
    model_relation_name: AdapterModelRelationNamer
    realize_model: AdapterModelRealizer
    render_resource: AdapterResourceRenderer
    render_set_difference_comparison: AdapterSetDifferenceComparisonRenderer
    run_sql_rules: bool = True

@final
@dataclass(frozen=True)
class CompileProjectInputs:
    discovered_inputs: DiscoveredProjectInputs
    adapter_profile: CompilerAdapterProfile
    effective_target: CompilerTargetMetadata
    variables: tuple[tuple[str, object], ...]
    macro_registry: MacroRegistry
    macro_context: MacroContext
    pipelines: tuple[LoadedPipeline, ...]
    tests: tuple[LoadedSqlTest, ...]
    audits: tuple[LoadedSqlAudit, ...]
    sources: tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...] = ()
    virtual_environments: bool = False
    project_name: str | None = None
    target_name: str | None = None
    model_reference_scope: ModelReferenceScope | str = ModelReferenceScope.PROJECT
    allowed_cross_pipeline_references: tuple[AllowedCrossPipelineReference, ...] = ()

@final
@dataclass(frozen=True)
class CompiledProject:
    sources: tuple[CompiledSource, ...]
    models: tuple[CompiledModel, ...]
    pipelines: tuple[CompiledPipeline, ...]
    tests: tuple[LoadedSqlTest, ...]
    test_cases: tuple[SqlTestCase, ...]
    audits: tuple[LoadedSqlAudit, ...]
    macro_registry: MacroRegistry = field(default_factory=MacroRegistry)
    macro_context: MacroContext | None = None
    project_name: str | None = None
    target_name: str | None = None
    production_target: bool = False
    destruction_relation_drop_size_limit: int | None = None
    model_reference_scope: ModelReferenceScope | str = ModelReferenceScope.PROJECT
    allowed_cross_pipeline_references: tuple[AllowedCrossPipelineReference, ...] = ()

@final
@dataclass(frozen=True)
class CompilationTimings:
    discovery_ms: int
    compile_inputs_ms: int
    assembly_ms: int
    graph_ms: int
    realization_ms: int
    rules_ms: int = 0

@final
@dataclass(frozen=True)
class RealizedProject:
    project: CompiledProject
    resources_by_logical_key: Mapping[
        LogicalResourceKey,
        tuple[AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView, ...],
    ]
    relation_name_by_logical_key: Mapping[LogicalResourceKey, str]
    resolved_query_by_model_key: Mapping[LogicalResourceKey, str]
    desired_state: DesiredState

@final
@dataclass(frozen=True)
class CompileAnalysis:
    discovered_inputs: DiscoveredProjectInputs
    compile_inputs: CompileProjectInputs
    adapter_profile: CompilerAdapterProfile
    compiled_project: CompiledProject
    realized_project: RealizedProject
    graph: ProjectGraph
    diagnostics: tuple[CompilerDiagnostic, ...]
    timings: CompilationTimings
    access_policy: CompiledAccessPolicy | None = None
    sensors: CompiledSensors | None = None

@final
@dataclass(frozen=True)
class CompiledAccessGrant:
    permissions: tuple[Permission, ...]
    pipelines: tuple[str, ...] = ()
    scope: GrantScope | None = None

@final
@dataclass(frozen=True)
class CompiledAccessRole:
    name: str
    description: str | None
    grants: tuple[CompiledAccessGrant, ...]

@final
@dataclass(frozen=True)
class CompiledAccessPolicy:
    roles: tuple[CompiledAccessRole, ...]
    fingerprint: str

class PipelineCompileError(ValueError):
    span: SqlSourceSpan | None
    location: SourceLocation | None
    diagnostic: CompilerDiagnostic | None
    def __init__(
        self,
        message: str,
        *,
        span: SqlSourceSpan | None = None,
        location: SourceLocation | None = None,
    ) -> None: ...

class AuditCompileError(ValueError): ...

class TransformSqlContractError(Exception):
    transform_name: str
    span: SqlSourceSpan | None
    diagnostic: CompilerDiagnostic | None
    def __init__(self, transform_name: str) -> None: ...

class TransformSqlParseError(TransformSqlContractError):
    query: str
    details: str
    def __init__(self, transform_name: str, query: str, details: str) -> None: ...

class TransformSqlMultipleStatementsError(TransformSqlContractError):
    statement_count: int
    def __init__(self, transform_name: str, statement_count: int) -> None: ...

class TransformSqlFinalQueryShapeError(TransformSqlContractError):
    statement_type: str
    def __init__(self, transform_name: str, statement_type: str) -> None: ...

class TransformSqlTopLevelSetOperationError(TransformSqlContractError):
    statement_sql: str
    def __init__(self, transform_name: str, statement_sql: str) -> None: ...

class TransformSqlStarProjectionError(TransformSqlContractError):
    column_index: int
    def __init__(self, transform_name: str, column_index: int) -> None: ...

class TransformSqlDuplicateAliasError(TransformSqlContractError):
    alias: str
    def __init__(self, transform_name: str, alias: str) -> None: ...

class TransformSqlUntypedProjectionError(TransformSqlContractError):
    column_index: int
    projection_sql: str
    def __init__(self, transform_name: str, column_index: int, projection_sql: str) -> None: ...

class TransformOrderByUnknownColumnError(TransformSqlContractError):
    expression: str
    unknown_column_names: tuple[str, ...]
    available_column_names: tuple[str, ...]
    def __init__(
        self,
        transform_name: str,
        expression: str,
        unknown_column_names: tuple[str, ...],
        available_column_names: tuple[str, ...],
    ) -> None: ...

class TransformPartitionByUnknownColumnError(TransformSqlContractError):
    expression: str
    unknown_column_names: tuple[str, ...]
    available_column_names: tuple[str, ...]
    def __init__(
        self,
        transform_name: str,
        expression: str,
        unknown_column_names: tuple[str, ...],
        available_column_names: tuple[str, ...],
    ) -> None: ...

class TransformTtlUnknownColumnError(TransformSqlContractError):
    expression: str
    unknown_column_names: tuple[str, ...]
    available_column_names: tuple[str, ...]
    def __init__(
        self,
        transform_name: str,
        expression: str,
        unknown_column_names: tuple[str, ...],
        available_column_names: tuple[str, ...],
    ) -> None: ...

class MacroError(ValueError):
    location: SourceLocation | None
    diagnostic: CompilerDiagnostic
    def __init__(
        self,
        message: str,
        *,
        location: SourceLocation | None = None,
        related_locations: tuple[RelatedDiagnosticLocation, ...] = (),
    ) -> None: ...

class PipelineDiscoveryError(ValueError):
    diagnostic: CompilerDiagnostic | None
    def __init__(self, message: str) -> None: ...

class ModelHeaderSyntaxError(ValueError): ...
class ProjectSpecError(ValueError): ...
class ProjectConfigError(ProjectSpecError): ...
class DurationParseError(ProjectSpecError): ...
class RetentionConfigError(ProjectSpecError): ...

class AccessPolicyError(ValueError):
    location: SourceLocation
    def __init__(self, message: str, *, location: SourceLocation) -> None: ...

DESIRED_OBJECT_TYPE_KAFKA_TABLE: DesiredObjectType
DESIRED_OBJECT_TYPE_TABLE: DesiredObjectType
DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW: DesiredObjectType
DESIRED_OBJECT_TYPE_VIEW: DesiredObjectType
RAW_TABLE_NAME_PREFIX: str
MATERIALIZED_VIEW_NAME_PREFIX: str
REPLAY_PARTITION_COLUMN_NAME: str
REPLAY_OFFSET_COLUMN_NAME: str
REPLAY_TIMESTAMP_COLUMN_NAME: str
REPLAY_LANDED_AT_COLUMN_NAME: str
REPLAY_CURSOR_COLUMN_NAME: str
SECONDS_BY_DURATION_UNIT: dict[str, int]
ACCESS_POLICY_FILE_NAME: str

# Planning and execution shapes: planner, backfill, direct, population, promotion and repair.

class PlannedChangeType(StrEnum):
    REBUILD = "rebuild"
    CREATE = "create"
    REPLACE = "replace"
    NO_OP = "no_op"

class TableSchemaChangeKind(StrEnum):
    NON_BREAKING = "non_breaking"
    BREAKING = "breaking"

class TableSchemaSeedCompatibility(StrEnum):
    SEEDABLE = "seedable"
    NON_SEEDABLE = "non_seedable"

class RebuildStrategy(StrEnum):
    SHADOW_REBUILD = "shadow_rebuild"
    OFFLINE_REBUILD = "offline_rebuild"

class RebuildExecutionMode(StrEnum):
    FULL_REBUILD = "full_rebuild"
    SEEDED_BOUNDED_REBUILD = "seeded_bounded_rebuild"
    UNSEEDED_BOUNDED_REBUILD = "unseeded_bounded_rebuild"

class DeploymentPhase(StrEnum):
    PLAN = "plan"
    BACKFILL = "backfill"
    AUDIT = "audit"
    PUBLISH = "publish"

class DeploymentAction(StrEnum):
    PLAN_SHADOW_TABLE = "plan_shadow_table"
    PLAN_SHADOW_MATERIALIZED_VIEW = "plan_shadow_materialized_view"
    PLAN_SHADOW_VIEW = "plan_shadow_view"
    BACKFILL_SUBTREE = "backfill_subtree"
    AUDIT_SUBTREE = "audit_subtree"
    PUBLISH_SUBTREE = "publish_subtree"

class RootDeploymentStateKind(StrEnum):
    ACTIVE_VIEW_PRESENT = "active_view_present"
    GREENFIELD = "greenfield"
    LOGICAL_VIEW_MISSING = "logical_view_missing"
    INVALID_ACTIVE_VIEW = "invalid_active_view"

class DirectPlanReason(StrEnum):
    SELECTED = "selected"
    CHANGED = "changed"
    SOURCE_DRIFT = "source_drift"
    DOWNSTREAM_OF_SELECTED = "downstream_of_selected"
    MISSING_UPSTREAM = "missing_upstream"
    ALL_MODELS = "all_models"

class DirectSelectionMode(StrEnum):
    ALL_MODELS = "all_models"
    EXPLICIT = "explicit"
    CHANGED = "changed"

class DirectSqlBaselineStatus(StrEnum):
    FIRST_BASELINE = "first_baseline"
    QUERY_CHANGED = "query_changed"
    NO_QUERY_CHANGE = "no_query_change"
    BASELINE_UNAVAILABLE = "baseline_unavailable"

class DirectRelationAction(StrEnum):
    DROP = "drop"
    CREATE = "create"

class DirectResourceKind(StrEnum):
    TABLE = "table"
    MATERIALIZED_VIEW = "materialized_view"
    VIEW = "view"

class PublishOperation(StrEnum):
    PROMOTE = "promote"
    ROLLBACK = "rollback"

class PromotionPreviewClassification(StrEnum):
    INITIAL_PUBLISH = "initial_publish"
    PROMOTION = "promotion"

@final
@dataclass(frozen=True)
class PlanningWarehouseSnapshot:
    catalog: CatalogSnapshot
    object_state_records: tuple[ObjectStateRecord, ...]

@final
@dataclass(frozen=True)
class ActualKafkaTable:
    key: ObjectKey
    spec: KafkaTableSpec
    @property
    def name(self) -> str: ...
    @property
    def columns(self) -> tuple[Column, ...]: ...
    @property
    def kafka(self) -> KafkaSettings: ...

@final
@dataclass(frozen=True)
class ActualTable:
    key: ObjectKey
    spec: TableSpec
    @property
    def name(self) -> str: ...
    @property
    def columns(self) -> tuple[Column, ...]: ...
    @property
    def engine(self) -> str: ...
    @property
    def order_by(self) -> tuple[str, ...]: ...
    @property
    def partition_by(self) -> str | None: ...
    @property
    def ttl(self) -> str | None: ...
    @property
    def settings(self) -> Mapping[str, str] | None: ...

@final
@dataclass(frozen=True)
class ActualMaterializedView:
    key: ObjectKey
    spec: MaterializedViewSpec
    @property
    def name(self) -> str: ...
    @property
    def source_table_name(self) -> str: ...
    @property
    def target_table_name(self) -> str: ...
    @property
    def query(self) -> str: ...

@final
@dataclass(frozen=True)
class ActualView:
    key: ObjectKey
    spec: ViewSpec
    @property
    def name(self) -> str: ...
    @property
    def query(self) -> str: ...

@final
@dataclass(frozen=True)
class ActualState:
    objects: tuple[ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView, ...]

@final
@dataclass(frozen=True)
class RootDeploymentInspection:
    root_key: ObjectKey
    state_kind: str
    active_deployment_id: str | None

@final
@dataclass(frozen=True)
class ObjectStateMetadataRow:
    deployment_id: str
    database_name: str | None
    object_type: str
    object_name: str
    normalized_fingerprint: str
    normalized_query: str | None
    recorded_at: str
    observation_id: str
    state_kind: str

@final
@dataclass(frozen=True)
class PlannedObjectChange:
    key: ObjectKey
    change_type: PlannedChangeType | str
    force_full_refresh: bool = False
    forced_start_time: str | None = None
    schema_change_kind: TableSchemaChangeKind | str | None = None
    seed_compatibility: TableSchemaSeedCompatibility | str | None = None

@final
@dataclass(frozen=True)
class PlannedSqlDiff:
    key: ObjectKey
    object_type: str
    name: str
    diff_lines: tuple[str, ...]

@final
@dataclass(frozen=True)
class RebuildSubtree:
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

@final
@dataclass(frozen=True)
class DeploymentStep:
    step_id: str
    phase: DeploymentPhase | str
    action: DeploymentAction | str
    root_key: ObjectKey
    target_key: ObjectKey | None = None
    physical_name: str | None = None

@final
@dataclass(frozen=True)
class PreparedShadowObject:
    logical_key: ObjectKey
    physical_name: str
    logical_model_name: str

@final
@dataclass(frozen=True)
class PlannerWarning:
    warning_code: str
    message: str
    root_key: ObjectKey
    target_key: ObjectKey | None = None

@final
@dataclass(frozen=True)
class DeploymentPlan:
    deployment_id: str | None
    object_changes: tuple[PlannedObjectChange, ...]
    rebuild_subtrees: tuple[RebuildSubtree, ...]
    steps: tuple[DeploymentStep, ...]
    prepared_shadow_objects: tuple[PreparedShadowObject, ...]
    warnings: tuple[PlannerWarning, ...]
    sql_diffs: tuple[PlannedSqlDiff, ...] = ()

@final
@dataclass(frozen=True)
class PreparedObjectMapping:
    logical_key: ObjectKey
    physical_name: str
    logical_model_name: str

@final
@dataclass(frozen=True)
class ObjectStateRecord:
    deployment_id: str
    key: ObjectKey
    normalized_fingerprint: str
    normalized_query: str | None
    recorded_at: str
    observation_id: str = ""
    state_kind: str = "deployment"

@final
@dataclass(frozen=True)
class DeploymentRecord:
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

@final
@dataclass(frozen=True)
class DeploymentWatermarkRecord:
    deployment_id: str
    root_key: ObjectKey
    anchor_key: ObjectKey
    boundary_key: str
    cutoff_value: str

@final
@dataclass(frozen=True)
class PublishEventRecord:
    deployment_id: str
    published_at: str
    logical_view_names: tuple[str, ...]
    database: str = ""
    physical_relation_names: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class MetadataState:
    object_states: tuple[ObjectStateRecord, ...]
    deployments: tuple[DeploymentRecord, ...]
    deployment_watermarks: tuple[DeploymentWatermarkRecord, ...]
    publish_events: tuple[PublishEventRecord, ...]

@final
@dataclass(frozen=True)
class DirectWarehouseSnapshot:
    catalog: CatalogSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot = ...

@final
@dataclass(frozen=True)
class DirectPlanEntry:
    model_key: LogicalResourceKey
    reason: DirectPlanReason | str
    relation_names: tuple[str, ...]
    resource_kinds: tuple[DirectResourceKind | str, ...]
    driving_input_key: LogicalResourceKey | None
    is_replay_root: bool
    sql_change: DirectSqlChange | None = None

@final
@dataclass(frozen=True)
class DirectSqlChange:
    status: DirectSqlBaselineStatus | str
    current_sql: str
    current_hash: str
    previous_sql: str | None
    previous_hash: str | None
    unified_diff: str | None
    warning: str | None = None

@final
@dataclass(frozen=True)
class DirectPrerequisite:
    key: LogicalResourceKey
    relation_names: tuple[str, ...]
    present: bool
    framework_managed: bool = False

@final
@dataclass(frozen=True)
class DirectReplayRoot:
    model_key: LogicalResourceKey
    driving_input_key: LogicalResourceKey
    driving_input_relation_name: str
    driving_input_replay_columns: AdapterReplayColumns
    replay_boundary_mode: ReplayLineageMode | str
    propagated_model_keys: tuple[LogicalResourceKey, ...]
    has_aggregate_semantics: bool = False
    replay_settings: tuple[tuple[str, str], ...] = ()

@final
@dataclass(frozen=True)
class DirectRelationOperation:
    relation_name: str
    action: DirectRelationAction | str
    model_key: LogicalResourceKey
    resource_kind: DirectResourceKind | str

@final
@dataclass(frozen=True)
class DirectSourceDriftSelection:
    source_key: LogicalResourceKey
    pipeline_name: str
    reader_pipeline_names: tuple[str, ...]
    drifted_setting_names: tuple[str, ...]
    model_keys: tuple[LogicalResourceKey, ...]

@final
@dataclass(frozen=True)
class DirectRowErrorPolicyChange:
    model_key: LogicalResourceKey
    view_name: str
    dead_letter_view_name: str
    desired_policy: RowErrorPolicy
    landing_view_names: tuple[str, ...]
    logical_view_name: str = ""
    logical_dead_letter_view_name: str = ""
    previous_view_definition: str | None = None
    landing_repair_only: bool = False

@final
@dataclass(frozen=True)
class DirectPlan:
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

@dataclass(frozen=True)
class BackfillBootstrapRequest:
    desired_state: DesiredState
    default_database: str
    metadata_database: str
    replay_lineage_mode: ReplayLineageMode | str
    confirmed_plan: DeploymentPlan | None = None
    deployment_id: str | None = None
    full_refresh_keys: frozenset[ObjectKey] = frozenset()
    start_time_keys: frozenset[ObjectKey] = frozenset()
    start_time: str | None = None
    created_at: str | None = None
    boundary_time: str | None = None
    stabilization_seconds: float = 5.0
    confirmed_target_catalog: CatalogSnapshot | None = None
    confirmed_metadata_catalog: CatalogSnapshot | None = None
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = ()

@final
@dataclass(frozen=True)
class BackfillBootstrapResult:
    deployment_id: str
    created_at: str
    deployment_plan: DeploymentPlan
    root_reports: tuple[RootBackfillReport, ...]
    existing_relation_names: frozenset[str]

@final
@dataclass(frozen=True)
class BackfillRootReplayResult:
    root_key: ObjectKey
    written_rows: int | None

@final
@dataclass(frozen=True)
class BackfillExecutionResult:
    bootstrap: BackfillBootstrapResult
    boundary_time: str
    replay_results: tuple[BackfillRootReplayResult, ...]

@final
@dataclass(frozen=True)
class BackfillDeploymentIdentity:
    deployment_id: str
    created_at: str

@final
@dataclass(frozen=True)
class RootBackfillReport:
    root_key: ObjectKey
    state_kind: str
    replay_strategy: str
    active_deployment_id: str | None

@dataclass(frozen=True)
class DirectBuildRequest:
    plan: DirectPlan
    realized_project: RealizedProject
    database: str
    metadata_database: str
    tool_version: str
    workflow_id: str = ...
    stabilization_seconds: float = 5.0
    boundary_time: str | None = None
    audits: tuple[DirectBuildAudit, ...] = ()
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = ()
    @property
    def effective_start_time(self) -> str | None: ...

@final
@dataclass(frozen=True)
class DriftedManagedSource:
    source_key: LogicalResourceKey
    owner_pipeline_names: tuple[str, ...]
    drifted_setting_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class DirectBuildAudit:
    name: str
    query: str
    severity: str
    description: str | None

@final
@dataclass(frozen=True)
class DirectReplayRange:
    partition_value: str | None
    source_partition_column_name: str | None
    source_position_column_name: str
    source_timestamp_column_name: str | None
    lower_value: str
    upper_value: str
    replay_cutoff_value: str
    cutoff_inclusive: bool
    anchor_row_count: int | None = None

@final
@dataclass(frozen=True)
class DirectReplayCapture:
    capture_id: str
    workflow_id: str
    target_database: str
    logical_model_name: str
    driving_input_relation_name: str
    boundary_mode: ReplayLineageMode | str
    captured_at: str | None
    ranges: tuple[DirectReplayRange, ...]

@final
@dataclass(frozen=True)
class DirectRuntimeReplay:
    model_name: str
    capture_step_id: str
    replay_step_id: str
    replay: AdapterReplayRequest
    boundary_column_type: str | None
    managed_landing_anchor: bool = False

@dataclass(frozen=True)
class DirectBuildWorkflow:
    template: BuildWorkflow
    runtime_replays: tuple[DirectRuntimeReplay, ...]
    workflow_id: str
    source_recovery_statements: tuple[WarehouseStatement, ...] = ()
    recreated_sources: tuple[PopulationSourceRecreation, ...] = ()
    @property
    def mode(self) -> WorkflowMode: ...
    @property
    def plan_json(self) -> str: ...
    @property
    def statements(self) -> tuple[WarehouseStatement, ...]: ...

@final
@dataclass(frozen=True)
class DirectRuntimeExecution:
    workflow: BuildWorkflow
    execution: WorkflowExecutionResult
    captures: tuple[DirectReplayCapture, ...]

@final
@dataclass(frozen=True)
class DirectReplayBoundary:
    model_name: str
    driving_input_relation_name: str
    replay_boundary_mode: ReplayLineageMode | str
    boundary_key: str
    cutoff_value: str
    cutoff_inclusive: bool

@final
@dataclass(frozen=True)
class DirectRootReplayResult:
    model_name: str
    written_rows: int | None

@final
@dataclass(frozen=True)
class DirectBuildResult:
    database: str
    preserved_source_relation_names: tuple[str, ...]
    created_source_relation_names: tuple[str, ...]
    dropped_relation_names: tuple[str, ...]
    created_relation_names: tuple[str, ...]
    boundary_time: str
    boundaries: tuple[DirectReplayBoundary, ...]
    replay_results: tuple[DirectRootReplayResult, ...]
    effective_start_time: str | None = None

@final
@dataclass(frozen=True)
class DirectBuildExecutionResult:
    build_result: DirectBuildResult
    audit_result: SqlAuditRunResult

@final
@dataclass(frozen=True)
class PopulationRoot:
    root_key: ObjectKey
    affected_keys: tuple[ObjectKey, ...]
    upstream_boundary_key: ObjectKey
    replay_lineage_mode: ReplayLineageMode | str
    execution_mode: RebuildExecutionMode | str = RebuildExecutionMode.FULL_REBUILD
    forced_start_time: str | None = None
    execution_lookback_seconds: int | None = None
    persist_watermarks: bool = True

@final
@dataclass(frozen=True)
class PopulationObject:
    logical_key: ObjectKey
    physical_name: str

@final
@dataclass(frozen=True)
class PopulationPlan:
    execution_id: str
    roots: tuple[PopulationRoot, ...]
    objects: tuple[PopulationObject, ...]

@final
@dataclass(frozen=True)
class PopulationWatermark:
    root_key: ObjectKey
    anchor_key: ObjectKey
    boundary_key: str
    cutoff_value: str

@final
@dataclass(frozen=True)
class PopulationManagedSource:
    resource: AdapterManagedSource
    database: str

@final
@dataclass(frozen=True)
class PopulationRealization:
    resource: (
        AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView
    )
    database: str

@final
@dataclass(frozen=True)
class PopulationSourceRecreation:
    managed_source: PopulationManagedSource
    landing_view_name: str
    landing_relation_name: str
    consumer_group: str
    drifted_setting_names: tuple[str, ...]
    @property
    def staged_relation_name(self) -> str: ...

@final
@dataclass(frozen=True)
class PopulationSourcePreparation:
    preserved_relation_names: tuple[str, ...]
    created_relation_names: tuple[str, ...]
    landing_views: tuple[DesiredMaterializedView, ...]
    managed_sources: tuple[PopulationManagedSource, ...]
    recreated_sources: tuple[PopulationSourceRecreation, ...] = ()

@final
@dataclass(frozen=True)
class PublishRequest:
    deployment_id: str | None
    metadata_database: str
    default_database: str
    operation: PublishOperation | str = PublishOperation.PROMOTE
    previous_deployment_id: str | None = None

@final
@dataclass(frozen=True)
class PublishedView:
    view_name: str
    target_table_name: str

@final
@dataclass(frozen=True)
class PublishResult:
    deployment_id: str
    published_views: tuple[PublishedView, ...]
    per_relation_atomic_replace: bool
    graph_atomic_publish: bool
    operation: PublishOperation | str = PublishOperation.PROMOTE
    previous_deployment_id: str | None = None

@final
@dataclass(frozen=True)
class PromotionBindingAddition:
    database: str
    logical_name: str
    physical_name: str

@final
@dataclass(frozen=True)
class PromotionBindingReplacement:
    database: str
    logical_name: str
    from_physical_name: str
    to_physical_name: str

@final
@dataclass(frozen=True)
class PromotionBindingRemoval:
    database: str
    logical_name: str
    physical_name: str

@final
@dataclass(frozen=True)
class PromotionOrphanedRelation:
    database: str
    physical_name: str

@final
@dataclass(frozen=True)
class DeploymentPromotionPreview:
    classification: PromotionPreviewClassification
    additions: tuple[PromotionBindingAddition, ...]
    replacements: tuple[PromotionBindingReplacement, ...]
    removals: tuple[PromotionBindingRemoval, ...]
    orphaned_relations: tuple[PromotionOrphanedRelation, ...]

@final
@dataclass(frozen=True)
class RollbackRequest:
    deployment_id: str | None
    previous: bool
    metadata_database: str
    default_database: str

@final
@dataclass(frozen=True)
class RollbackPlan:
    current_deployment_id: str
    target_deployment_id: str
    logical_view_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class RepairActiveViewRequest:
    default_database: str
    table_name: str
    deployment_id: str

@final
@dataclass(frozen=True)
class RepairActiveViewResult:
    table_name: str
    target_table_name: str

class DeploymentPlanError(ValueError): ...
class ActualStateError(ValueError): ...
class DirectPlanError(ValueError): ...
class BackfillExecutionError(ValueError): ...
class DirectBuildError(RuntimeError): ...

class DirectWorkflowExecutionError(DirectBuildError):
    workflow: BuildWorkflow
    partial_result: WorkflowExecutionResult
    captures: tuple[DirectReplayCapture, ...]
    failed_step_id: str
    cause: BaseException
    def __init__(
        self,
        *,
        workflow: BuildWorkflow,
        partial_result: WorkflowExecutionResult,
        captures: tuple[DirectReplayCapture, ...],
        failed_step_id: str,
        cause: BaseException,
    ) -> None: ...

class PopulationScopeError(LookupError): ...
class PublishExecutionError(ValueError): ...

PLANNED_CHANGE_TYPE_REBUILD: PlannedChangeType
PLANNED_CHANGE_TYPE_CREATE: PlannedChangeType
PLANNED_CHANGE_TYPE_NO_OP: PlannedChangeType
TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING: TableSchemaChangeKind
TABLE_SCHEMA_CHANGE_KIND_BREAKING: TableSchemaChangeKind
TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE: TableSchemaSeedCompatibility
TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE: TableSchemaSeedCompatibility
REBUILD_STRATEGY_SHADOW: RebuildStrategy
REBUILD_EXECUTION_MODE_FULL: RebuildExecutionMode
REBUILD_EXECUTION_MODE_SEEDED_BOUNDED: RebuildExecutionMode
REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED: RebuildExecutionMode
DEPLOYMENT_PHASE_PLAN: DeploymentPhase
DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE: DeploymentAction
BLANK_VALUES: tuple[object, ...]
DIRECT_DEAD_LETTERS_VIEW_SUFFIX: str
REDACTED_SECRET_PLACEHOLDER: str
ADAPTER_DATABASE_PLACEHOLDER: str
METADATA_OBJECT_STATE_TABLE_NAME: str
METADATA_DEPLOYMENTS_TABLE_NAME: str
METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME: str
METADATA_PUBLISH_HISTORY_TABLE_NAME: str
METADATA_SCHEMA_VERSIONS_TABLE_NAME: str
METADATA_DIRECT_FINGERPRINTS_TABLE_NAME: str
METADATA_MANIFESTS_TABLE_NAME: str
METADATA_DEAD_LETTERS_TABLE_NAME: str
QUARANTINE_LIVE_VIEW_PATTERN: str
METADATA_INVOCATIONS_TABLE_NAME: str
METADATA_NODE_RESULTS_TABLE_NAME: str
METADATA_RUN_EVENTS_TABLE_NAME: str
METADATA_RUN_STATEMENTS_TABLE_NAME: str
METADATA_SENSOR_CHECKPOINTS_TABLE_NAME: str
METADATA_SENSOR_TICKS_TABLE_NAME: str
METADATA_SENSOR_STEPS_TABLE_NAME: str
METADATA_SENSOR_OVERRIDES_TABLE_NAME: str
METADATA_SENSOR_LEASES_TABLE_NAME: str
VIRTUAL_OBJECT_STATE_KIND_DEPLOYMENT: str
VIRTUAL_OBJECT_STATE_KIND_RECONCILE: str
VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE: str
MANAGED_SOURCE_KIND_KAFKA: str
ADAPTER_SECRET_PLACEHOLDER_PREFIX: str
ADAPTER_SECRET_PLACEHOLDER_SUFFIX: str
CLICKHOUSE_ADAPTER_NAME: str
CLICKHOUSE_WRITTEN_ROWS_SUMMARY_KEY: str
CLICKHOUSE_DEFAULT_DATABASE: str
CLICKHOUSE_SQL_ANALYSIS_DIALECT: str
CLICKHOUSE_VIRTUAL_ENVIRONMENTS_SUPPORTED: bool
CLICKHOUSE_MANAGED_SOURCE_KINDS: frozenset[str]
CLICKHOUSE_REPLAY_BOUNDARY_MODES: frozenset[AdapterReplayBoundaryMode]
CLICKHOUSE_HISTORY_PREFIX_SEED_SUPPORTED: bool
CLICKHOUSE_STABLE_LOGICAL_BINDINGS_SUPPORTED: bool
CLICKHOUSE_PER_RELATION_ATOMIC_REPLACE: bool
CLICKHOUSE_GRAPH_ATOMIC_PUBLISH: bool
CLICKHOUSE_SET_DIFFERENCE_COMPARISON_SUPPORTED: bool
CLICKHOUSE_DIRECT_REBUILD_SUPPORTED: bool
CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS: float
CLICKHOUSE_MODEL_TABLE_NAME_PREFIX: str
STAGED_SOURCE_NAME_PREFIX: str

# Operational executor shapes: destruction, readiness, deployment, auditing, observability,
# janitor, reconcile, SQL tests and doctor.

class DestructionOperation(StrEnum):
    DESTROY_PIPELINES = "destroy_pipelines"
    RESET_TARGET = "reset_target"

class DestructionRelationKind(StrEnum):
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    TABLE = "table"
    MANAGED_SOURCE = "managed_source"

class DestructionOwnership(StrEnum):
    CURRENT_MANIFEST = "current_manifest"
    VIRTUAL_PHYSICAL_MAPPING = "virtual_physical_mapping"
    PUBLISHED_STABLE_BINDING = "published_stable_binding"
    OWNERSHIP_LEDGER = "ownership_ledger"
    HISTORICAL_MANIFEST = "historical_manifest"

class DestructionPlanningErrorCode(StrEnum):
    SELECTION = "selection"
    UNKNOWN_PIPELINES = "unknown_pipelines"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    UNSUPPORTED_HISTORICAL_KIND = "unsupported_historical_kind"
    INVALID_OBJECT_TYPE = "invalid_object_type"

class AuditAssessment(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    CAUTION = "caution"

class DeploymentLifecycleState(StrEnum):
    ACTIVE = "active"
    STAGED = "staged"
    SUPERSEDED = "superseded"
    INCOMPLETE = "incomplete"
    METADATA_MISSING = "metadata_missing"
    PHYSICAL_MISSING = "physical_missing"

class DeploymentDiffStatus(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    PHYSICAL_MISSING = "physical_missing"

class AuditSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"

class QualityResultStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    ERROR = "error"
    DEFERRED = "deferred"

class QualityResultTrigger(StrEnum):
    BUILD = "build"
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    DEPLOYMENT = "deployment"

class MaterializationOutcome(StrEnum):
    APPLIED = "applied"
    FAILED = "failed"

@final
@dataclass(frozen=True)
class DestructionRequest:
    operation: DestructionOperation | str
    target: str
    database: str
    metadata_database: str
    pipeline_names: tuple[str, ...] = ()
    included_dependent_pipeline_names: tuple[str, ...] = ()
    include_orphans: bool = False

@final
@dataclass(frozen=True)
class InactivePipeline:
    name: str
    model_count: int
    resource_count: int
    last_published_at: str

@final
@dataclass(frozen=True)
class DestructionRelationEvidence:
    database: str
    name: str
    kind: DestructionRelationKind | str
    exists: bool
    total_bytes: int | None
    active_parts: int | None
    catalog_fingerprint: str | None
    logical_names: tuple[str, ...]
    pipeline_names: tuple[str, ...]
    ownership: tuple[DestructionOwnership, ...]
    dependency_relation_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class DestructionPlan:
    plan_id: str
    operation: DestructionOperation
    target: str
    database: str
    metadata_database: str
    requested_pipeline_names: tuple[str, ...]
    included_dependent_pipeline_names: tuple[str, ...]
    affected_pipeline_names: tuple[str, ...]
    affected_model_names: tuple[str, ...]
    affected_source_names: tuple[str, ...]
    relations: tuple[DestructionRelationEvidence, ...]
    challenges: tuple[str, ...]
    preserves_sources: bool
    preserves_replay_data: bool
    manifest_fingerprint: str
    plan_fingerprint: str
    created_at: datetime
    expires_at: datetime
    relation_drop_size_limit: int | None = None
    relation_drop_size_server_limit: int | None = None
    relation_drop_size_override: int | None = None
    relation_drop_size_policy_observed: bool = False
    include_orphans: bool = False
    @property
    def estimated_bytes(self) -> int: ...

@final
@dataclass(frozen=True)
class DestructionExecutionResult:
    invocation_id: str
    outcome: str
    completed_statement_sequences: tuple[int, ...]
    pending_statement_sequences: tuple[int, ...]
    remaining_relation_names: tuple[str, ...] | None
    error_message: str | None
    residual_catalog_status: str = "observed"
    residual_catalog_error: str | None = None

@final
@dataclass(frozen=True)
class DestructionActor:
    actor_id: str
    actor_name: str

@final
@dataclass(frozen=True)
class DestructionRecordingContext:
    plan: DestructionPlan
    actor_id: str
    actor_name: str
    reviewed_at: datetime
    confirmed_at: datetime
    challenge_responses: tuple[str, ...]
    connection: AdapterConnection
    observation_connection: AdapterConnection
    project_dir: Path
    started: tuple[str, str, int]
    statements: tuple[WarehouseStatement, ...]
    sink: RunEventSink

@final
@dataclass(frozen=True)
class DeploymentReadinessThresholds:
    maximum_lag_seconds: float = ...
    minimum_staged_row_ratio: float = ...

@final
@dataclass(frozen=True)
class DeploymentAuditRequest:
    deployment_id: str | None
    metadata_database: str
    default_database: str
    thresholds: DeploymentReadinessThresholds = field(default_factory=DeploymentReadinessThresholds)

@final
@dataclass(frozen=True)
class AuditDeploymentCandidate:
    deployment_id: str
    created_at: str | None = None
    deployment_status: str | None = None
    root_names: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class LoadedAuditDeployment:
    deployment_id: str
    created_at: str
    status: str
    replay_lineage_mode: ReplayLineageMode | str | None
    warning_codes: tuple[str, ...]
    root_keys: tuple[ObjectKey, ...]
    prepared_object_mappings: tuple[tuple[ObjectKey, str], ...]

@final
@dataclass(frozen=True)
class OffsetCatchupSummary:
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

@final
@dataclass(frozen=True)
class ScalarCatchupSummary:
    active_min_value: str | None
    active_max_value: str | None
    staged_min_value: str | None
    staged_max_value: str | None
    lag_seconds: float | None

@final
@dataclass(frozen=True)
class RootAuditResult:
    root_key: ObjectKey
    staged_physical_name: str
    staged_exists: bool
    active_exists: bool
    active_row_count: int | None
    staged_row_count: int | None
    row_delta: int | None
    row_ratio: float | None
    assessment: AuditAssessment | str
    replay_lineage_mode: ReplayLineageMode | str | None
    offset_catchup_summary: OffsetCatchupSummary | None
    scalar_catchup_summary: ScalarCatchupSummary | None
    state: str
    replay_source_name: str | None
    replay_source_row_count: int | None
    warnings: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class DeploymentAuditResult:
    deployment_id: str
    deployment_status: str
    assessment: AuditAssessment | str
    replay_lineage_mode: ReplayLineageMode | str | None
    warning_codes: tuple[str, ...]
    root_results: tuple[RootAuditResult, ...]
    quality_check_results: tuple[SqlAuditResult, ...] = ()
    thresholds: DeploymentReadinessThresholds = field(default_factory=DeploymentReadinessThresholds)

@final
@dataclass(frozen=True)
class DeploymentSummary:
    deployment_id: str
    state: DeploymentLifecycleState
    created_at: str | None
    persisted_status: str | None
    root_names: tuple[str, ...]
    physical_relation_names: tuple[str, ...]
    missing_physical_relation_names: tuple[str, ...]
    active_binding_names: tuple[str, ...]
    latest_published_at: str | None

@final
@dataclass(frozen=True)
class DeploymentInventory:
    database: str
    deployments: tuple[DeploymentSummary, ...]

@final
@dataclass(frozen=True)
class DeploymentDiffRequest:
    database: str
    metadata_database: str
    comparison: str

@final
@dataclass(frozen=True)
class DeploymentDiffColumn:
    name: str
    type: str
    default_expression: str | None

@final
@dataclass(frozen=True)
class DeploymentDiffRelation:
    database: str
    logical_name: str
    status: DeploymentDiffStatus | str
    from_physical_name: str | None
    to_physical_name: str | None
    from_columns: tuple[DeploymentDiffColumn, ...]
    to_columns: tuple[DeploymentDiffColumn, ...]
    from_row_count: int | None
    to_row_count: int | None

@final
@dataclass(frozen=True)
class DeploymentDiffResult:
    database: str
    from_endpoint: str
    to_endpoint: str
    relations: tuple[DeploymentDiffRelation, ...]

@final
@dataclass(frozen=True)
class SqlAuditResult:
    file_path: Path
    referenced_model_names: tuple[str, ...]
    severity: str
    passed: bool
    failing_row_count: int
    sample_column_names: tuple[str, ...]
    sample_rows: tuple[tuple[object, ...], ...]
    description: str | None = None
    name: str | None = None
    error_message: str | None = None
    deferred_until: str | None = None
    missing_relation_names: tuple[str, ...] = ()
    @property
    def deferred(self) -> bool: ...

@final
@dataclass(frozen=True)
class SqlAuditRunResult:
    audit_results: tuple[SqlAuditResult, ...]
    @property
    def error_failure_count(self) -> int: ...
    @property
    def warning_failure_count(self) -> int: ...

@final
@dataclass(frozen=True)
class AuditWarmupState:
    eligible: bool
    anchor: str | None
    eligible_at: str | None
    missing_model_names: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class RunStartupTimings:
    compile_ms: int
    observability_ms: int
    planning_ms: int
    @property
    def total_ms(self) -> int: ...

@final
@dataclass(frozen=True)
class TerminalInvocation:
    project_dir: Path
    target_identity: str
    command: str
    mode: str | None
    outcome: str
    exit_code: int
    materialized_outcome: str | None
    deployment_id: str | None
    workflow_id: str | None
    selected_node_count: int
    error_message: str | None
    summary: dict[str, object]

@final
@dataclass(frozen=True)
class QualityResultContext:
    trigger: QualityResultTrigger | str
    scheduled_for: str | None = None
    cadence_seconds: int | None = None
    warmup_seconds: int = 0

@final
@dataclass(frozen=True)
class JanitorPreviewCandidate:
    deployment_id: str
    created_at: str
    status: str
    logical_view_names: tuple[str, ...]
    physical_object_names: tuple[str, ...]
    deletable: bool
    reason: str

@final
@dataclass(frozen=True)
class JanitorPreviewResult:
    database: str
    retention_days: int
    minimum_rollback_deployments: int
    candidates: tuple[JanitorPreviewCandidate, ...]

@final
@dataclass(frozen=True)
class JanitorApplyResult:
    database: str
    retention_days: int
    minimum_rollback_deployments: int
    deleted_deployment_ids: tuple[str, ...]
    deleted_object_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class JanitorRequest:
    database: str
    metadata_database: str
    retention_days: int
    apply: bool
    minimum_rollback_deployments: int = 2

@final
@dataclass(frozen=True)
class ReconcileRejectedTarget:
    target_key: ObjectKey
    target_name: str
    reasons: tuple[str, ...]

@final
@dataclass(frozen=True)
class ReconcilePreview:
    database: str
    reconcile_id: str
    eligible_records: tuple[ObjectStateRecord, ...]
    rejected_targets: tuple[ReconcileRejectedTarget, ...]

@final
@dataclass(frozen=True)
class ReconcileResult:
    database: str
    reconcile_id: str
    reconciled_records: tuple[ObjectStateRecord, ...]
    rejected_targets: tuple[ReconcileRejectedTarget, ...]

@final
@dataclass(frozen=True)
class SqlTestTargetExecutionResult:
    target_model_name: str
    passed: bool
    column_names: tuple[str, ...]
    missing_rows: tuple[tuple[object, ...], ...]
    unexpected_rows: tuple[tuple[object, ...], ...]

@final
@dataclass(frozen=True)
class SqlTestExecutionResult:
    file_path: Path
    passed: bool
    target_results: tuple[SqlTestTargetExecutionResult, ...]
    executed_sql: str
    warnings: tuple[str, ...] = ()
    name: str | None = None
    test_index: int = 1
    error_message: str | None = None

@final
@dataclass(frozen=True)
class DoctorRequest:
    default_database: str

@final
@dataclass(frozen=True)
class ActiveViewStatus:
    table_name: str
    state_kind: str
    active_deployment_id: str | None
    candidate_deployment_ids: tuple[str, ...]

@final
@dataclass(frozen=True)
class DoctorResult:
    active_views: tuple[ActiveViewStatus, ...]

class DestructionError(RuntimeError): ...
class DestructionSelectionError(DestructionError): ...
class DestructionValidationError(DestructionError, ValueError): ...
class DestructionResourceError(DestructionError, TypeError): ...
class DestructionConsistencyError(DestructionError): ...

class DestructionDependencyError(DestructionSelectionError):
    dependent_pipeline_names: tuple[str, ...]
    def __init__(self, dependent_pipeline_names: tuple[str, ...]) -> None: ...

class DestructionExternalDependencyError(DestructionSelectionError):
    relation_names: tuple[str, ...]
    def __init__(self, relation_names: tuple[str, ...]) -> None: ...

class DestructionPlanNotFoundError(DestructionError): ...
class DestructionPlanExpiredError(DestructionError): ...
class DestructionPlanNotReviewedError(DestructionError): ...
class DestructionPlanCorruptError(DestructionError): ...
class DestructionChallengeError(DestructionError): ...
class DestructionDriftError(DestructionError): ...
class DestructionRecordingError(DestructionError): ...
class DestructionRecoveryError(DestructionError): ...
class DestructionRecoveryNotFoundError(DestructionRecoveryError): ...
class DeploymentAuditExecutionError(ValueError): ...
class DeploymentNotFoundError(ValueError): ...
class DeploymentDiffError(ValueError): ...
class AuditExecutionError(ValueError): ...
class JanitorInputError(ValueError): ...
class ReconcileError(RuntimeError): ...
class SqlTestExecutionError(ValueError): ...

DEFAULT_DESTRUCTION_PLAN_TTL: timedelta
DESTRUCTION_PLAN_PAYLOAD_VERSION: int
PRODUCTION_TARGET_NAMES: frozenset[str]
ACCEPTABLE_LAG_SECONDS: float
MINIMUM_STAGED_ROW_RATIO: float
ACTIVE_DEPLOYMENT_DIFF_ENDPOINT: str
EXPLICIT_DEPLOYMENT_DIFF_ENDPOINT_COUNT: int
AUDIT_SAMPLE_LIMIT: int
MAX_OBSERVATION_JSON_BYTES: int
MAX_OBSERVATION_ERROR_LENGTH: int
HEARTBEAT_INTERVAL_SECONDS: float
RUN_DISPLAY_COMMAND_ENV_VAR: str
RUN_INVOCATION_ID_ENV_VAR: str
PROJECT_REVISION_ENV_VAR: str
RECONCILE_DEPLOYMENT_ID_PREFIX: str

# CLI, account, authorization, workflow, sensor, dev server and diagnostic shapes.

class AdminCommand(StrEnum):
    MIGRATE = "migrate"
    CREATE_USER = "create-user"
    GRANT_ROLE = "grant-role"
    REVOKE_ROLE = "revoke-role"
    ENABLE_USER = "enable-user"
    DISABLE_USER = "disable-user"
    RESET_PASSWORD = "reset-password"

class CliCommand(StrEnum):
    ADMIN = "admin"
    PLAYGROUND = "playground"
    DISCOVER = "discover"
    DEV = "dev"
    COMPILE = "compile"
    LINT = "lint"
    FORMAT = "format"
    TEST = "test"
    PLAN = "plan"
    BUILD = "build"
    DESTROY = "destroy"
    RESET_TARGET = "reset-target"
    DEPLOYMENT = "deployment"
    AUDIT = "audit"
    RECONCILE = "reconcile"
    JANITOR = "janitor"
    DOCTOR = "doctor"
    REPAIR = "repair"

class CliSubcommand(StrEnum):
    ACTIVE_VIEW = "active-view"
    DIFF = "diff"
    LIST = "list"
    SHOW = "show"
    AUDIT = "audit"
    PROMOTE = "promote"
    ROLLBACK = "rollback"

class AuthenticationMode(StrEnum):
    DISABLED = "disabled"
    PASSWORD = "password"
    TRUSTED_PROXY = "trusted_proxy"

class AuthenticationSource(StrEnum):
    LOCAL = "local"
    PASSWORD = "password"
    TRUSTED_PROXY = "trusted_proxy"

class UnknownUserPolicy(StrEnum):
    AUTO_PROVISION = "auto_provision"
    DENY = "deny"

class AuthorizationReason(StrEnum):
    SYSTEM_ADMIN = "system_admin"
    GRANTED = "granted"
    POLICY_ABSENT = "policy_absent"
    NO_MATCHING_ASSIGNMENT = "no_matching_assignment"
    STALE_ASSIGNMENT = "stale_assignment"
    EMPTY_PIPELINE_SCOPE = "empty_pipeline_scope"
    MISSING_PERMISSION = "missing_permission"
    MISSING_PIPELINES = "missing_pipelines"

class ActivityTone(StrEnum):
    NEUTRAL = "neutral"
    GOOD = "good"
    BAD = "bad"
    CAUTION = "caution"

class RunPresentationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"
    CANCELLATION_FAILED = "cancellation_failed"
    RUNNING = "running"
    UNRESPONSIVE = "unresponsive"
    PRESUMED_FAILED = "presumed_failed"

class CompileStateKind(StrEnum):
    OK = "ok"
    FAILING = "failing"

class ReplayAnchorReason(StrEnum):
    ELIGIBLE = "eligible"
    AGGREGATE = "aggregate"
    MUTABLE_REF = "mutable_ref"
    NEVER = "never"
    LINEAGE_LOSS = "lineage_loss"
    VIEW = "view"

class Freshness(StrEnum):
    FRESH = "fresh"
    LAGGING = "lagging"
    STALLED = "stalled"

class SensorSchedulerState(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    RUNNING = "running"
    STANDBY = "standby"
    BACKING_OFF = "backing_off"

class AuditScheduleState(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    DUE = "due"
    SCHEDULED = "scheduled"
    WARMING_UP = "warming_up"
    NOT_MATERIALIZED = "not_materialized"
    RUNNING = "running"
    BLOCKED = "blocked"
    BACKING_OFF = "backing_off"

@final
@dataclass(frozen=True)
class SourceLocation:
    path: Path
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None

@final
@dataclass(frozen=True)
class RelatedDiagnosticLocation:
    label: str
    location: SourceLocation
    message: str | None = None

@final
@dataclass(frozen=True)
class CompilerDiagnostic:
    phase: DiagnosticPhase | str
    severity: DiagnosticSeverity | str
    code: str
    message: str
    resource_name: str | None = None
    location: SourceLocation | None = None
    related_locations: tuple[RelatedDiagnosticLocation, ...] = ()
    help: str | None = None

@final
@dataclass(frozen=True)
class Principal:
    user_id: UUID
    username: str
    display_name: str | None
    email: str | None
    authentication_source: AuthenticationSource

@final
@dataclass(frozen=True, repr=False)
class AuthSettings:
    mode: AuthenticationMode
    control_store_url: str
    username_header: str = "X-SQLTide-User"
    display_name_header: str | None = None
    email_header: str | None = None
    unknown_user_policy: UnknownUserPolicy = ...
    default_role: str = "viewer"
    session_cookie_name: str = "sqltide_session"
    session_ttl_seconds: int = ...
    session_cookie_secure: bool = True
    proxy_logout_url: str | None = None
    def __repr__(self) -> str: ...

@final
@dataclass(frozen=True)
class UserAccount:
    user_id: UUID
    username: str
    display_name: str | None
    email: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    roles: tuple[str, ...] = ()
    authentication_sources: tuple[AuthenticationSource, ...] = ()

@final
@dataclass(frozen=True)
class SessionCredentials:
    token: str
    csrf_token: str
    expires_at: datetime

@final
@dataclass(frozen=True)
class ResolvedSession:
    principal: Principal
    roles: tuple[str, ...]
    csrf_token: str
    expires_at: datetime

@final
@dataclass(frozen=True)
class AuthenticatedRequest:
    principal: Principal
    roles: tuple[str, ...]
    csrf_token: str | None = None

@final
@dataclass(frozen=True)
class AccountAuditRecord:
    operation: str
    actor_user_id: UUID | None
    affected_user_id: UUID | None
    occurred_at: datetime
    details: dict[str, object]

@final
@dataclass(frozen=True)
class ProjectRoleAssignment:
    assignment_id: UUID
    user_id: UUID
    project_name: str
    role_name: str
    target_name: str | None
    assigned_by: UUID | None
    assigned_at: datetime
    revoked_by: UUID | None = None
    revoked_at: datetime | None = None

@final
@dataclass(frozen=True)
class AuthorizationRequest:
    authenticated: AuthenticatedRequest
    permission: Permission
    project_name: str
    target_name: str | None
    grant_scope: GrantScope | None
    affected_pipelines: tuple[str, ...]
    policy: CompiledAccessPolicy | None

@final
@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: AuthorizationReason
    permission: Permission
    project_name: str
    target_name: str | None
    affected_pipelines: tuple[str, ...]
    matched_roles: tuple[str, ...] = ()
    missing_pipelines: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class CapabilityRequest:
    authenticated: AuthenticatedRequest
    project_name: str
    target_name: str | None
    pipeline_names: tuple[str, ...]
    policy: CompiledAccessPolicy | None

@final
@dataclass(frozen=True)
class EffectiveCapabilities:
    system_admin: bool
    project_name: str
    target_name: str | None
    permissions: tuple[Permission, ...]
    pipeline_permissions: tuple[tuple[Permission, tuple[str, ...]], ...]
    stale_roles: tuple[str, ...] = ()

@final
@dataclass(frozen=True)
class WarehouseStatement:
    sequence: int
    step_id: str
    phase: WorkflowPhase
    intent: StatementIntent
    sql: str
    continue_on_error: bool = False
    display_name: str | None = None
    replay_offset_progress: AdapterReplayOffsetProgressRequest | None = None

@dataclass(frozen=True)
class BuildWorkflow:
    mode: WorkflowMode
    plan_json: str
    statements: tuple[WarehouseStatement, ...]
    recovery_statements: tuple[WarehouseStatement, ...] = ()

@final
@dataclass(frozen=True)
class PublishedBuildWorkflow:
    workflow: BuildWorkflow
    artifact_root: Path
    workflow_sha256: str

@final
@dataclass(frozen=True)
class WorkflowStatementResult:
    step_id: str
    query_result: AdapterQueryResult | None
    mutation_result: AdapterMutationResult | None
    error_message: str | None = None

@final
@dataclass(frozen=True)
class WorkflowExecutionResult:
    statement_results: tuple[WorkflowStatementResult, ...]

@final
@dataclass(frozen=True)
class BuildCommandOptions:
    pipelines_root: Path
    database: str | None
    metadata_database: str | None
    selectors: tuple[str, ...]
    json_output: bool
    verbose: bool
    auto_approve: bool
    deployment_id: str | None = None
    full_refresh: bool = False
    start_time: str | None = None
    events_output: bool = False
    confirmations: tuple[str, ...] = ()
    changed: bool = False
    include_missing_upstream: bool = False

@final
@dataclass(frozen=True)
class BuildProtectionRequirement:
    pipeline_name: str
    warning: str
    confirmation: str

@final
@dataclass(frozen=True)
class WorkflowPreparationOptions:
    database: str | None
    metadata_database: str | None
    selectors: tuple[str, ...]
    deployment_id: str | None
    full_refresh: bool
    start_time: str | None
    verbose: bool
    changed: bool = False
    include_missing_upstream: bool = False

@dataclass(frozen=True)
class DirectBuildPreviewContext:
    analysis: CompileAnalysis
    plan: DirectPlan
    warehouse_snapshot: DirectWarehouseSnapshot
    database: str
    metadata_database: str
    adapter_name: str
    effective_start_time: str | None = None
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = ()

@dataclass(frozen=True)
class VirtualBuildPreviewContext:
    database: str
    metadata_database: str
    desired_state: DesiredState
    plan: DeploymentPlan
    replay_lineage_mode: ReplayLineageMode
    deployment_id: str
    created_at: str
    root_reports: tuple[RootBackfillReport, ...]
    existing_relation_names: frozenset[str]
    target_catalog: CatalogSnapshot
    metadata_catalog: CatalogSnapshot
    full_refresh_keys: frozenset[ObjectKey] = frozenset()
    start_time_keys: frozenset[ObjectKey] = frozenset()
    start_time: str | None = None
    execution_logical_model_keys: frozenset[LogicalResourceKey] = frozenset()
    run_execution_scope: tuple[LogicalResourceKey, ...] = ()
    run_context_scope: tuple[LogicalResourceKey, ...] = ()
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = ()

@final
@dataclass(frozen=True)
class DirectWorkflowPreparation:
    preview: DirectBuildPreviewContext
    request: DirectBuildRequest
    workflow: DirectBuildWorkflow
    plan_text: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()

@final
@dataclass(frozen=True)
class VirtualWorkflowPreparation:
    preview: VirtualBuildPreviewContext
    request: BackfillBootstrapRequest
    workflow: BuildWorkflow
    plan_text: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()

@final
@dataclass(frozen=True)
class MixedWorkflowPreparation:
    virtual: VirtualWorkflowPreparation
    direct: DirectWorkflowPreparation
    plan_text: str
    plan_json: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()

@final
@dataclass(frozen=True, repr=False)
class DestructionCommandOptions:
    operation: DestructionOperation
    pipelines_root: Path
    project_dir: Path
    selected_target: str
    database: str | None
    selectors: tuple[str, ...]
    control_store_url: str
    include_orphans: bool = False
    cli_variables: tuple[tuple[str, object], ...] = ()
    environment: Mapping[str, str] | None = None

@final
@dataclass(frozen=True, repr=False)
class DevCommandOptions:
    pipelines_root: Path
    database: str | None
    host: str
    port: int
    selected_target: str | None = None
    cli_variables: tuple[tuple[str, object], ...] = ()
    environment: Mapping[str, str] | None = None
    connection_host: str | None = None
    connection_port: int | None = None
    connection_username: str | None = None
    connection_password: str | None = None
    auth_settings: AuthSettings | None = None

@final
@dataclass(frozen=True)
class CliEntrypointHandlers:
    run_discover: Callable[..., int]
    run_compile: Callable[..., int]
    run_test: Callable[..., int]
    run_audit: Callable[..., int]
    run_plan: Callable[..., int]
    run_build: Callable[..., int]
    run_destruction: Callable[..., int]
    run_deployment_diff: Callable[..., int]
    run_deployment_list: Callable[..., int]
    run_deployment_show: Callable[..., int]
    run_deployment_audit: Callable[..., int]
    run_deployment_promote: Callable[..., int]
    run_deployment_rollback: Callable[..., int]
    run_reconcile: Callable[..., int]
    run_janitor: Callable[..., int]
    run_doctor: Callable[..., int]
    run_repair_active_view: Callable[..., int]
    run_dev: Callable[..., int]

@final
@dataclass(frozen=True)
class PlanCommandOptions:
    pipelines_root: Path
    database: str | None
    selectors: tuple[str, ...]
    deployment_id: str | None
    full_refresh: bool
    start_time: str | None
    json_output: bool
    verbose: bool
    changed: bool = False
    include_missing_upstream: bool = False

@final
@dataclass(frozen=True)
class SelectionResolution:
    desired_state: DesiredState
    selected_logical_model_keys: frozenset[LogicalResourceKey]
    selected_model_keys: frozenset[ObjectKey]
    replay_lineage_mode: ReplayLineageMode | None
    execution_logical_model_keys: frozenset[LogicalResourceKey] = frozenset()

class AuthenticationError(Exception): ...
class AuthorizationError(Exception): ...
class ControlStoreError(Exception): ...
class AuthConfigurationError(ValueError): ...
class AccountValidationError(ValueError): ...
class PasswordValidationError(ValueError): ...
class AccountConflictError(ControlStoreError): ...
class AccountNotFoundError(ControlStoreError): ...
class CompileArtifactError(ValueError): ...

class CliUserError(Exception):
    hint: str | None
    def __init__(self, message: str, *, hint: str | None = None) -> None: ...

class BuildPipelineLimitError(CliUserError): ...

class SensorError(ValueError):
    location: SourceLocation | None
    diagnostic: CompilerDiagnostic
    def __init__(
        self,
        message: str,
        *,
        location: SourceLocation | None = None,
        related_locations: tuple[RelatedDiagnosticLocation, ...] = (),
    ) -> None: ...

class SensorStepError(RuntimeError): ...
class SensorEvaluationTimeoutError(RuntimeError): ...
class DevServerError(RuntimeError): ...
class DevConfigurationError(DevServerError): ...
class KafkaCollectorClosedError(DevServerError): ...
class ProjectNotCompiledError(DevServerError): ...
class BuildInProgressError(DevServerError): ...
class BuildStartError(DevServerError): ...
class AuditSchedulerPersistenceError(DevServerError): ...
class MessageQueryValidationError(DevServerError): ...
class MessageSchemaError(DevServerError): ...

CONTROL_STORE_ENV_VAR: str
SQLTIDE_TOOL_VERSION: str
EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR: str
EXPECTED_BUILD_READ_SCOPE_ENV_VAR: str
EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR: str
DEV_CLI_VARIABLES_ENV_VAR: str
COMMANDS_REQUIRING_PIPELINES_ROOT: frozenset[CliCommand]
COMMANDS_WITHOUT_ADAPTER_CONNECTION: frozenset[CliCommand]
VIRTUAL_ENVIRONMENT_ONLY_COMMANDS: frozenset[CliCommand]
DISPLAY_NAME_BY_COMMAND: Mapping[CliCommand, str]
METADATA_TABLE_NAME_PREFIX: str
AFFIRMATIVE_RESPONSES: frozenset[str]
TRUE_ENV_VALUES: frozenset[str]
ADMIN_ROLE: str
VIEWER_ROLE: str
ACCOUNT_SCHEMA_COMPONENT: str
PASSWORD_MIN_LENGTH: int
SQLITE_MEMORY_PATH: str
LOCAL_USER_ID: UUID
WORKFLOW_PHASE_ORDER: tuple[WorkflowPhase, ...]
RECOVER_MODEL_VIEW_STEP_PREFIX: str
RECOVER_SOURCE_STEP_PREFIX: str
RECOVER_DEAD_LETTERS_STEP_PREFIX: str
DEFAULT_SENSOR_TIMEOUT_SECONDS: float
DEFAULT_DEV_SERVER_HOST: str
DEFAULT_DEV_SERVER_PORT: int
KAFKA_REUSE_LOG_FIRST_READ_COUNT: int
KAFKA_REUSE_LOG_INTERVAL: int
CANCEL_GRACE_SECONDS: float
TERMINATE_GRACE_SECONDS: float
ACTIVITY_WINDOW_SECONDS: int

# Sensor runtime, event observation and dev server shapes.

class SensorKind(StrEnum):
    EVENT = "event"
    POLLING = "polling"

class DefaultSensorStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"

class SensorOverrideStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    DECLARED_IN_CODE = "declared_in_code"

class SensorTickStatus(StrEnum):
    STARTED = "started"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"

class StepPolicy(StrEnum):
    AT_LEAST_ONCE = "at_least_once"
    AT_MOST_ONCE = "at_most_once"

@final
@dataclass(frozen=True)
class LoadedSensor:
    name: str
    kind: SensorKind
    declaration: SensorDeclaration
    file_path: Path
    relative_path: Path
    source: str
    definition_line: int
    description: str | None = None
    timeout_seconds: float = ...
    @property
    def identity_fingerprint(self) -> str: ...
    @property
    def default_status(self) -> DefaultSensorStatus: ...
    @property
    def retry_policy(self) -> SensorRetryPolicy: ...

@final
@dataclass(frozen=True)
class CompiledSensors:
    registry: SensorRegistry
    providers: tuple[DiscoveredProvider, ...] = ()

@final
@dataclass(frozen=True)
class SensorStreamPosition:
    completed_at: str
    result_id: str

@final
@dataclass(frozen=True)
class StepMarker:
    status: str
    result_json: str | None
    attempt: int

@final
@dataclass(frozen=True)
class TickAttemptState:
    failed_attempts: int
    last_failed_at: str | None
    last_error_message: str | None
    resolved: bool

@final
@dataclass(frozen=True)
class SensorTickView:
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

@final
@dataclass(frozen=True)
class PollingTickState:
    last_started_at: str | None
    last_success_at: str | None
    cursor: str | None

@final
@dataclass(frozen=True)
class SensorEvaluation:
    status: SensorTickStatus
    skip_reason: str | None = None
    error_message: str | None = None
    cursor: str | None = None

@final
@dataclass(frozen=True)
class SensorDeliveryOutcome:
    resolved: bool
    evaluation: SensorEvaluation | None = None
    dead_lettered: bool = False

@final
@dataclass(frozen=True)
class SensorEventDelivery:
    position: SensorStreamPosition
    events: tuple[SensorEvent, ...]

@final
@dataclass(frozen=True)
class SensorDispatchSummary:
    lease_acquired: bool = True
    evaluated: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    dead_lettered: int = 0

@final
@dataclass(frozen=True)
class SensorRegistry:
    sensors: Mapping[str, LoadedSensor] = ...
    def ordered(self) -> tuple[LoadedSensor, ...]: ...

@final
@dataclass(frozen=True)
class NodeResultObservation:
    result_id: str
    invocation_id: str
    node_kind: QualityNodeKind
    node_name: str
    binding_key: str
    target_identity: str
    trigger: str
    status: QualityResultStatus
    severity: str | None
    failure_count: int
    completed_at: str
    scheduled_for: str | None
    payload_json: str
    error_message: str | None

@final
@dataclass(frozen=True)
class InvocationObservation:
    invocation_id: str
    command: str
    mode: str | None
    outcome: str
    exit_code: int
    target_identity: str
    deployment_id: str | None
    selected_node_count: int
    error_message: str | None
    completed_at: str

@final
@dataclass(frozen=True)
class ReplayOffsetProgress:
    percentage: float
    eta_seconds: float | None
    completed_span: int
    total_span: int
    observed_partitions: int
    total_partitions: int

@final
@dataclass(frozen=True, repr=False)
class DevExecutionContext:
    database: str | None = None
    selected_target: str | None = None
    cli_variables: tuple[tuple[str, object], ...] = ()
    environment: Mapping[str, str] | None = None
    connection_host: str | None = None
    connection_port: int | None = None
    connection_username: str | None = None
    connection_password: str | None = None
    run_presumed_failed_after_seconds: int = ...
    connection_factory: Callable[[], AdapterConnection] | None = None
    observation_connection_factory: Callable[[], AdapterConnection] | None = None

@final
@dataclass(frozen=True)
class CompileErrorInfo:
    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

@final
@dataclass(frozen=True)
class CompileOutcome:
    state: CompileStateKind
    version_key: str
    compiled_at: str
    analysis: CompileAnalysis | None = None
    timings: CompilationTimings | None = None
    error: CompileErrorInfo | None = None

@final
@dataclass(frozen=True)
class OperationAuthorizationContext:
    store: ControlStore
    project_dir: Path
    selected_target: str | None

@final
@dataclass(frozen=True)
class DevControlStores:
    accounts: ControlStore
    destruction_plans: RelationalDestructionPlanStore
    owns_accounts: bool
    def close(self) -> None: ...

@final
@dataclass(frozen=True)
class KafkaTopicInfo:
    name: str
    partition_count: int
    replication_factor: int
    internal: bool

@final
@dataclass(frozen=True)
class KafkaTopicsSnapshot:
    topics: tuple[KafkaTopicInfo, ...]

@final
@dataclass(frozen=True)
class KafkaPartitionLag:
    partition: int
    committed_offset: int | None
    end_offset: int
    lag_messages: int | None

@final
@dataclass(frozen=True)
class KafkaLagSnapshot:
    total_messages: int | None
    partitions: tuple[KafkaPartitionLag, ...]

@final
@dataclass(frozen=True)
class RelationStorage:
    rows: int = 0
    bytes: int = 0

@final
@dataclass(frozen=True)
class BuildCancellationOutcome:
    invocation_id: str
    query_id: str | None
    requested_at: str
    status: Literal["cancelled", "cancellation_failed"]
    process_exit_code: int
    warehouse_supported: bool | None
    query_found: bool | None
    warehouse_termination_confirmed: bool | None
    error_message: str | None = None

# Authored project specification and discovery shapes.

@final
@dataclass(frozen=True, repr=False)
class DiscoveredProjectFile:
    file_path: Path
    relative_path: Path
    contents: str

@final
@dataclass(frozen=True, repr=False)
class RawConnectionConfig:
    values: tuple[tuple[str, object], ...] = ()

@final
@dataclass(frozen=True)
class AuditSchedulerConfig:
    enabled: bool = False

@final
@dataclass(frozen=True)
class AuditSchedulerOverride:
    enabled: bool | None = None

@final
@dataclass(frozen=True)
class SensorAutomationConfig:
    enabled: bool = False
    tick_retention_days: int = 0
    maximum_event_age_seconds: int | None = None

@final
@dataclass(frozen=True)
class SensorAutomationOverride:
    enabled: bool | None = None
    tick_retention_days: int | None = None
    maximum_event_age_seconds: int | None = None

@final
@dataclass(frozen=True)
class BuildConfig:
    max_pipelines: int | None = None

@final
@dataclass(frozen=True)
class DestructionConfig:
    max_table_size_to_drop_bytes: int | None = None

@final
@dataclass(frozen=True)
class UiConfig:
    timezone: str = "UTC"

@final
@dataclass(frozen=True)
class AllowedCrossPipelineReference:
    upstream_pipeline: str
    downstream_pipeline: str

@final
@dataclass(frozen=True)
class ProjectDependencies:
    model_reference_scope: ModelReferenceScope | str = ...
    allowed_cross_pipeline_references: tuple[AllowedCrossPipelineReference, ...] = ()

@final
@dataclass(frozen=True, repr=False)
class ProjectTarget:
    database: str | None = None
    connection: RawConnectionConfig = ...
    variables: tuple[tuple[str, object], ...] = ()
    audit_scheduler: AuditSchedulerOverride = ...
    sensors: SensorAutomationOverride = ...
    build: BuildConfig = ...
    destruction: DestructionConfig = ...
    production: bool = False

@final
@dataclass(frozen=True, repr=False)
class LocalProjectTarget:
    database: str | None = None
    connection: RawConnectionConfig = ...
    variables: tuple[tuple[str, object], ...] = ()
    audit_scheduler: AuditSchedulerOverride = ...
    sensors: SensorAutomationOverride = ...

@final
@dataclass(frozen=True)
class ReplayOnChangeRule:
    mode: ReplayOnChangeMode | str
    lookback_seconds: int | None = None

@final
@dataclass(frozen=True)
class ReplayOnChangePolicy:
    breaking: ReplayOnChangeRule | None = None
    non_breaking: ReplayOnChangeRule | None = None

@final
@dataclass(frozen=True)
class SourceFreshnessPolicy:
    warn_after: str | None = None
    error_after: str | None = None

@final
@dataclass(frozen=True)
class AuditDefaults:
    severity: str | None = None
    cadence_seconds: int | None = None
    warmup_seconds: int | None = None

@final
@dataclass(frozen=True)
class DeploymentReadinessDefaults:
    maximum_lag_seconds: float = 30.0
    minimum_staged_row_ratio: float = 0.5

@final
@dataclass(frozen=True)
class KafkaSourceDefaults:
    naming_macro: str | None = None
    retention: KafkaRetentionPolicy | Literal[False] | None = None
    retention_template: object | None = field(default=None, repr=False, compare=False)

@final
@dataclass(frozen=True)
class ModelRetentionPolicy:
    duration_seconds: int
    timestamp_column: str
    cap_at_column: str | None = None
    when_missing: RetentionMissingBehavior | str = ...

@final
@dataclass(frozen=True)
class KafkaRetentionPolicy:
    duration_seconds: int
    timestamp: KafkaRetentionReference | str = ...
    fallback: KafkaRetentionReference | str = ...
    cap_at: KafkaRetentionReference | str | None = None

@final
@dataclass(frozen=True)
class ModelDefaults:
    retention: ModelRetentionPolicy | Literal[False] | None = None
    retention_template: object | None = field(default=None, repr=False, compare=False)
    on_row_error: RowErrorPolicy | str | None = None

@final
@dataclass(frozen=True)
class SourceDefaults:
    kafka: KafkaSourceDefaults = ...

@final
@dataclass(frozen=True)
class ProjectDefaults:
    managed_source_ttl: str | None = None
    model_ttl: str | None = None
    kafka_broker_list: str | None = None
    pipeline_mode: PipelineMode | str = ...
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback | None = None
    run_presumed_failed_after_seconds: int = ...
    freshness: SourceFreshnessPolicy | None = None
    audits: AuditDefaults = ...
    deployment_readiness: DeploymentReadinessDefaults = ...
    sources: SourceDefaults = ...
    models: ModelDefaults = ...

@final
@dataclass(frozen=True)
class LocalProjectDefaults:
    pipeline_mode: PipelineMode | str | None = None

@final
@dataclass(frozen=True)
class ProjectNaming:
    pipeline_prefix: str = ...
    pipeline_naming_macro: str | None = None
    table_prefix: str = ...
    view_prefix: str = ...

@final
@dataclass(frozen=True)
class PipelineNaming:
    table_prefix: str | None = None
    view_prefix: str | None = None

@final
@dataclass(frozen=True)
class PipelineProtection:
    warning: str
    confirmation: str

@final
@dataclass(frozen=True, repr=False)
class AuthoredProjectConfig:
    name: str
    adapter: str
    default_target: str
    connection: RawConnectionConfig
    variables: tuple[tuple[str, object], ...]
    targets: tuple[tuple[str, ProjectTarget], ...]
    defaults: ProjectDefaults = ...
    naming: ProjectNaming = ...
    audit_scheduler: AuditSchedulerConfig = ...
    sensors: SensorAutomationConfig = ...
    build: BuildConfig = ...
    ui: UiConfig = ...
    dependencies: ProjectDependencies = ...

@final
@dataclass(frozen=True, repr=False)
class LocalProjectConfig:
    target: str | None = None
    adapter: str | None = None
    defaults: LocalProjectDefaults = ...
    connection: RawConnectionConfig = ...
    variables: tuple[tuple[str, object], ...] = ()
    targets: tuple[tuple[str, LocalProjectTarget], ...] = ()

@final
@dataclass(frozen=True, repr=False)
class LoadedProjectConfiguration:
    project: AuthoredProjectConfig
    local: LocalProjectConfig
    project_source: DiscoveredProjectFile
    local_source: DiscoveredProjectFile | None

@final
@dataclass(frozen=True, repr=False)
class EffectiveProjectConfiguration:
    name: str
    adapter: str
    target_name: str
    database: str | None
    connection: RawConnectionConfig
    variables: tuple[tuple[str, object], ...]
    defaults: ProjectDefaults
    naming: ProjectNaming = ...
    audit_scheduler: AuditSchedulerConfig = ...
    sensors: SensorAutomationConfig = ...
    build: BuildConfig = ...
    destruction: DestructionConfig = ...
    ui: UiConfig = ...
    production_target: bool = False
    dependencies: ProjectDependencies = ...

@final
@dataclass(frozen=True, repr=False)
class DiscoveryKafkaSettings:
    broker_list: str
    topic: str
    consumer_group: str | None = None
    format: str = ...
    ttl: str | None = None
    settings: Mapping[str, str] | None = None
    retention: KafkaRetentionPolicy | Literal[False] | None = None
    retention_origin: KafkaRetentionOrigin | str | None = None

@final
@dataclass(frozen=True, repr=False)
class KafkaLandingStep:
    name: str
    kafka: DiscoveryKafkaSettings
    replay_boundary: ReplayBoundary | None = None
    freshness: SourceFreshnessPolicy | None = None
    name_origin: SourceNameOrigin | str = ...
    naming_macro: str | None = None
    naming_macro_fingerprint: str | None = None

@final
@dataclass(frozen=True)
class ReplayBoundaryColumns:
    partition: str | None = None
    offset: str | None = None
    timestamp: str | None = None
    landed_at: str | None = None
    cursor: str | None = None

@final
@dataclass(frozen=True)
class ReplayBoundary:
    mode: ReplayBoundaryMode | str
    columns: ReplayBoundaryColumns

@final
@dataclass(frozen=True)
class ExternalTableSourceStep:
    name: str
    kind: SourceKind | str
    table_name: str
    replay_boundary: ReplayBoundary
    freshness: SourceFreshnessPolicy | None = None
    name_origin: SourceNameOrigin | str = ...

@final
@dataclass(frozen=True)
class PostgresRefreshSourceStep:
    name: str
    kind: SourceKind | str
    host: str
    database: str
    table: str
    user: str
    refresh: str
    port: int = ...
    password_env: str | None = None
    append: bool = True
    freshness: SourceFreshnessPolicy | None = None
    name_origin: SourceNameOrigin | str = ...

@final
@dataclass(frozen=True)
class ModelColumnSpec:
    name: str
    description: str | None = None
    audits: tuple[object, ...] = ()

@final
@dataclass(frozen=True)
class ExecutionSettings:
    replay: Mapping[str, str] | None = None

@final
@dataclass(frozen=True)
class TransformStep:
    name: str
    source: str
    engine: str
    order_by: Sequence[str]
    relation_name: str | None = None
    description: str | None = None
    columns: tuple[ModelColumnSpec, ...] = ()
    audits: tuple[object, ...] = ()
    query: str | None = None
    sql_file: str | None = None
    partition_by: str | None = None
    ttl: str | None = None
    settings: Mapping[str, str] | None = None
    execution_settings: ExecutionSettings = ...
    replay_anchor: ReplayAnchorMode | str = ...
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback | str | None = None
    source_file_path: Path | None = None
    source_line: int = 1
    source_column: int = 1
    retention: ModelRetentionPolicy | Literal[False] | None = None
    on_row_error: RowErrorPolicy | str | None = None

@final
@dataclass(frozen=True)
class ViewStep:
    name: str
    query: str | None = None
    sql_file: str | None = None
    relation_name: str | None = None
    description: str | None = None
    columns: tuple[ModelColumnSpec, ...] = ()
    audits: tuple[object, ...] = ()
    source_file_path: Path | None = None
    source_line: int = 1
    source_column: int = 1

@final
@dataclass(frozen=True)
class Project:
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback | str | None = None
    model_ttl: str | None = None
    default_database: str | None = None
    adapter: str = ...
    naming: ProjectNaming = ...
    audit_defaults: AuditDefaults = ...
    audit_scheduler: AuditSchedulerConfig = ...
    model_retention: ModelRetentionPolicy | Literal[False] | None = None
    dependencies: ProjectDependencies = ...
    model_on_row_error: RowErrorPolicy | str | None = None

@final
@dataclass(frozen=True)
class Pipeline:
    name: str
    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep | None
    transforms: Sequence[TransformStep | ViewStep] = ...
    mode: PipelineMode | str = ...
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback | str | None = None
    naming: PipelineNaming = ...
    protection: PipelineProtection | None = None
    audit_defaults: AuditDefaults = ...
    execution_settings: ExecutionSettings = ...
    model_retention: ModelRetentionPolicy | Literal[False] | None = None
    model_on_row_error: RowErrorPolicy | str | None = None

@final
@dataclass(frozen=True)
class LoadedPipeline:
    pipeline: Pipeline
    file_path: Path
    project: Project | None = None

@final
@dataclass(frozen=True, repr=False)
class LoadedProject:
    project: Project
    source_file: DiscoveredProjectFile
    configuration: LoadedProjectConfiguration | None = None
    effective_configuration: EffectiveProjectConfiguration | None = None
    source_files: tuple[DiscoveredSourceFile, ...] = ()
    macro_files: tuple[DiscoveredProjectFile, ...] = ()
    macro_registry: MacroRegistry | None = None

@final
@dataclass(frozen=True, repr=False)
class DiscoveredSourceFile:
    source_file: DiscoveredProjectFile
    sources: tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...]

@final
@dataclass(frozen=True, repr=False)
class DiscoveredPipelineDirectory:
    pipeline_dir: Path
    config_file: DiscoveredProjectFile | None = None

@final
@dataclass(frozen=True, repr=False)
class DiscoveredProjectInputs:
    project_dir: Path
    loaded_project: LoadedProject | None
    source_files: tuple[DiscoveredSourceFile, ...]
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...]
    model_files: tuple[DiscoveredProjectFile, ...]
    test_files: tuple[DiscoveredProjectFile, ...]
    audit_files: tuple[DiscoveredProjectFile, ...]
    macro_files: tuple[DiscoveredProjectFile, ...]
    access_file: DiscoveredProjectFile | None = None
    constant_files: tuple[DiscoveredProjectFile, ...] = ()
    enum_files: tuple[DiscoveredProjectFile, ...] = ()

# Neutral adapter identity, connection, catalog, metadata and result shapes.

@final
@dataclass(frozen=True)
class AdapterIdentity:
    name: str

@final
@dataclass(frozen=True)
class AdapterCapabilities:
    virtual_environments: bool
    managed_source_kinds: frozenset[str]
    replay_boundary_modes: frozenset[AdapterReplayBoundaryMode]
    history_prefix_seed: bool
    stable_logical_bindings: bool
    per_relation_atomic_replace: bool
    graph_atomic_publish: bool
    set_difference_comparison: bool
    direct_rebuild: bool

@final
@dataclass(frozen=True)
class AdapterReplayCoverageRange:
    driving_input_relation_name: str
    replay_boundary_mode: AdapterReplayBoundaryMode | str
    boundary_key: str
    source_partition_column_name: str | None
    source_position_column_name: str
    source_timestamp_column_name: str | None
    lower_value: str
    upper_value: str

@final
@dataclass(frozen=True)
class AdapterSetDifferenceTarget:
    name: str
    column_names: tuple[str, ...]
    ctes: tuple[tuple[str, str], ...]
    actual_query: str
    expected_query: str | None

@final
@dataclass(frozen=True)
class AdapterSetDifferenceComparisonRequest:
    targets: tuple[AdapterSetDifferenceTarget, ...]

@final
@dataclass(frozen=True)
class AdapterColumn:
    name: str
    type: str
    default_expression: str | None = None

@final
@dataclass(frozen=True)
class AdapterManagedSource:
    source_kind: str
    name: str
    columns: tuple[AdapterColumn, ...]
    broker_list: str
    topic: str
    consumer_group: str
    format: str
    settings: tuple[tuple[str, str], ...] = ()
    naming_macro_fingerprint: str | None = None

@final
@dataclass(frozen=True)
class AdapterTable:
    name: str
    columns: tuple[AdapterColumn, ...]
    engine: str
    order_by: tuple[str, ...]
    partition_by: str | None = None
    ttl: str | None = None
    settings: tuple[tuple[str, str], ...] = ()

@final
@dataclass(frozen=True)
class AdapterMaterializedView:
    name: str
    source_relation_name: str
    target_relation_name: str
    query: str
    database_template: str
    refresh: str | None = None
    append: bool = True
    dead_letters: bool = False

@final
@dataclass(frozen=True)
class AdapterView:
    name: str
    query: str
    database_template: str

@final
@dataclass(frozen=True)
class AdapterManagedSourceRealizationRequest:
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

@final
@dataclass(frozen=True)
class AdapterAdoptedSourceRealizationRequest:
    logical_name: str
    relation_name: str

@final
@dataclass(frozen=True)
class AdapterSourceRealization:
    relation_name: str
    resources: tuple[AdapterManagedSource | AdapterTable | AdapterMaterializedView, ...]

@final
@dataclass(frozen=True)
class AdapterRefreshState:
    view_name: str
    status: str
    last_refresh_at: str | None
    last_success_at: str | None
    next_refresh_at: str | None
    exception: str | None

@final
@dataclass(frozen=True)
class AdapterModelRealizationRequest:
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

@final
@dataclass(frozen=True)
class AdapterViewRealizationRequest:
    logical_name: str
    target_relation_name: str
    resolved_query: str
    resolved_database_template: str

@final
@dataclass(frozen=True)
class AdapterModelRealization:
    relation_name: str
    resources: tuple[AdapterTable | AdapterMaterializedView | AdapterView, ...]

@final
@dataclass(frozen=True)
class AdapterStableView:
    name: str
    target_relation_name: str

@final
@dataclass(frozen=True)
class AdapterStableBinding:
    database: str
    logical_name: str
    physical_name: str

@final
@dataclass(frozen=True)
class AdapterStableBindingRemoval:
    database: str
    logical_name: str

@final
@dataclass(frozen=True)
class AdapterBindingReplacementRequest:
    bindings: tuple[AdapterStableBinding, ...]
    removals: tuple[AdapterStableBindingRemoval, ...] = ()

@final
@dataclass(frozen=True)
class AdapterRelationCleanupRequest:
    database: str
    relation_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class AdapterTargetMutationLock:
    database: str
    owner_id: str

@final
@dataclass(frozen=True)
class AdapterReadinessRootRequest:
    database: str
    logical_name: str
    staged_relation_name: str
    active_exists: bool

@final
@dataclass(frozen=True)
class AdapterReadinessRequest:
    roots: tuple[AdapterReadinessRootRequest, ...]

@final
@dataclass(frozen=True)
class AdapterReadinessOffsetSummary:
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

@final
@dataclass(frozen=True)
class AdapterReadinessScalarSummary:
    active_min_value: str | None
    active_max_value: str | None
    staged_min_value: str | None
    staged_max_value: str | None
    lag_seconds: float | None

@final
@dataclass(frozen=True)
class AdapterReadinessRootObservation:
    root: AdapterReadinessRootRequest
    staged_exists: bool
    active_row_count: int | None
    staged_row_count: int | None
    replay_source_name: str | None
    replay_source_row_count: int | None
    replay_boundary_mode: AdapterReplayBoundaryMode | None
    offset_summary: AdapterReadinessOffsetSummary | None
    scalar_summary: AdapterReadinessScalarSummary | None

@final
@dataclass(frozen=True)
class AdapterPhysicalRelationMapping:
    logical_name: str
    physical_name: str

@final
@dataclass(frozen=True)
class AdapterReplayQuery:
    query: str
    physical_relation_mappings: tuple[AdapterPhysicalRelationMapping, ...]
    aggregate_semantics: bool

@final
@dataclass(frozen=True)
class AdapterReplayBoundary:
    boundary_key: str
    cutoff_value: str
    cutoff_inclusive: bool
    partition_value: str | None = None

@final
@dataclass(frozen=True)
class AdapterReplayColumns:
    partition: str
    offset: str
    timestamp: str
    landed_at: str
    cursor: str

@final
@dataclass(frozen=True)
class AdapterReplayWindow:
    lower_bound_mode: AdapterReplayLowerBoundMode
    lower_bound_inclusive: bool
    boundary_time: str
    forced_start_time: str | None
    lookback_seconds: int | None

@final
@dataclass(frozen=True)
class AdapterReplayRelations:
    root: str
    source: str
    anchor: str
    target: str

@final
@dataclass(frozen=True)
class AdapterReplayRequest:
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

@final
@dataclass(frozen=True)
class AdapterReplayCoverageRequest:
    replay: AdapterReplayRequest
    boundary_column_type: str | None
    count_anchor_rows: bool = False

@final
@dataclass(frozen=True)
class AdapterReplayLowerBound:
    value: str
    partition_value: str | None = None

@final
@dataclass(frozen=True)
class AdapterCapturedReplayRequest:
    replay: AdapterReplayRequest
    boundary_column_type: str | None
    lower_bounds: tuple[AdapterReplayLowerBound, ...]

@final
@dataclass(frozen=True)
class AdapterDirectFingerprintRecord:
    fingerprint_id: str
    logical_model_identity: str
    definition_sql: str
    definition_hash: str
    identity_metadata: str
    workflow_id: str
    tool_version: str
    applied_at: str | None = None

@final
@dataclass(frozen=True)
class AdapterDirectFingerprintSnapshot:
    status: AdapterOptionalStateStatus | str
    baselines: tuple[AdapterDirectFingerprintRecord, ...]
    warning: str | None = None

@final
@dataclass(frozen=True)
class AdapterManifestResource:
    pipeline_name: str
    logical_type: str
    logical_name: str
    resource_role: str
    resource_database: str
    resource_name: str
    resource_kind: str

@final
@dataclass(frozen=True)
class AdapterManifest:
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

@final
@dataclass(frozen=True)
class AdapterManifestSnapshot:
    status: AdapterOptionalStateStatus | str
    manifests: tuple[AdapterManifest, ...]
    warning: str | None = None

@final
@dataclass(frozen=True)
class AdapterDeploymentReplayRequest:
    replay: AdapterReplayRequest
    metadata_database: str
    deployment_id: str
    boundary_column_type: str | None
    active_relation_name: str
    active_column_names: tuple[str, ...]
    anchor_column_names: tuple[str, ...]

@final
@dataclass(frozen=True)
class AdapterMetadataObjectKey:
    database: str | None
    object_type: str
    name: str

@final
@dataclass(frozen=True)
class AdapterPreparedObjectMapping:
    logical_key: AdapterMetadataObjectKey
    physical_name: str
    logical_model_name: str

@final
@dataclass(frozen=True)
class AdapterObjectStateRecord:
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

@final
@dataclass(frozen=True)
class AdapterDeploymentRecord:
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

@final
@dataclass(frozen=True)
class AdapterDeploymentWatermarkRecord:
    deployment_id: str
    root_key: AdapterMetadataObjectKey
    anchor_key: AdapterMetadataObjectKey
    boundary_key: str
    cutoff_value: str
    lower_value: str | None = None
    cutoff_inclusive: bool = True
    captured_at: str = "1970-01-01 00:00:00.000"

@final
@dataclass(frozen=True)
class AdapterPublishEventRecord:
    deployment_id: str
    published_at: str
    logical_view_names: tuple[str, ...]
    publication_id: str = ""
    bindings: tuple[AdapterStableBinding, ...] = ()
    operation: str = "promote"
    previous_deployment_id: str | None = None

@final
@dataclass(frozen=True)
class AdapterInvocationRecord:
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

@final
@dataclass(frozen=True)
class AdapterNodeResultRecord:
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

@final
@dataclass(frozen=True)
class AdapterRunEventRecord:
    invocation_id: str
    sequence: int
    event_kind: str
    step_id: str | None
    phase: str | None
    payload_json: str

@final
@dataclass(frozen=True)
class AdapterRunStatementRecord:
    invocation_id: str
    statement_sequence: int
    step_id: str
    phase: str
    intent: str
    sql: str
    sql_sha256: str
    workflow_sha256: str
    workflow_revision: int

@final
@dataclass(frozen=True)
class AdapterSensorCheckpointRecord:
    sensor_name: str
    source: str
    position_completed_at: str
    position_result_id: str

@final
@dataclass(frozen=True)
class AdapterSensorTickRecord:
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

@final
@dataclass(frozen=True)
class AdapterSensorStepRecord:
    sensor_name: str
    event_id: str
    step_key: str
    policy: str
    status: str
    attempt: int
    result_json: str | None
    error_message: str | None

@final
@dataclass(frozen=True)
class AdapterSensorOverrideRecord:
    override_id: str
    sensor_name: str
    status: str
    actor: str | None

@final
@dataclass(frozen=True)
class AdapterSensorLeaseRecord:
    lease_name: str
    owner_id: str
    acquired_at: str
    expires_at: str

@final
@dataclass(frozen=True)
class AdapterSensorState:
    checkpoints: tuple[AdapterSensorCheckpointRecord, ...] = ()
    ticks: tuple[AdapterSensorTickRecord, ...] = ()
    steps: tuple[AdapterSensorStepRecord, ...] = ()
    overrides: tuple[AdapterSensorOverrideRecord, ...] = ()
    leases: tuple[AdapterSensorLeaseRecord, ...] = ()

@final
@dataclass(frozen=True)
class AdapterCurrentQualityNode:
    node_kind: str
    node_name: str
    binding_key: str
    definition_fingerprint: str
    execution_fingerprint: str
    cadence_seconds: int | None = None
    warmup_seconds: int = 0

@final
@dataclass(frozen=True)
class AdapterDeploymentInventory:
    deployments: tuple[AdapterDeploymentRecord, ...]
    publish_events: tuple[AdapterPublishEventRecord, ...]

@final
@dataclass(frozen=True)
class AdapterMetadataState:
    object_states: tuple[AdapterObjectStateRecord, ...]
    deployments: tuple[AdapterDeploymentRecord, ...]
    deployment_watermarks: tuple[AdapterDeploymentWatermarkRecord, ...]
    publish_events: tuple[AdapterPublishEventRecord, ...]
    invocations: tuple[AdapterInvocationRecord, ...] = ()
    node_results: tuple[AdapterNodeResultRecord, ...] = ()

@final
@dataclass(frozen=True)
class CatalogIdentity:
    adapter: AdapterIdentity
    database: str

@final
@dataclass(frozen=True)
class CatalogColumn:
    name: str
    type: str
    default_expression: str | None = None

@final
@dataclass(frozen=True)
class CatalogRelation:
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
    def storage_engine(self) -> str: ...
    @property
    def source_relation_name(self) -> str | None: ...

@final
@dataclass(frozen=True)
class CatalogSnapshot:
    identity: CatalogIdentity
    warehouse_timezone: str
    relations: tuple[CatalogRelation, ...]
    def relation(self, name: str) -> CatalogRelation | None: ...
    def relation_names(self) -> frozenset[str]: ...

@final
@dataclass(frozen=True)
class InspectedActiveTableBinding:
    database: str
    logical_name: str
    physical_name: str

@final
@dataclass(frozen=True)
class InspectedPhysicalTableCandidate:
    database: str
    logical_name: str
    physical_name: str
    object_type: str = "table"

@final
@dataclass(frozen=True)
class InspectedManagedTableState:
    active_bindings: tuple[InspectedActiveTableBinding, ...]
    physical_candidates: tuple[InspectedPhysicalTableCandidate, ...]

@final
@dataclass(frozen=True, repr=False)
class AdapterConnectionConfig:
    host: str
    port: int
    username: str
    password: str
    database: str | None = None
    settings: tuple[tuple[str, str], ...] = ()
    def __repr__(self) -> str: ...

@final
@dataclass(frozen=True)
class AdapterQueryResult:
    rows: tuple[tuple[object, ...], ...]
    column_names: tuple[str, ...] = ()
    def named_rows(self) -> tuple[Mapping[str, object], ...]: ...

@final
@dataclass(frozen=True)
class AdapterWarehouseDisk:
    name: str
    path: str | None
    disk_type: str | None
    total_bytes: int | None
    free_bytes: int | None
    unreserved_bytes: int | None
    keep_free_bytes: int | None
    status: AdapterWarehouseHealthStatus | str

@final
@dataclass(frozen=True)
class AdapterWarehouseMemory:
    resident_bytes: int | None
    host_total_bytes: int | None
    cgroup_used_bytes: int | None
    cgroup_limit_bytes: int | None
    basis: str
    pressure_fraction: float | None

@final
@dataclass(frozen=True)
class AdapterWarehouseActivity:
    active_queries: int | None
    active_merges: int | None
    incomplete_mutations: int | None

@final
@dataclass(frozen=True)
class AdapterWarehouseStuckKafkaConsumer:
    table: str
    stuck_seconds: int | None
    recent_errors: int
    latest_error: str

@final
@dataclass(frozen=True)
class AdapterWarehouseKafkaConsumers:
    configured_tables: int
    materialized_tables: int
    materialized_table_names: tuple[str, ...]
    polling_tables: int
    exception_tables: int
    stuck_consumers: tuple[AdapterWarehouseStuckKafkaConsumer, ...] = ()

@final
@dataclass(frozen=True)
class AdapterWarehouseDeadLetterModel:
    model_name: str
    row_count: int
    latest_failed_columns: tuple[str, ...]
    latest_payload: str
    latest_quarantined_at: str

@final
@dataclass(frozen=True)
class AdapterWarehouseTable:
    name: str
    rows: int | None
    bytes_on_disk: int | None
    active_parts: int | None

@final
@dataclass(frozen=True)
class AdapterWarehouseHealth:
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

@final
@dataclass(frozen=True)
class AdapterMutationResult:
    written_rows: int | None = None

@final
@dataclass(frozen=True)
class AdapterStatementProgress:
    elapsed_seconds: float
    read_rows: int
    read_bytes: int
    total_rows_approx: int
    memory_usage_bytes: int
    settings: tuple[tuple[str, str], ...] = ()

@final
@dataclass(frozen=True)
class AdapterQueryCancellation:
    query_id: str
    supported: bool
    query_found: bool
    termination_confirmed: bool
    detail: str | None = None

@final
@dataclass(frozen=True)
class AdapterReplayOffsetRange:
    partition: int
    lower_offset: int
    upper_offset: int

@final
@dataclass(frozen=True)
class AdapterReplayOffsetProgressRequest:
    database: str
    relation: str
    partition_column: str
    offset_column: str
    ranges: tuple[AdapterReplayOffsetRange, ...]

@final
@dataclass(frozen=True)
class AdapterReplayOffsetFrontier:
    partition: int
    completed_offset: int

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
def validate_sensor_retry_policy(*, max_attempts: int, backoff_seconds: float) -> None: ...
def provider_name(provider_class: type[Provider]) -> str: ...
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
def reset_fresh_landing_offsets(
    *,
    source_preparation: PopulationSourcePreparation,
    client_factory: KafkaAdminClientFactory | None = None,
) -> tuple[ConsumerGroupOffsetReset, ...]: ...
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

class ClickHouseConnectionCore:
    """A neutral adapter connection backed by the ClickHouse driver."""

    def __init__(self, raw_client: RawClickHouseClient) -> None: ...
    @property
    def adapter_identity(self) -> AdapterIdentity: ...
    @property
    def capabilities(self) -> AdapterCapabilities: ...
    def load_catalog(self, database: str) -> CatalogSnapshot: ...
    def load_relation_drop_size_limit(self) -> int | None: ...
    def load_relation_drop_size_server_default(self) -> int | None: ...
    def load_external_dependants(
        self, *, database: str, relation_names: tuple[str, ...]
    ) -> tuple[str, ...]: ...
    def load_refresh_states(self, database: str) -> tuple[AdapterRefreshState, ...]: ...
    def replay_anchor_has_repeated_offsets(
        self, *, database: str, relation: str, partition_column: str, offset_column: str
    ) -> bool: ...
    def load_warehouse_health(
        self,
        *,
        database: str,
        managed_source_names: tuple[str, ...] = (),
        include_dead_letters: bool = False,
    ) -> AdapterWarehouseHealth: ...
    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]: ...
    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState: ...
    def query(self, statement: str) -> AdapterQueryResult: ...
    def execute_workflow_query(
        self, *, statement: str, query_id: str | None
    ) -> AdapterQueryResult: ...
    def _query(self, *, statement: str, query_id: str | None) -> AdapterQueryResult: ...
    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult: ...
    def execute_workflow_mutation(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult: ...
    def _execute_workflow_sql(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult: ...
    def load_statement_progress(self, *, query_id: str) -> AdapterStatementProgress | None: ...
    def cancel_workflow_query(self, *, query_id: str) -> AdapterQueryCancellation: ...
    def load_replay_offset_frontiers(
        self, *, query_id: str, request: AdapterReplayOffsetProgressRequest
    ) -> tuple[AdapterReplayOffsetFrontier, ...]: ...
    def capture_warehouse_timestamp(self) -> str: ...
    def acquire_target_mutation_lock(
        self, *, database: str, owner_id: str
    ) -> AdapterTargetMutationLock: ...
    def release_target_mutation_lock(self, lock: AdapterTargetMutationLock) -> None: ...
    def render_ensure_database(self, database: str) -> str: ...
    def database_exists(self, database: str) -> bool: ...
    def render_resource(
        self,
        *,
        resource: AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView,
        database: str,
        if_not_exists: bool = False,
    ) -> str: ...
    def rename_streambuild_metadata(self, database: str) -> None: ...
    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]: ...
    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]: ...
    def render_terminal_observations(
        self,
        *,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> tuple[str, ...]: ...
    def render_latest_node_status_query(
        self,
        *,
        database: str,
        project_identity: str,
        target_identity: str,
        nodes: tuple[AdapterCurrentQualityNode, ...],
    ) -> str: ...
    def validate_metadata_state(self, database: str) -> None: ...
    def render_run_events(
        self,
        *,
        database: str,
        events: tuple[AdapterRunEventRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]: ...
    def render_run_statements(
        self,
        *,
        database: str,
        statements: tuple[AdapterRunStatementRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]: ...
    def render_sensor_state(
        self, *, database: str, state: AdapterSensorState, include_migration: bool = False
    ) -> tuple[str, ...]: ...
    def render_sensor_retention_cleanup(
        self, *, database: str, retention_days: int
    ) -> tuple[str, ...]: ...
    def load_direct_fingerprints(
        self, *, database: str, logical_model_identities: tuple[str, ...]
    ) -> AdapterDirectFingerprintSnapshot: ...
    def render_direct_fingerprint_observations(
        self, *, database: str, fingerprints: tuple[AdapterDirectFingerprintRecord, ...]
    ) -> tuple[str, ...]: ...
    def load_manifests(
        self, *, database: str, project_identity: str, target_name: str, target_database: str
    ) -> AdapterManifestSnapshot: ...
    def render_manifest_publication(
        self, *, database: str, manifest: AdapterManifest
    ) -> tuple[str, ...]: ...
    def catalog_resource_matches(
        self,
        *,
        resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
        relation: CatalogRelation,
        database: str,
    ) -> bool: ...
    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory: ...
    def render_replay_coverage_query(self, request: AdapterReplayCoverageRequest) -> str: ...
    def render_replay_from_capture(self, request: AdapterCapturedReplayRequest) -> str: ...
    def render_replay_from_deployment(
        self, request: AdapterDeploymentReplayRequest
    ) -> tuple[str, ...]: ...
    def compare_readiness(
        self, request: AdapterReadinessRequest
    ) -> tuple[AdapterReadinessRootObservation, ...]: ...
    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]: ...
    def render_restore_materialized_view(self, definition_sql: str) -> str: ...
    def render_cleanup_relations(
        self, request: AdapterRelationCleanupRequest
    ) -> tuple[str, ...]: ...
    def close(self) -> None: ...

class ClickHouseAdapterCore:
    """The built-in ClickHouse adapter."""

    @property
    def identity(self) -> AdapterIdentity: ...
    @property
    def sql_analysis_dialect(self) -> str: ...
    @property
    def default_database(self) -> str: ...
    @property
    def default_schema(self) -> None: ...
    def connect(self, config: AdapterConnectionConfig) -> ClickHouseConnection: ...
    def build_connection_config(
        self, *, values: Mapping[str, object], database: str | None
    ) -> AdapterConnectionConfig: ...
    def render_resource(
        self,
        *,
        resource: AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView,
        database: str,
        if_not_exists: bool = False,
    ) -> str: ...
    def realize_source(
        self,
        *,
        request: AdapterManagedSourceRealizationRequest | AdapterAdoptedSourceRealizationRequest,
    ) -> AdapterSourceRealization: ...
    def model_relation_name(self, *, logical_name: str) -> str: ...
    def realize_model(
        self, *, request: AdapterModelRealizationRequest | AdapterViewRealizationRequest
    ) -> AdapterModelRealization: ...
    def render_set_difference_comparison(
        self, *, request: AdapterSetDifferenceComparisonRequest
    ) -> str: ...
    def _open_raw_client(self, config: AdapterConnectionConfig) -> RawClickHouseClient: ...

def build_adapter_registry(adapters: tuple[Adapter, ...]) -> dict[str, Adapter]: ...
def builtin_adapters() -> tuple[Adapter, ...]: ...
def resolve_adapter(adapter_name: str) -> Adapter: ...

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

class UnreachableHostLogFilter:
    def __new__(cls) -> Self: ...
    def filter(self, record: logging.LogRecord) -> bool: ...

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

    def read(self, *, kafka: DiscoveryKafkaSettings, database: str) -> KafkaLagSnapshot | None: ...

class KafkaTopicSource(Protocol):
    """Cached topic inventory for one broker configuration."""

    def read(self, *, kafka: DiscoveryKafkaSettings) -> KafkaTopicsSnapshot | None: ...

def build_kafka_client_config(*, kafka: DiscoveryKafkaSettings) -> dict[str, object]: ...
def kafka_client_key(
    *, kafka: DiscoveryKafkaSettings
) -> tuple[str, tuple[tuple[str, str], ...]]: ...
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
    def read(self, *, kafka: DiscoveryKafkaSettings, database: str) -> KafkaLagSnapshot | None: ...
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
    def read(self, *, kafka: DiscoveryKafkaSettings) -> KafkaTopicsSnapshot | None: ...
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
