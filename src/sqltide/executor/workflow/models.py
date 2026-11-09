"""Immutable workflow values and execution results."""

from dataclasses import dataclass
from pathlib import Path

from sqltide.adapter.models import (
    AdapterMutationResult,
    AdapterQueryResult,
    AdapterReplayOffsetProgressRequest,
)
from sqltide.executor.workflow.constants import WORKFLOW_PHASE_ORDER
from sqltide.executor.workflow.exceptions import WorkflowValidationError
from sqltide.executor.workflow.types import StatementIntent, WorkflowMode, WorkflowPhase


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

    def __post_init__(self) -> None:
        if not self.step_id or not self.step_id.replace("_", "").isalnum():
            raise WorkflowValidationError(
                "Workflow step IDs may contain only letters, numbers, and underscores"
            )
        if not self.sql.endswith(";") or self.sql.endswith(";;"):
            raise WorkflowValidationError(
                f"Workflow statement {self.step_id!r} must end with exactly one semicolon"
            )


@dataclass(frozen=True)
class BuildWorkflow:
    mode: WorkflowMode
    plan_json: str
    statements: tuple[WarehouseStatement, ...]
    recovery_statements: tuple[WarehouseStatement, ...] = ()

    def __post_init__(self) -> None:
        expected_sequence: int = 1
        previous_phase_index: int = 0
        statement: WarehouseStatement
        for statement in self.statements:
            if statement.sequence != expected_sequence:
                raise WorkflowValidationError(
                    "Workflow statement sequences must be continuous from one"
                )
            phase_index: int = WORKFLOW_PHASE_ORDER.index(statement.phase)
            if phase_index < previous_phase_index:
                raise WorkflowValidationError("Workflow phases must be monotonic")
            expected_sequence += 1
            previous_phase_index = phase_index


@dataclass(frozen=True)
class PublishedBuildWorkflow:
    workflow: BuildWorkflow
    artifact_root: Path
    workflow_sha256: str


@dataclass(frozen=True)
class WorkflowStatementResult:
    step_id: str
    query_result: AdapterQueryResult | None
    mutation_result: AdapterMutationResult | None
    error_message: str | None = None


@dataclass(frozen=True)
class WorkflowExecutionResult:
    statement_results: tuple[WorkflowStatementResult, ...]
