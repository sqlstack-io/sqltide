"""Compile-specific runtime models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from sqltide.adapter.models import AdapterIdentity
from sqltide.adapter.types import (
    AdapterModelRealizer,
    AdapterModelRelationNamer,
    AdapterResourceRenderer,
    AdapterSetDifferenceComparisonRenderer,
    AdapterSourceRealizer,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.types import (
    DesiredObjectType,
    LogicalResourceType,
    RetentionOrigin,
)
from sqltide.compiler.discovery.models import (
    AllowedCrossPipelineReference,
    DiscoveredProjectInputs,
    ExternalTableSourceStep,
    KafkaLandingStep,
    LoadedPipeline,
    ModelRetentionPolicy,
    Pipeline,
    PostgresRefreshSourceStep,
    Project,
    ReplayOnChangePolicy,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    ModelKind,
    ModelReferenceScope,
    RefType,
    ReplayBoundaryMode,
    ReplayLineageMode,
    SourceKind,
    SqlRelationType,
)
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.sql_analysis.models import SqlModelAnalysis, SqlSourceSpan
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase


@dataclass(frozen=True)
class ObjectKey:
    """Stable identity for a comparable deployed object."""

    database: str | None
    object_type: DesiredObjectType | str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_type", DesiredObjectType(self.object_type))


@dataclass(frozen=True)
class Column:
    """A normalized comparable column definition."""

    name: str
    type: str
    default: str | None = None


@dataclass(frozen=True)
class KafkaSettings:
    """Normalized comparable Kafka engine settings."""

    broker_list: str
    topic: str
    consumer_group: str
    format: str
    settings: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.settings is not None:
            object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))


@dataclass(frozen=True)
class KafkaTableSpec:
    """Comparable Kafka table specification."""

    columns: tuple[Column, ...]
    kafka: KafkaSettings
    naming_macro_fingerprint: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TableStorage:
    """Comparable managed-table storage definition."""

    engine: str
    order_by: tuple[str, ...]
    partition_by: str | None = None
    ttl: str | None = None
    settings: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.settings is not None:
            object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))


@dataclass(frozen=True)
class TableSpec:
    """Comparable managed-table specification."""

    columns: tuple[Column, ...]
    storage: TableStorage


@dataclass(frozen=True)
class MaterializedViewSpec:
    """Comparable materialized-view specification."""

    source_table_name: str
    target_table_name: str
    query: str
    database_template: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ViewSpec:
    """Comparable ordinary-view specification."""

    query: str
    database_template: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class DesiredKafkaTable:
    """A desired Kafka engine table for a landing step."""

    key: ObjectKey
    deps: tuple[ObjectKey, ...]
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
class DesiredTable:
    """A desired managed ClickHouse table."""

    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: TableSpec
    logical_model_name: str | None = field(default=None, compare=False)
    replay_on_change: ReplayOnChangePolicy | None = None
    bounded_replay_fallback: BoundedReplayFallback = BoundedReplayFallback(
        BoundedReplayFallback.FULL
    )
    replay_settings: Mapping[str, str] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.replay_settings is not None:
            object.__setattr__(
                self, "replay_settings", MappingProxyType(dict(self.replay_settings))
            )

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
class DesiredMaterializedView:
    """A desired materialized view between a source and target relation."""

    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: MaterializedViewSpec
    logical_model_name: str | None = field(default=None, compare=False)
    refresh: str | None = field(default=None, compare=False)
    append: bool = field(default=True, compare=False)

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

    @property
    def database_template(self) -> str | None:
        return self.spec.database_template


@dataclass(frozen=True)
class DesiredView:
    """A desired ordinary query view."""

    key: ObjectKey
    deps: tuple[ObjectKey, ...]
    spec: ViewSpec
    logical_model_name: str | None = field(default=None, compare=False)

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def query(self) -> str:
        return self.spec.query

    @property
    def database_template(self) -> str | None:
        return self.spec.database_template


@dataclass(frozen=True)
class ParsedRef:
    """A parsed logical `__source(...)` or `__ref(...)` occurrence from transform SQL."""

    name: str
    relation_type: SqlRelationType | str
    ref_type: RefType | None = None
    span: SqlSourceSpan | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_type", SqlRelationType(self.relation_type))


@dataclass(frozen=True)
class LogicalResourceKey:
    """Stable identity for one selectable logical source or model."""

    resource_type: LogicalResourceType | str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_type", LogicalResourceType(self.resource_type))


@dataclass(frozen=True)
class CompiledSource:
    """One semantically compiled logical streaming source."""

    key: LogicalResourceKey
    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    effective_replay_lineage_mode: ReplayLineageMode


@dataclass(frozen=True)
class ModelRetentionResolution:
    """Effective typed retention choice and its authored scope."""

    value: ModelRetentionPolicy | Literal[False] | None = None
    origin: RetentionOrigin | str | None = None

    def __post_init__(self) -> None:
        if self.origin is not None:
            object.__setattr__(self, "origin", RetentionOrigin(self.origin))


@dataclass(frozen=True)
class CompiledModel:
    """One semantically compiled logical model."""

    key: LogicalResourceKey
    pipeline_name: str
    relation_name: str
    kind: ModelKind | str
    sql_analysis: SqlModelAnalysis

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ModelKind(self.kind))

    @property
    def query(self) -> str:
        return self.sql_analysis.authored_sql

    @property
    def output_columns(self) -> tuple[Column, ...]:
        return tuple(
            Column(name=column.name, type=column.type)
            for column in self.sql_analysis.output_columns
        )

    @property
    def parsed_refs(self) -> tuple[ParsedRef, ...]:
        return tuple(
            ParsedRef(
                name=reference.name,
                relation_type=SqlRelationType(reference.relation_type),
                ref_type=None if reference.ref_type is None else RefType(reference.ref_type),
                span=reference.span,
            )
            for reference in self.sql_analysis.references
        )

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(reference.name for reference in self.sql_analysis.references)

    @property
    def has_aggregate_semantics(self) -> bool:
        return self.sql_analysis.aggregate_facts.has_semantics


@dataclass(frozen=True)
class CompiledTableModel(CompiledModel):
    """One compiled streaming table model and its replay semantics."""

    transform: TransformStep
    preserves_required_lineage: bool
    replay_anchor_eligible: bool
    effective_bounded_replay_fallback: BoundedReplayFallback
    replay_on_change: ReplayOnChangePolicy | None = None
    retention: ModelRetentionResolution = field(default_factory=ModelRetentionResolution)
    retention_applied: bool = False

    @property
    def has_mutable_refs(self) -> bool:
        return any(
            reference.relation_type == SqlRelationType.REF
            and reference.name != self.transform.source
            and reference.ref_type == RefType.MUTABLE
            for reference in self.sql_analysis.references
        )


@dataclass(frozen=True)
class CompiledViewModel(CompiledModel):
    """One compiled query-only terminal view model."""

    view: ViewStep


@dataclass(frozen=True)
class ExternalSourceReplayConfig:
    """Replay metadata for one adopted external source root."""

    key: ObjectKey
    table_name: str
    source_kind: SourceKind | str
    replay_boundary_mode: ReplayBoundaryMode | str
    partition_column_name: str | None = None
    offset_column_name: str | None = None
    timestamp_column_name: str | None = None
    landed_at_column_name: str | None = None
    cursor_column_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(
            self, "replay_boundary_mode", ReplayBoundaryMode(self.replay_boundary_mode)
        )


@dataclass(frozen=True)
class CompiledPipeline:
    """Logical sources and models compiled from one authored pipeline."""

    pipeline: Pipeline
    project: Project | None
    file_path: Path
    effective_replay_lineage_mode: ReplayLineageMode | None
    source: CompiledSource | None
    models: tuple[CompiledModel, ...]


@dataclass(frozen=True)
class DesiredState:
    """Project-level flat desired object graph."""

    objects: tuple[DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView, ...]
    replay_anchor_keys: frozenset[ObjectKey]
    mutable_ref_warning_keys: frozenset[ObjectKey]
    external_source_replay_configs: tuple[ExternalSourceReplayConfig, ...] = ()


@dataclass(frozen=True)
class CompilerExpressionInferenceProfile:
    """Static SQL expression inference behavior exposed by an adapter."""

    sql_analysis_dialect: str


@dataclass(frozen=True)
class CompilerTargetMetadata:
    """Connection-free adapter defaults used during semantic compilation."""

    default_database: str | None
    default_schema: str | None
    production_target: bool = False
    destruction_relation_drop_size_limit: int | None = None


@dataclass(frozen=True)
class CompilerAdapterProfile:
    """Immutable compiler-facing adapter identity, analysis, target, and realization contract."""

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


@dataclass(frozen=True)
class CompileProjectInputs:
    """Attached whole-project inputs used by semantic assembly."""

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "model_reference_scope",
            ModelReferenceScope(self.model_reference_scope),
        )
        object.__setattr__(
            self,
            "allowed_cross_pipeline_references",
            tuple(self.allowed_cross_pipeline_references),
        )


@dataclass(frozen=True)
class CompiledProject:
    """Current whole-project compile output consumed by every command."""

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "model_reference_scope",
            ModelReferenceScope(self.model_reference_scope),
        )
        object.__setattr__(
            self,
            "allowed_cross_pipeline_references",
            tuple(self.allowed_cross_pipeline_references),
        )
