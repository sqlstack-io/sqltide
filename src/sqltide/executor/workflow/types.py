"""Workflow ordering and execution intent types."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqltide.executor.workflow.models import WarehouseStatement


type NativeStatement = tuple[str, str, str, str, bool]


class WorkflowMode(StrEnum):
    DIRECT = "direct"
    VIRTUAL_ENVIRONMENT = "virtual_environment"


class WorkflowPhase(StrEnum):
    PREFLIGHT = "preflight"
    PREPARATION = "preparation"
    TEARDOWN = "teardown"
    REALIZATION = "realization"
    STABILIZATION = "stabilization"
    BOUNDARY = "boundary"
    REPLAY = "replay"
    AUDIT = "audit"
    FINALIZATION = "finalization"


class StatementIntent(StrEnum):
    ASSERTION = "assertion"
    QUERY = "query"
    MUTATION = "mutation"
    WAIT = "wait"


class WorkflowEventEmitter(Protocol):
    """Receives step-granular progress while a warehouse workflow executes."""

    def workflow_prepared(
        self, *, statements: tuple[WarehouseStatement, ...], workflow_sha256: str
    ) -> None:
        """Persist the complete immutable workflow before execution begins."""

    def statement_started(self, statement: WarehouseStatement) -> str | None:
        """One statement is about to execute; optionally return its warehouse query ID."""

    def statement_completed(
        self,
        *,
        statement: WarehouseStatement,
        error_message: str | None,
        written_rows: int | None,
        elapsed_ms: int,
    ) -> None:
        """One statement finished, successfully or not."""
