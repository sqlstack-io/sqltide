"""Backfill executor runtime models."""

from __future__ import annotations

from dataclasses import dataclass

from sqltide.adapter.models import CatalogSnapshot
from sqltide.compiler.compile.models import DesiredState, ObjectKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.models import DeploymentPlan, DirectRowErrorPolicyChange


@dataclass(frozen=True)
class BackfillBootstrapRequest:
    """Input required to bootstrap a staged backfill deployment."""

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "replay_lineage_mode", ReplayLineageMode(self.replay_lineage_mode))


@dataclass(frozen=True)
class BackfillBootstrapResult:
    """Result of bootstrapping a staged backfill deployment."""

    deployment_id: str
    created_at: str
    deployment_plan: DeploymentPlan
    root_reports: tuple[RootBackfillReport, ...]
    existing_relation_names: frozenset[str]


@dataclass(frozen=True)
class BackfillRootReplayResult:
    """One staged replay root actually executed in the warehouse."""

    root_key: ObjectKey
    written_rows: int | None


@dataclass(frozen=True)
class BackfillExecutionResult:
    """Result of executing a staged backfill through boundary capture and replay."""

    bootstrap: BackfillBootstrapResult
    boundary_time: str
    replay_results: tuple[BackfillRootReplayResult, ...]


@dataclass(frozen=True)
class BackfillDeploymentIdentity:
    """Deployment identity fixed before a virtual build is confirmed."""

    deployment_id: str
    created_at: str


@dataclass(frozen=True)
class RootBackfillReport:
    """User-facing rebuild strategy report for one managed root."""

    root_key: ObjectKey
    state_kind: str
    replay_strategy: str
    active_deployment_id: str | None
