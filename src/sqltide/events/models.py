"""Event envelopes and the persisted observation rows they derive from."""

from __future__ import annotations

from dataclasses import dataclass

from sqltide._native import InvocationObservation as InvocationObservation
from sqltide._native import NodeResultObservation as NodeResultObservation
from sqltide.events.types import AuditTransition, ObservedCommand
from sqltide.executor.auditing.types import QualityResultStatus


@dataclass(frozen=True)
class AuditCompleted:
    """One audit attempt reached a terminal status."""

    id: str
    audit_name: str
    status: QualityResultStatus
    previous_status: QualityResultStatus | None
    transition: AuditTransition
    severity: str | None
    failure_count: int
    target: str
    trigger: str
    completed_at: str
    binding_key: str
    invocation_id: str
    scheduled_for: str | None
    error_message: str | None
    sample_column_names: tuple[str, ...] = ()
    sample_rows: tuple[tuple[object, ...], ...] = ()


@dataclass(frozen=True)
class RunCompleted:
    """One recorded command invocation reached a terminal outcome."""

    id: str
    command: ObservedCommand
    mode: str | None
    outcome: str
    exit_code: int
    target: str
    deployment_id: str | None
    selected_node_count: int
    error_message: str | None
    completed_at: str
