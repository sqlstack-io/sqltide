"""Immutable destruction requests, evidence, and plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqltide._native import RunEventSink
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.executor.destruction.exceptions import DestructionValidationError
from sqltide.executor.destruction.types import (
    DestructionOperation,
    DestructionOwnership,
    DestructionRelationKind,
)
from sqltide.executor.workflow.models import WarehouseStatement


@dataclass(frozen=True)
class DestructionRequest:
    operation: DestructionOperation | str
    target: str
    database: str
    metadata_database: str
    pipeline_names: tuple[str, ...] = ()
    included_dependent_pipeline_names: tuple[str, ...] = ()
    include_orphans: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", DestructionOperation(self.operation))
        if self.include_orphans and self.operation != DestructionOperation.DESTROY_PIPELINES:
            raise DestructionValidationError(
                "Orphan inclusion is only valid for selected pipeline destruction"
            )
        if len(set(self.pipeline_names)) != len(self.pipeline_names):
            raise DestructionValidationError(
                "Pipeline destruction selection contains duplicate names"
            )
        if len(set(self.included_dependent_pipeline_names)) != len(
            self.included_dependent_pipeline_names
        ):
            raise DestructionValidationError(
                "Included dependent pipeline selection contains duplicate names"
            )
        overlap: set[str] = set(self.pipeline_names) & set(self.included_dependent_pipeline_names)
        if overlap:
            raise DestructionValidationError(
                "Pipelines cannot be both originally selected and included dependants: "
                f"{tuple(sorted(overlap))!r}"
            )
        object.__setattr__(self, "pipeline_names", tuple(sorted(set(self.pipeline_names))))
        object.__setattr__(
            self,
            "included_dependent_pipeline_names",
            tuple(sorted(set(self.included_dependent_pipeline_names))),
        )


@dataclass(frozen=True)
class InactivePipeline:
    """A historically published pipeline absent from the compiled project."""

    name: str
    model_count: int
    resource_count: int
    last_published_at: str


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DestructionRelationKind(self.kind))


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
    def estimated_bytes(self) -> int:
        return sum(relation.total_bytes or 0 for relation in self.relations)


@dataclass(frozen=True)
class RelationalStoredDestructionPlan:
    """Validated database state associated with one immutable plan payload."""

    plan: DestructionPlan
    payload_json: str
    payload_sha256: str
    status: str
    reviewed_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True)
class DestructionExecutionResult:
    """Terminal recorded outcome for one consumed frozen plan."""

    invocation_id: str
    outcome: str
    completed_statement_sequences: tuple[int, ...]
    pending_statement_sequences: tuple[int, ...]
    remaining_relation_names: tuple[str, ...] | None
    error_message: str | None
    residual_catalog_status: str = "observed"
    residual_catalog_error: str | None = None


@dataclass(frozen=True)
class DestructionActor:
    """Authenticated actor identity recorded with destructive execution evidence."""

    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class DestructionRecordingContext:
    """Immutable identity and connections shared by destructive recording phases."""

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


@dataclass(frozen=True)
class DestructionPlanParts:
    """Internal values assembled before a frozen plan receives its fingerprint."""

    plan_id: str | None
    created_at: datetime
    ttl: timedelta
    requested_pipeline_names: tuple[str, ...]
    included_dependent_pipeline_names: tuple[str, ...]
    affected_pipeline_names: tuple[str, ...]
    affected_model_names: tuple[str, ...]
    affected_source_names: tuple[str, ...]
    relations: tuple[DestructionRelationEvidence, ...]
    challenges: tuple[str, ...]
    manifest_fingerprint: str
    relation_drop_size_limit: int | None
    relation_drop_size_server_limit: int | None
    relation_drop_size_override: int | None
    relation_drop_size_policy_observed: bool
    include_orphans: bool
