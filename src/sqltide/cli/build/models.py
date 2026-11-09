"""Options and prepared contexts for the mode-aware build command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqltide.adapter.models import CatalogSnapshot
from sqltide.compiler.compile.models import DesiredState, LogicalResourceKey, ObjectKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    DirectPlan,
    DirectRowErrorPolicyChange,
    DirectWarehouseSnapshot,
)
from sqltide.executor.backfill.models import BackfillBootstrapRequest, RootBackfillReport
from sqltide.executor.direct.models import DirectBuildRequest, DirectBuildWorkflow
from sqltide.executor.workflow.models import BuildWorkflow


@dataclass(frozen=True)
class BuildCommandOptions:
    """Every operator-supplied option for one `sqt build` invocation."""

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


@dataclass(frozen=True)
class BuildProtectionRequirement:
    """One protected pipeline touched by the resolved build closure."""

    pipeline_name: str
    warning: str
    confirmation: str


@dataclass(frozen=True)
class WorkflowPreparationOptions:
    """Connected planning options shared by `sqt plan` and `sqt build`."""

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
    """The plan and resolved databases a build renders before it writes anything."""

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
    """The confirmed virtual deployment plan and its fixed execution identity."""

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


@dataclass(frozen=True)
class DirectWorkflowPreparation:
    """One fully assembled direct workflow and its result-decoding request."""

    preview: DirectBuildPreviewContext
    request: DirectBuildRequest
    workflow: DirectBuildWorkflow
    plan_text: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()


@dataclass(frozen=True)
class VirtualWorkflowPreparation:
    """One fully assembled virtual workflow and its result-decoding request."""

    preview: VirtualBuildPreviewContext
    request: BackfillBootstrapRequest
    workflow: BuildWorkflow
    plan_text: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()


@dataclass(frozen=True)
class MixedWorkflowPreparation:
    """Virtual-first and direct-second workflows prepared for one build."""

    virtual: VirtualWorkflowPreparation
    direct: DirectWorkflowPreparation
    plan_text: str
    plan_json: str
    protection_requirements: tuple[BuildProtectionRequirement, ...] = ()
