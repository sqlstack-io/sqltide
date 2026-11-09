"""Execute one confirmed direct-mode build command."""

import json
import sys

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.cli.build._helpers.audits import select_direct_build_audits
from sqltide.cli.build._helpers.execution import execute_confirmed_direct_build
from sqltide.cli.build._helpers.rendering import (
    render_direct_build_json,
    render_direct_build_text,
    render_interrupted_build_message,
)
from sqltide.cli.build.models import (
    BuildCommandOptions,
    DirectBuildPreviewContext,
    DirectWorkflowPreparation,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.compiler.quality.main.require_quality_identity import require_quality_identity
from sqltide.executor.auditing.main.resolve_audit_warmup_states import (
    resolve_audit_warmup_states,
)
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult, SqlAuditRunResult
from sqltide.executor.auditing.types import AuditSeverity
from sqltide.executor.direct.exceptions import DirectBuildError
from sqltide.executor.direct.models import (
    DirectBuildExecutionResult,
    DirectBuildResult,
)
from sqltide.executor.observability.classes.run_event_sink import RunEventSink
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.build_node_result_record import (
    build_node_result_record,
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
from sqltide.executor.observability.models import (
    QualityResultContext,
    RunStartupTimings,
    TerminalInvocation,
)
from sqltide.executor.observability.types import MaterializationOutcome, QualityResultTrigger

_SIGINT_EXIT_CODE: int = 130


def execute_direct_build_command(
    *,
    preparation: DirectWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    confirmation_required: bool = True,
    startup_timings: RunStartupTimings | None = None,
    _acquire_target_mutation_lock: bool = True,
) -> int:
    """Confirm, execute, and audit one prepared direct build."""

    sink: RunEventSink = _build_event_sink(
        options=options,
        client=observation_client,
        preparation=preparation,
        invocation_id=started[0],
    )
    try:
        if sink is not None:
            sink.run_started(
                command="build",
                mode="direct",
                total_statements=len(preparation.workflow.statements),
                selected_node_count=len(preparation.preview.plan.execution_scope),
                selectors=options.selectors,
                start_time=options.start_time,
                executed_logical_ids=logical_resource_identities(
                    preparation.preview.plan.execution_scope
                ),
                context_logical_ids=logical_resource_identities(
                    tuple(item.key for item in preparation.preview.plan.prerequisite_scope)
                ),
                startup_timings=startup_timings,
            )
        if (
            preparation.preview.plan.selection_mode == DirectSelectionMode.CHANGED
            and not preparation.preview.plan.execution_scope
            and not preparation.preview.row_error_policy_changes
        ):
            invocation: AdapterInvocationRecord = _build_invocation(
                started=started,
                preparation=preparation,
                options=options,
                exit_code=0,
                outcome="succeeded",
                materialized_outcome=None,
                audit_result=None,
                error_message=None,
            )
            _persist_terminal_observations(
                client=client,
                database=preparation.preview.metadata_database,
                invocation=invocation,
                node_results=(),
            )
            if sink is not None:
                sink.run_completed(outcome="succeeded", exit_code=0, error_message=None)
            if not options.events_output:
                print(
                    json.dumps({"mode": "direct", "status": "no_op", "reason": "no_changes"})
                    if options.json_output
                    else "No changed models to build."
                )
            return 0
        execution: DirectBuildExecutionResult | None = execute_confirmed_direct_build(
            preparation=preparation,
            options=options,
            client=client,
            emitter=sink,
            confirmation_required=confirmation_required,
            _acquire_target_mutation_lock=_acquire_target_mutation_lock,
        )
    except (AdapterError, DirectBuildError, OSError) as error:
        failed_invocation: AdapterInvocationRecord = _build_invocation(
            started=started,
            preparation=preparation,
            options=options,
            exit_code=1,
            outcome="failed",
            materialized_outcome=MaterializationOutcome.FAILED,
            audit_result=None,
            error_message=str(error),
        )
        _persist_terminal_observations(
            client=client,
            database=preparation.preview.metadata_database,
            invocation=failed_invocation,
            node_results=(),
        )
        if sink is not None:
            sink.run_completed(outcome="failed", exit_code=1, error_message=str(error))
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        interrupted_invocation: AdapterInvocationRecord = _build_invocation(
            started=started,
            preparation=preparation,
            options=options,
            exit_code=_SIGINT_EXIT_CODE,
            outcome="cancelled",
            materialized_outcome=MaterializationOutcome.FAILED,
            audit_result=None,
            error_message=None,
        )
        _persist_terminal_observations(
            client=client,
            database=preparation.preview.metadata_database,
            invocation=interrupted_invocation,
            node_results=(),
        )
        if sink is not None:
            sink.run_completed(outcome="cancelled", exit_code=_SIGINT_EXIT_CODE, error_message=None)
        try:
            print(render_interrupted_build_message(), file=sys.stderr)
        except Exception:
            pass
        return _SIGINT_EXIT_CODE
    if execution is None:
        invocation: AdapterInvocationRecord = _build_invocation(
            started=started,
            preparation=preparation,
            options=options,
            exit_code=1,
            outcome="cancelled",
            materialized_outcome=None,
            audit_result=None,
            error_message=None,
        )
        _persist_terminal_observations(
            client=client,
            database=preparation.preview.metadata_database,
            invocation=invocation,
            node_results=(),
        )
        if sink is not None:
            sink.run_completed(outcome="cancelled", exit_code=1, error_message=None)
        print("Build cancelled.")
        return 1
    exit_code: int = 1 if execution.audit_result.error_failure_count else 0
    invocation = _build_invocation(
        started=started,
        preparation=preparation,
        options=options,
        exit_code=exit_code,
        outcome="failed" if exit_code else "succeeded",
        materialized_outcome=MaterializationOutcome.APPLIED,
        audit_result=execution.audit_result,
        error_message=None,
    )
    all_selected_audits: tuple[LoadedSqlAudit, ...] = select_direct_build_audits(
        audits=preparation.preview.analysis.compiled_project.audits,
        execution_model_names=frozenset(
            key.name for key in preparation.preview.plan.execution_scope
        ),
        full_build=(preparation.preview.plan.selection_mode == DirectSelectionMode.ALL_MODELS),
    )
    selected_audits: tuple[LoadedSqlAudit, ...] = tuple(
        audit for audit in all_selected_audits if audit.warmup_seconds == 0
    )
    deferred_audits: tuple[LoadedSqlAudit, ...] = tuple(
        audit for audit in all_selected_audits if audit.warmup_seconds > 0
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = _direct_audit_node_results(
        invocation=invocation,
        audits=selected_audits,
        audit_result=execution.audit_result,
    )
    node_results = (
        *node_results,
        *_deferred_direct_audit_node_results(
            invocation=invocation,
            audits=deferred_audits,
        ),
    )
    _persist_terminal_observations(
        client=client,
        database=preparation.preview.metadata_database,
        invocation=invocation,
        node_results=node_results,
    )
    if sink is not None:
        sink.run_completed(
            outcome="failed" if exit_code else "succeeded",
            exit_code=exit_code,
            error_message=None,
        )
    if not options.events_output:
        if deferred_audits:
            print(
                f"Deferred {len(deferred_audits)} audit(s) for post-build warmup.",
                file=sys.stderr,
            )
        print(
            _rendered_result(
                options=options,
                preview=preparation.preview,
                result=execution.build_result,
                audit_result=execution.audit_result,
            )
        )
    return exit_code


def _persist_terminal_observations(
    *,
    client: AdapterConnection,
    database: str,
    invocation: AdapterInvocationRecord,
    node_results: tuple[AdapterNodeResultRecord, ...],
) -> None:
    warning: str | None = persist_terminal_observations(
        client=client,
        database=database,
        invocation=invocation,
        node_results=node_results,
    )
    if warning is not None:
        try:
            print(warning, file=sys.stderr)
        except Exception:
            return


def _build_event_sink(
    *,
    options: BuildCommandOptions,
    client: AdapterConnection,
    preparation: DirectWorkflowPreparation,
    invocation_id: str,
) -> RunEventSink:
    return RunEventSink(
        connection=client,
        database=preparation.preview.metadata_database,
        invocation_id=invocation_id,
        project_identity=logical_project_identity(project_dir=options.pipelines_root.parent),
        jsonl_stream=sys.stdout if options.events_output else None,
    )


def _direct_audit_node_results(
    *,
    invocation: AdapterInvocationRecord,
    audits: tuple[LoadedSqlAudit, ...],
    audit_result: SqlAuditRunResult,
) -> tuple[AdapterNodeResultRecord, ...]:
    records: list[AdapterNodeResultRecord] = []
    audit: LoadedSqlAudit
    result: SqlAuditResult
    for audit, result in zip(audits, audit_result.audit_results, strict=True):
        status: str = (
            "error"
            if result.error_message is not None
            else (
                "passed"
                if result.passed
                else ("warning" if result.severity == AuditSeverity.WARNING else "failed")
            )
        )
        records.append(
            build_node_result_record(
                invocation=invocation,
                identity=require_quality_identity(audit.quality_identity),
                context=QualityResultContext(
                    trigger=QualityResultTrigger.BUILD,
                    cadence_seconds=audit.cadence_seconds,
                    warmup_seconds=audit.warmup_seconds,
                ),
                status=status,
                severity=result.severity,
                failure_count=result.failing_row_count,
                payload={
                    "sample_column_names": list(result.sample_column_names),
                    "sample_rows": [list(row) for row in result.sample_rows[:5]],
                },
                error_message=result.error_message,
            )
        )
    return tuple(records)


def _deferred_direct_audit_node_results(
    *, invocation: AdapterInvocationRecord, audits: tuple[LoadedSqlAudit, ...]
) -> tuple[AdapterNodeResultRecord, ...]:
    if not audits:
        return ()
    anchors_by_model: dict[str, str] = {}
    for audit in audits:
        for model_name in audit.referenced_model_names:
            anchors_by_model[model_name] = invocation.completed_at
    states: dict[str, AuditWarmupState] = resolve_audit_warmup_states(
        audits=audits,
        anchors_by_model=anchors_by_model,
        materialized_model_names=frozenset(anchors_by_model),
        warehouse_now=invocation.completed_at,
    )
    return tuple(
        build_node_result_record(
            invocation=invocation,
            identity=require_quality_identity(audit.quality_identity),
            context=QualityResultContext(
                trigger=QualityResultTrigger.BUILD,
                cadence_seconds=audit.cadence_seconds,
                warmup_seconds=audit.warmup_seconds,
            ),
            status="deferred",
            severity=audit.severity,
            failure_count=0,
            payload={
                "eligible_at": states[audit.name or audit.file_path.stem].eligible_at,
            },
            error_message=None,
        )
        for audit in audits
    )


def _build_invocation(
    *,
    started: tuple[str, str, int],
    preparation: DirectWorkflowPreparation,
    options: BuildCommandOptions,
    exit_code: int,
    outcome: str,
    materialized_outcome: str | None,
    audit_result: SqlAuditRunResult | None,
    error_message: str | None,
) -> AdapterInvocationRecord:
    return build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=options.pipelines_root.parent,
            target_identity=preparation.preview.database,
            command="build",
            mode="direct",
            outcome=outcome,
            exit_code=exit_code,
            materialized_outcome=materialized_outcome,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=len(preparation.preview.plan.execution_scope),
            error_message=error_message,
            summary={
                "audit_error_failure_count": (
                    audit_result.error_failure_count if audit_result is not None else 0
                ),
                "audit_warning_failure_count": (
                    audit_result.warning_failure_count if audit_result is not None else 0
                ),
            },
        ),
    )


def _rendered_result(
    *,
    options: BuildCommandOptions,
    preview: DirectBuildPreviewContext,
    result: DirectBuildResult,
    audit_result: SqlAuditRunResult,
) -> str:
    if options.json_output:
        return render_direct_build_json(
            result=result,
            adapter_name=preview.adapter_name,
            audit_result=audit_result,
        )
    return render_direct_build_text(
        result=result,
        adapter_name=preview.adapter_name,
        audit_result=audit_result,
    )
