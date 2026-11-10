"""Confirmation gate and execution of one previewed direct build."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import sqltide._native as _native
from sqltide._native import row_error_policy_failure_message
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.cli.build._helpers.confirmation import confirm_build
from sqltide.cli.build._helpers.execution_artifacts import render_direct_execution_json
from sqltide.cli.build.models import BuildCommandOptions, DirectWorkflowPreparation
from sqltide.cli.workflow_artifacts.main._publish_build_workflow import publish_build_workflow
from sqltide.executor.direct.exceptions import DirectBuildError, DirectWorkflowExecutionError
from sqltide.executor.direct.main.build_direct_execution_result import (
    build_direct_execution_result,
)
from sqltide.executor.direct.main.execute_direct_build_workflow import (
    execute_direct_build_workflow,
)
from sqltide.executor.direct.main.persist_direct_fingerprints import (
    persist_direct_fingerprints,
)
from sqltide.executor.direct.main.plan_preserved_managed_sources import (
    plan_preserved_managed_sources,
)
from sqltide.executor.direct.models import (
    DirectBuildExecutionResult,
    DirectBuildRequest,
    DirectBuildWorkflow,
    DirectRuntimeExecution,
)
from sqltide.executor.kafka_admin.main.reset_fresh_landing_offsets import (
    reset_fresh_landing_offsets,
)
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock
from sqltide.executor.workflow.models import BuildWorkflow, WorkflowExecutionResult
from sqltide.executor.workflow.types import WorkflowEventEmitter


def execute_confirmed_direct_build(
    *,
    preparation: DirectWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
    confirmation_required: bool = True,
    _acquire_target_mutation_lock: bool = True,
) -> DirectBuildExecutionResult | None:
    """Show the destructive plan, require confirmation, then build."""

    if confirmation_required and not confirm_build(
        options=options,
        plan_text=preparation.plan_text,
        protection_requirements=preparation.protection_requirements,
    ):
        return None
    lock_context: AbstractContextManager[None] = (
        target_mutation_lock(
            connection=client,
            database=preparation.request.database,
            ensure_database=True,
        )
        if _acquire_target_mutation_lock
        else nullcontext()
    )
    with lock_context:
        _reject_new_out_of_scope_consumers(preparation=preparation, client=client)
        _reset_fresh_landing_offsets_for_direct_build(preparation=preparation)
        try:
            runtime_execution: DirectRuntimeExecution = execute_direct_build_workflow(
                workflow=preparation.workflow,
                connection=client,
                emitter=emitter,
            )
        except DirectWorkflowExecutionError as error:
            artifact_warning: str | None = _publish_direct_build_artifact(
                target_dir=options.pipelines_root.parent / "target",
                workflow=error.workflow,
                execution_json=render_direct_execution_json(
                    request=preparation.request,
                    status="cancelled" if isinstance(error.cause, KeyboardInterrupt) else "failed",
                    captures=error.captures,
                    execution=error.partial_result,
                    failed_step_id=error.failed_step_id,
                    error_message=str(error.cause),
                    audit_result=None,
                ),
            )
            _print_optional_warning(artifact_warning)
            applied_model_names: frozenset[str] = _applied_model_names(
                request=preparation.request,
                workflow=preparation.workflow,
                execution=error.partial_result,
            )
            fingerprint_warning: str | None = persist_direct_fingerprints(
                request=preparation.request,
                connection=client,
                applied_model_names=applied_model_names,
            )
            _print_optional_warning(fingerprint_warning)
            policy_failure: str | None = row_error_policy_failure_message(
                changes=preparation.request.row_error_policy_changes,
                failed_step_id=error.failed_step_id,
                cause=error.cause,
            )
            if preparation.request.row_error_policy_changes and isinstance(
                error.cause, KeyboardInterrupt
            ):
                _print_optional_warning("\n".join(getattr(error.cause, "__notes__", ())) or None)
            if policy_failure is not None and isinstance(error.cause, AdapterError):
                raise AdapterError(policy_failure) from error.cause
            raise error.cause from error
        result: DirectBuildExecutionResult = build_direct_execution_result(
            request=preparation.request,
            execution=runtime_execution.execution,
            captures=runtime_execution.captures,
        )
        artifact_warning = _publish_direct_build_artifact(
            target_dir=options.pipelines_root.parent / "target",
            workflow=runtime_execution.workflow,
            execution_json=render_direct_execution_json(
                request=preparation.request,
                status="failed" if result.audit_result.error_failure_count else "succeeded",
                captures=runtime_execution.captures,
                execution=runtime_execution.execution,
                failed_step_id=None,
                error_message=None,
                audit_result=result.audit_result,
            ),
        )
        _print_optional_warning(artifact_warning)
        fingerprint_warning = persist_direct_fingerprints(
            request=preparation.request,
            connection=client,
        )
        _print_optional_warning(fingerprint_warning)
        return result


def _reject_new_out_of_scope_consumers(
    *, preparation: DirectWorkflowPreparation, client: AdapterConnection
) -> None:
    if not preparation.request.plan.prerequisite_execution_scope:
        return
    consumers: tuple[str, ...] = _native.find_direct_out_of_scope_consumers(
        realized_project=preparation.request.realized_project,
        catalog=client.load_catalog(preparation.request.database),
        prerequisite_execution_scope=preparation.request.plan.prerequisite_execution_scope,
        execution_scope=preparation.request.plan.execution_scope,
    )
    if consumers:
        raise DirectBuildError(
            "Direct build stopped because out-of-scope materialized views now consume rebuilt "
            f"upstream models: {', '.join(consumers)}"
        )


def _publish_direct_build_artifact(
    *, target_dir: Path, workflow: BuildWorkflow, execution_json: str
) -> str | None:
    try:
        _ = publish_build_workflow(
            target_dir=target_dir,
            workflow=workflow,
            execution_json=execution_json,
        )
    except OSError as error:
        return f"Direct build artifact was not recorded: {error}"
    return None


def _applied_model_names(
    *,
    request: DirectBuildRequest,
    workflow: DirectBuildWorkflow,
    execution: WorkflowExecutionResult,
) -> frozenset[str]:
    completed_step_ids: frozenset[str] = frozenset(
        result.step_id for result in execution.statement_results
    )
    replayed_root_names: frozenset[str] = frozenset(
        runtime.model_name
        for runtime in workflow.runtime_replays
        if runtime.replay_step_id in completed_step_ids
    )
    applied_model_names: set[str] = set()
    for replay_root in request.plan.replay_roots:
        if replay_root.model_key.name in replayed_root_names:
            applied_model_names.update(
                propagated_key.name for propagated_key in replay_root.propagated_model_keys
            )
    incomplete_runtime_models: set[str] = {
        runtime.model_name
        for runtime in workflow.runtime_replays
        if runtime.replay_step_id not in completed_step_ids
    }
    applied_model_names.difference_update(incomplete_runtime_models)
    return frozenset(applied_model_names)


def _reset_fresh_landing_offsets_for_direct_build(
    *, preparation: DirectWorkflowPreparation
) -> None:
    """Fresh landing tables consume from earliest: stale committed offsets are orphans."""

    source_preparation, _source_realizations = plan_preserved_managed_sources(
        plan=preparation.request.plan,
        realized_project=preparation.preview.analysis.realized_project,
        catalog=preparation.preview.warehouse_snapshot.catalog,
        database=preparation.preview.database,
    )
    for reset in reset_fresh_landing_offsets(
        source_preparation=source_preparation,
    ):
        _print_optional_warning(reset.notice)


def _print_optional_warning(warning: str | None) -> None:
    if warning is not None:
        try:
            print(warning, file=sys.stderr)
        except Exception:
            return
