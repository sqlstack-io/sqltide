"""Assemble virtual build output from authoritative workflow evidence."""

import sqltide._native as _native
from sqltide.adapter.models import AdapterMutationResult, AdapterQueryResult
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.planner.models import DeploymentPlan
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.backfill.models import (
    BackfillBootstrapRequest,
    BackfillBootstrapResult,
    BackfillExecutionResult,
    BackfillRootReplayResult,
    RootBackfillReport,
)
from sqltide.executor.workflow.models import WorkflowExecutionResult, WorkflowStatementResult


def build_virtual_execution_result(
    *,
    request: BackfillBootstrapRequest,
    root_reports: tuple[RootBackfillReport, ...],
    existing_relation_names: frozenset[str],
    execution: WorkflowExecutionResult,
) -> BackfillExecutionResult:
    """Decode boundary and written-row evidence without further warehouse reads."""

    deployment_plan: DeploymentPlan = _deployment_plan(request)
    deployment_id: str = _deployment_id(request)
    created_at: str = _created_at(request)
    boundary_time: str = _boundary_time(execution)
    replay_results: tuple[BackfillRootReplayResult, ...] = tuple(
        BackfillRootReplayResult(
            root_key=subtree.root_key,
            written_rows=_root_written_rows(
                execution=execution,
                root_key=subtree.root_key,
            ),
        )
        for subtree in deployment_plan.rebuild_subtrees
        if subtree.replay_required
    )
    return BackfillExecutionResult(
        bootstrap=BackfillBootstrapResult(
            deployment_id=deployment_id,
            created_at=created_at,
            deployment_plan=deployment_plan,
            root_reports=root_reports,
            existing_relation_names=existing_relation_names,
        ),
        boundary_time=boundary_time,
        replay_results=replay_results,
    )


def _boundary_time(execution: WorkflowExecutionResult) -> str:
    result: WorkflowStatementResult = _statement_result(
        execution=execution,
        step_id="read_boundary_time",
    )
    query_result: AdapterQueryResult | None = result.query_result
    if query_result is None or not query_result.rows or not str(query_result.rows[0][0]):
        raise BackfillExecutionError("Virtual workflow returned no captured boundary time")
    return str(query_result.rows[0][0])


def _root_written_rows(*, execution: WorkflowExecutionResult, root_key: ObjectKey) -> int | None:
    segment: str = _native.workflow_step_segment(root_key.name)
    results: tuple[WorkflowStatementResult, ...] = tuple(
        result
        for result in execution.statement_results
        if result.step_id in {f"seed_{segment}", f"replay_{segment}"}
    )
    if not results:
        raise BackfillExecutionError(
            f"Virtual workflow did not execute replay root '{root_key.name}'"
        )
    mutation_results: tuple[AdapterMutationResult, ...] = tuple(
        result.mutation_result for result in results if result.mutation_result is not None
    )
    if len(mutation_results) != len(results) or any(
        result.written_rows is None for result in mutation_results
    ):
        return None
    return sum(result.written_rows or 0 for result in mutation_results)


def _statement_result(
    *, execution: WorkflowExecutionResult, step_id: str
) -> WorkflowStatementResult:
    result: WorkflowStatementResult
    for result in execution.statement_results:
        if result.step_id == step_id:
            return result
    raise BackfillExecutionError(f"Virtual workflow omitted required result '{step_id}'")


def _deployment_id(request: BackfillBootstrapRequest) -> str:
    if request.deployment_id is None or request.confirmed_plan is None:
        raise BackfillExecutionError("Virtual execution result requires a confirmed deployment")
    return request.deployment_id


def _deployment_plan(request: BackfillBootstrapRequest) -> DeploymentPlan:
    if request.confirmed_plan is None:
        raise BackfillExecutionError("Virtual execution result requires a confirmed plan")
    return request.confirmed_plan


def _created_at(request: BackfillBootstrapRequest) -> str:
    if request.created_at is None:
        raise BackfillExecutionError("Virtual execution result requires a creation timestamp")
    return request.created_at
