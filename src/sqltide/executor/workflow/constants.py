"""Stable workflow validation constants."""

from sqltide.executor.workflow.types import WorkflowPhase

WORKFLOW_PHASE_ORDER: tuple[WorkflowPhase, ...] = (
    WorkflowPhase.PREFLIGHT,
    WorkflowPhase.PREPARATION,
    WorkflowPhase.TEARDOWN,
    WorkflowPhase.REALIZATION,
    WorkflowPhase.STABILIZATION,
    WorkflowPhase.BOUNDARY,
    WorkflowPhase.REPLAY,
    WorkflowPhase.AUDIT,
    WorkflowPhase.FINALIZATION,
)

RECOVER_MODEL_VIEW_STEP_PREFIX: str = "recover_view_"
RECOVER_SOURCE_STEP_PREFIX: str = "recover_source_"
