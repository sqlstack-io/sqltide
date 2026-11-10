"""Execute one due scheduled-audit batch."""

from pathlib import Path

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.exceptions import AuditSchedulerPersistenceError
from sqltide.executor.auditing.main.execute_sql_audits import execute_sql_audits
from sqltide.executor.auditing.models import SqlAuditResult, SqlAuditRunResult
from sqltide.executor.auditing.types import AuditSeverity, QualityResultStatus
from sqltide.executor.observability.classes.run_event_sink import RunEventSink
from sqltide.executor.observability.main.build_invocation_record import build_invocation_record
from sqltide.executor.observability.main.build_node_result_record import (
    build_node_result_record,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import QualityResultContext, TerminalInvocation
from sqltide.executor.observability.types import QualityResultTrigger


def execute_due_audits(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    observation_connection: AdapterConnection | None = None,
    database: str,
    project_dir: Path,
    due: tuple[dict[str, object], ...],
) -> int:
    """Execute one sequential scheduled batch and persist its typed observations."""

    scheduled_for_by_name: dict[str, str] = {
        str(item["name"]): str(item["scheduledFor"]) for item in due
    }
    audits: tuple[LoadedSqlAudit, ...] = tuple(
        audit
        for audit in analysis.compiled_project.audits
        if (audit.name or audit.file_path.stem) in scheduled_for_by_name
    )
    if not audits:
        return 0
    started: tuple[str, str, int] = start_invocation()
    event_sink: RunEventSink | None = (
        None
        if observation_connection is None
        else RunEventSink(
            connection=observation_connection,
            database=database,
            invocation_id=started[0],
            project_identity=project_dir.name,
        )
    )
    if event_sink is not None:
        event_sink.run_started(
            command="audit",
            mode=QualityResultTrigger.SCHEDULED,
            total_statements=len(audits),
            selected_node_count=len(audits),
        )
    try:
        audit_results: list[SqlAuditResult] = []
        for audit in audits:
            audit_name: str = audit.name or audit.file_path.stem
            if event_sink is not None:
                event_sink.audit_started(name=audit_name)
            audit_result: SqlAuditResult = execute_sql_audits(
                loaded_audits=(audit,),
                resolver={
                    model.key.name: (
                        f"{database}."
                        f"{analysis.realized_project.relation_name_by_logical_key[model.key]}"
                    )
                    for model in analysis.compiled_project.models
                },
                client=connection,
                dialect=analysis.adapter_profile.sql_analysis_dialect,
            ).audit_results[0]
            audit_results.append(audit_result)
            if event_sink is not None:
                event_sink.audit_completed(
                    name=audit_name,
                    status=(
                        QualityResultStatus.ERROR
                        if audit_result.error_message is not None
                        else (
                            QualityResultStatus.PASSED
                            if audit_result.passed
                            else (
                                QualityResultStatus.WARNING
                                if audit_result.severity == AuditSeverity.WARNING
                                else QualityResultStatus.FAILED
                            )
                        )
                    ),
                    failure_count=audit_result.failing_row_count,
                    error_message=audit_result.error_message,
                )
        result: SqlAuditRunResult = SqlAuditRunResult(audit_results=tuple(audit_results))
    except Exception as error:
        if event_sink is not None:
            event_sink.run_completed(outcome="failed", exit_code=1, error_message=str(error))
        failed_invocation: AdapterInvocationRecord = build_invocation_record(
            started=started,
            terminal=TerminalInvocation(
                project_dir=project_dir,
                target_identity=database,
                command="audit",
                mode=QualityResultTrigger.SCHEDULED,
                outcome="failed",
                exit_code=1,
                materialized_outcome=None,
                deployment_id=None,
                workflow_id=None,
                selected_node_count=len(audits),
                error_message=str(error),
                summary={"trigger": "scheduled", "error_count": len(audits)},
            ),
        )
        error_results: tuple[AdapterNodeResultRecord, ...] = tuple(
            _scheduled_error_node_result(
                invocation=failed_invocation,
                audit=audit,
                scheduled_for=scheduled_for_by_name[audit.name or audit.file_path.stem],
                error=error,
            )
            for audit in audits
        )
        persistence_error: str | None = persist_terminal_observations(
            client=connection,
            database=database,
            invocation=failed_invocation,
            node_results=error_results,
        )
        if persistence_error is not None:
            raise AuditSchedulerPersistenceError(persistence_error) from error
        return len(error_results)
    execution_error_count: int = sum(
        1 for audit_result in result.audit_results if audit_result.error_message is not None
    )
    exit_code: int = 1 if result.error_failure_count or execution_error_count else 0
    outcome: str = "failed" if exit_code else "succeeded"
    if event_sink is not None:
        event_sink.run_completed(outcome=outcome, exit_code=exit_code, error_message=None)
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=project_dir,
            target_identity=database,
            command="audit",
            mode=QualityResultTrigger.SCHEDULED,
            outcome=outcome,
            exit_code=exit_code,
            materialized_outcome=None,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=len(audits),
            error_message=None,
            summary={
                "trigger": "scheduled",
                "scheduled_count": len(audits),
                "warning_failure_count": result.warning_failure_count,
                "error_failure_count": result.error_failure_count,
                "execution_error_count": execution_error_count,
            },
        ),
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = tuple(
        _scheduled_node_result(
            invocation=invocation,
            audit=audit,
            result=audit_result,
            scheduled_for=scheduled_for_by_name[audit.name or audit.file_path.stem],
        )
        for audit, audit_result in zip(audits, result.audit_results, strict=True)
    )
    persistence_error = persist_terminal_observations(
        client=connection,
        database=database,
        invocation=invocation,
        node_results=node_results,
    )
    if persistence_error is not None:
        raise AuditSchedulerPersistenceError(persistence_error)
    return len(node_results)


def _scheduled_error_node_result(
    *,
    invocation: AdapterInvocationRecord,
    audit: LoadedSqlAudit,
    scheduled_for: str,
    error: Exception,
) -> AdapterNodeResultRecord:
    return build_node_result_record(
        invocation=invocation,
        identity=_native.require_quality_identity(audit.quality_identity),
        context=QualityResultContext(
            trigger=QualityResultTrigger.SCHEDULED,
            scheduled_for=scheduled_for,
            cadence_seconds=audit.cadence_seconds,
            warmup_seconds=audit.warmup_seconds,
        ),
        status=QualityResultStatus.ERROR,
        severity=audit.severity,
        failure_count=1,
        payload={"batch_failure": True},
        error_message=str(error),
    )


def _scheduled_node_result(
    *,
    invocation: AdapterInvocationRecord,
    audit: LoadedSqlAudit,
    result: SqlAuditResult,
    scheduled_for: str,
) -> AdapterNodeResultRecord:
    status: QualityResultStatus = (
        QualityResultStatus.ERROR
        if result.error_message is not None
        else (
            QualityResultStatus.PASSED
            if result.passed
            else (
                QualityResultStatus.WARNING
                if result.severity == AuditSeverity.WARNING
                else QualityResultStatus.FAILED
            )
        )
    )
    return build_node_result_record(
        invocation=invocation,
        identity=_native.require_quality_identity(audit.quality_identity),
        context=QualityResultContext(
            trigger=QualityResultTrigger.SCHEDULED,
            scheduled_for=scheduled_for,
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
