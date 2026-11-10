"""Execute one confirmed virtual-environment build command."""

import sys
from contextlib import AbstractContextManager, nullcontext

from sqltide._native import plan_population_sources, row_error_policy_failure_message
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterInvocationRecord
from sqltide.cli.build._helpers.confirmation import confirm_build
from sqltide.cli.build._helpers.rendering import render_interrupted_build_message
from sqltide.cli.build.main.render_virtual_build_result import render_virtual_build_result
from sqltide.cli.build.models import BuildCommandOptions, VirtualWorkflowPreparation
from sqltide.cli.workflow_artifacts.main._publish_build_workflow import publish_build_workflow
from sqltide.executor.backfill.main.build_virtual_execution_result import (
    build_virtual_execution_result,
)
from sqltide.executor.backfill.models import BackfillExecutionResult
from sqltide.executor.kafka_admin.main.reset_fresh_landing_offsets import (
    reset_fresh_landing_offsets,
)
from sqltide.executor.observability.classes.run_event_sink import RunEventSink
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)
from sqltide.executor.observability.main.logical_resource_identities import (
    logical_resource_identities,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.models import RunStartupTimings, TerminalInvocation
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.main.execute_build_workflow import execute_build_workflow
from sqltide.executor.workflow.main.execute_recovery_statements import (
    execute_recovery_statements,
)
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock
from sqltide.executor.workflow.models import PublishedBuildWorkflow, WorkflowExecutionResult

_SIGINT_EXIT_CODE: int = 130


def execute_virtual_build_command(
    *,
    preparation: VirtualWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    confirmation_required: bool = True,
    startup_timings: RunStartupTimings | None = None,
    _acquire_target_mutation_lock: bool = True,
) -> int:
    """Confirm and populate one prepared isolated virtual deployment."""

    sink: RunEventSink = RunEventSink(
        connection=observation_client,
        database=preparation.preview.metadata_database,
        invocation_id=started[0],
        project_identity=logical_project_identity(project_dir=options.pipelines_root.parent),
        jsonl_stream=sys.stdout if options.events_output else None,
    )
    try:
        if sink is not None:
            sink.run_started(
                command="build",
                mode="virtual_environment",
                total_statements=len(preparation.workflow.statements),
                selected_node_count=len(preparation.preview.run_execution_scope),
                selectors=options.selectors,
                start_time=options.start_time,
                executed_logical_ids=logical_resource_identities(
                    preparation.preview.run_execution_scope
                ),
                context_logical_ids=logical_resource_identities(
                    preparation.preview.run_context_scope
                ),
                startup_timings=startup_timings,
            )
        confirmed: bool = (
            confirm_build(
                options=options,
                plan_text=preparation.plan_text,
                protection_requirements=preparation.protection_requirements,
            )
            if confirmation_required
            else True
        )
    except KeyboardInterrupt:
        return _cancel_virtual_build(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            sink=sink,
        )
    if not confirmed:
        _persist_virtual_invocation(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            outcome="cancelled",
            exit_code=1,
            materialized_outcome=None,
            error_message=None,
        )
        if sink is not None:
            sink.run_completed(outcome="cancelled", exit_code=1, error_message=None)
        print("Build cancelled.")
        return 1
    try:
        published_workflow: PublishedBuildWorkflow = publish_build_workflow(
            target_dir=options.pipelines_root.parent / "target",
            workflow=preparation.workflow,
        )
    except KeyboardInterrupt:
        return _cancel_virtual_build(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            sink=sink,
        )
    except OSError as error:
        _persist_virtual_invocation(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            outcome="failed",
            exit_code=1,
            materialized_outcome=None,
            error_message=str(error),
        )
        sink.run_completed(outcome="failed", exit_code=1, error_message=str(error))
        try:
            print(str(error), file=sys.stderr)
        except Exception:
            pass
        return 1
    try:
        lock_context: AbstractContextManager[None] = (
            target_mutation_lock(
                connection=client,
                database=preparation.request.default_database,
                ensure_database=True,
            )
            if _acquire_target_mutation_lock
            else nullcontext()
        )
        with lock_context:
            _reset_fresh_landing_offsets_for_virtual_build(preparation=preparation)
            try:
                execution: WorkflowExecutionResult = execute_build_workflow(
                    published_workflow=published_workflow,
                    connection=client,
                    emitter=sink,
                )
            except (WorkflowExecutionError, AdapterError, KeyboardInterrupt) as error:
                _ = execute_recovery_statements(
                    statements=preparation.workflow.recovery_statements,
                    connection=client,
                    emitter=sink,
                    cause=error.cause if isinstance(error, WorkflowExecutionError) else error,
                    start_sequence=len(preparation.workflow.statements) + 1,
                    completed_step_ids=_completed_step_ids(error),
                    retry_command="sqt build",
                )
                raise
    except (WorkflowExecutionError, AdapterError) as error:
        cause: BaseException = error.cause if isinstance(error, WorkflowExecutionError) else error
        if preparation.workflow.recovery_statements and isinstance(cause, KeyboardInterrupt):
            return _cancel_virtual_build(
                started=started,
                preparation=preparation,
                options=options,
                client=client,
                sink=sink,
                cause=cause,
            )
        failure_text: str = (
            row_error_policy_failure_message(
                changes=preparation.preview.row_error_policy_changes,
                failed_step_id=error.failed_step_id,
                cause=cause,
            )
            if isinstance(error, WorkflowExecutionError)
            else None
        ) or str(cause)
        _persist_virtual_invocation(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            outcome="failed",
            exit_code=1,
            materialized_outcome=None,
            error_message=failure_text,
        )
        if sink is not None:
            sink.run_completed(outcome="failed", exit_code=1, error_message=failure_text)
        print(failure_text, file=sys.stderr)
        return 1
    except KeyboardInterrupt as error:
        return _cancel_virtual_build(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            sink=sink,
            cause=error,
        )
    if _is_policy_only(preparation):
        return _finish_policy_only_build(
            started=started, preparation=preparation, options=options, client=client, sink=sink
        )
    try:
        result: BackfillExecutionResult = build_virtual_execution_result(
            request=preparation.request,
            root_reports=preparation.preview.root_reports,
            existing_relation_names=preparation.preview.existing_relation_names,
            execution=execution,
        )
    except KeyboardInterrupt:
        return _cancel_virtual_build(
            started=started,
            preparation=preparation,
            options=options,
            client=client,
            sink=sink,
        )
    _persist_virtual_invocation(
        started=started,
        preparation=preparation,
        options=options,
        client=client,
        outcome="succeeded",
        exit_code=0,
        materialized_outcome="applied",
        error_message=None,
    )
    if sink is not None:
        sink.run_completed(outcome="succeeded", exit_code=0, error_message=None)
    print(
        render_virtual_build_result(
            result=result,
            database=preparation.preview.database,
            json_output=options.json_output,
        )
    )
    return 0


def _completed_step_ids(error: BaseException) -> frozenset[str] | None:
    """Return the steps a failed workflow finished, or `None` when that is unknown."""

    if not isinstance(error, WorkflowExecutionError) or not isinstance(
        error.partial_result, WorkflowExecutionResult
    ):
        return None
    return frozenset(result.step_id for result in error.partial_result.statement_results)


def _is_policy_only(preparation: VirtualWorkflowPreparation) -> bool:
    """Return whether the build only switches policy on live views, creating no deployment."""

    return bool(preparation.preview.row_error_policy_changes) and not (
        preparation.preview.plan.rebuild_subtrees
    )


def _finish_policy_only_build(
    *,
    started: tuple[str, str, int],
    preparation: VirtualWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    sink: RunEventSink,
) -> int:
    _persist_virtual_invocation(
        started=started,
        preparation=preparation,
        options=options,
        client=client,
        outcome="succeeded",
        exit_code=0,
        materialized_outcome="applied",
        error_message=None,
    )
    sink.run_completed(outcome="succeeded", exit_code=0, error_message=None)
    print(preparation.plan_text)
    return 0


def _reset_fresh_landing_offsets_for_virtual_build(
    *, preparation: VirtualWorkflowPreparation
) -> None:
    """Fresh landing tables consume from earliest: stale committed offsets are orphans."""

    source_preparation, _source_realizations = plan_population_sources(
        desired_state=preparation.preview.desired_state,
        default_database=preparation.preview.database,
        existing_relation_names=preparation.preview.existing_relation_names,
    )
    for reset in reset_fresh_landing_offsets(
        source_preparation=source_preparation,
    ):
        _print_reset_notice(reset.notice)


def _print_reset_notice(notice: str | None) -> None:
    if notice is None:
        return
    try:
        print(notice, file=sys.stderr)
    except Exception:
        return


def _cancel_virtual_build(
    *,
    started: tuple[str, str, int],
    preparation: VirtualWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    sink: RunEventSink,
    cause: BaseException | None = None,
) -> int:
    _persist_virtual_invocation(
        started=started,
        preparation=preparation,
        options=options,
        client=client,
        outcome="cancelled",
        exit_code=_SIGINT_EXIT_CODE,
        materialized_outcome=None,
        error_message=None,
    )
    if sink is not None:
        sink.run_completed(outcome="cancelled", exit_code=_SIGINT_EXIT_CODE, error_message=None)
    try:
        print(render_interrupted_build_message(), file=sys.stderr)
        for note in getattr(cause, "__notes__", ()):
            print(note, file=sys.stderr)
    except Exception:
        pass
    return _SIGINT_EXIT_CODE


def _persist_virtual_invocation(
    *,
    started: tuple[str, str, int],
    preparation: VirtualWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    outcome: str,
    exit_code: int,
    materialized_outcome: str | None,
    error_message: str | None,
) -> None:
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=options.pipelines_root.parent,
            target_identity=preparation.preview.database,
            command="build",
            mode="virtual_environment",
            outcome=outcome,
            exit_code=exit_code,
            materialized_outcome=materialized_outcome,
            deployment_id=None
            if _is_policy_only(preparation)
            else preparation.preview.deployment_id,
            workflow_id=None,
            selected_node_count=len(preparation.preview.run_execution_scope),
            error_message=error_message,
            summary={},
        ),
    )
    warning: str | None = persist_terminal_observations(
        client=client,
        database=preparation.preview.metadata_database,
        invocation=invocation,
        node_results=(),
    )
    if warning is not None:
        try:
            print(warning, file=sys.stderr)
        except Exception:
            return
