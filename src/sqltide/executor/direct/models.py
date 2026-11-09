"""Immutable request and result contracts for direct-mode builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqltide.adapter.models import AdapterReplayRequest
from sqltide.compiler.compile.models import LogicalResourceKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import DirectPlan, DirectRowErrorPolicyChange
from sqltide.executor.auditing.models import SqlAuditRunResult
from sqltide.executor.population.models import PopulationSourceRecreation
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    WarehouseStatement,
    WorkflowExecutionResult,
)
from sqltide.executor.workflow.types import WorkflowMode


@dataclass(frozen=True)
class DirectBuildRequest:
    """One complete instruction to realize a planned direct closure."""

    plan: DirectPlan
    realized_project: RealizedProject
    database: str
    metadata_database: str
    tool_version: str
    workflow_id: str = field(default_factory=lambda: str(uuid4()))
    stabilization_seconds: float = 5.0
    boundary_time: str | None = None
    audits: tuple[DirectBuildAudit, ...] = ()
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = ()

    @property
    def effective_start_time(self) -> str | None:
        """Return the normalized lower bound carried by the confirmed plan."""

        return self.plan.effective_start_time


@dataclass(frozen=True)
class DriftedManagedSource:
    """One deployed managed source whose Kafka settings differ from the project declaration."""

    source_key: LogicalResourceKey
    owner_pipeline_names: tuple[str, ...]
    drifted_setting_names: tuple[str, ...]


@dataclass(frozen=True)
class DirectBuildAudit:
    """One selected direct audit with refs resolved before workflow assembly."""

    name: str
    query: str
    severity: str
    description: str | None


@dataclass(frozen=True)
class DirectReplayRange:
    """One retained interval captured for a direct replay root."""

    partition_value: str | None
    source_partition_column_name: str | None
    source_position_column_name: str
    source_timestamp_column_name: str | None
    lower_value: str
    upper_value: str
    replay_cutoff_value: str
    cutoff_inclusive: bool
    anchor_row_count: int | None = None


@dataclass(frozen=True)
class DirectReplayCapture:
    """Process-owned replay boundaries captured after live stabilization."""

    capture_id: str
    workflow_id: str
    target_database: str
    logical_model_name: str
    driving_input_relation_name: str
    boundary_mode: ReplayLineageMode | str
    captured_at: str | None
    ranges: tuple[DirectReplayRange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "boundary_mode", ReplayLineageMode(self.boundary_mode))


@dataclass(frozen=True)
class DirectRuntimeReplay:
    """One replay template and the steps that capture and consume its boundary."""

    model_name: str
    capture_step_id: str
    replay_step_id: str
    replay: AdapterReplayRequest
    boundary_column_type: str | None
    managed_landing_anchor: bool = False


@dataclass(frozen=True)
class DirectBuildWorkflow:
    """A direct workflow template plus its runtime replay realization inputs."""

    template: BuildWorkflow
    runtime_replays: tuple[DirectRuntimeReplay, ...]
    workflow_id: str
    source_recovery_statements: tuple[WarehouseStatement, ...] = ()
    recreated_sources: tuple[PopulationSourceRecreation, ...] = ()

    @property
    def mode(self) -> WorkflowMode:
        return self.template.mode

    @property
    def plan_json(self) -> str:
        return self.template.plan_json

    @property
    def statements(self) -> tuple[WarehouseStatement, ...]:
        return self.template.statements


@dataclass(frozen=True)
class DirectRuntimeExecution:
    """Exact direct workflow and typed captures produced by one runtime execution."""

    workflow: BuildWorkflow
    execution: WorkflowExecutionResult
    captures: tuple[DirectReplayCapture, ...]


@dataclass(frozen=True)
class DirectReplayBoundary:
    """One partition-scoped or scalar cutoff separating replay from live propagation."""

    model_name: str
    driving_input_relation_name: str
    replay_boundary_mode: ReplayLineageMode | str
    boundary_key: str
    cutoff_value: str
    cutoff_inclusive: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "replay_boundary_mode", ReplayLineageMode(self.replay_boundary_mode)
        )


@dataclass(frozen=True)
class DirectRootReplayResult:
    """One direct replay root actually executed in the warehouse."""

    model_name: str
    written_rows: int | None


@dataclass(frozen=True)
class DirectBuildResult:
    """Everything one direct build durably changed, in execution order."""

    database: str
    preserved_source_relation_names: tuple[str, ...]
    created_source_relation_names: tuple[str, ...]
    dropped_relation_names: tuple[str, ...]
    created_relation_names: tuple[str, ...]
    boundary_time: str
    boundaries: tuple[DirectReplayBoundary, ...]
    replay_results: tuple[DirectRootReplayResult, ...]
    effective_start_time: str | None = None


@dataclass(frozen=True)
class DirectBuildExecutionResult:
    """Direct build and audit evidence decoded from one workflow execution."""

    build_result: DirectBuildResult
    audit_result: SqlAuditRunResult
