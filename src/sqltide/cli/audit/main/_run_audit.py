"""CLI command for live SQL audits."""

from pathlib import Path

import sqltide._native as _native
from sqltide._native import analyze_project, resolve_audit_warmup_states
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.cli.audit._helpers.referenced_models import referenced_model_names
from sqltide.cli.audit._helpers.rendering import render_sql_audit_run_result
from sqltide.cli.audit._helpers.selection import select_loaded_sql_audits
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import CompiledPipeline, CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.auditing.main.deferred_audit_result import deferred_audit_result
from sqltide.executor.auditing.main.execute_sql_audits import execute_sql_audits
from sqltide.executor.auditing.main.load_materialized_model_names import (
    load_materialized_model_names,
)
from sqltide.executor.auditing.main.load_model_anchors import load_model_anchors
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult, SqlAuditRunResult
from sqltide.executor.auditing.types import AuditSeverity
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.build_node_result_record import (
    build_node_result_record,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import QualityResultContext, TerminalInvocation
from sqltide.executor.observability.types import QualityResultTrigger


def run_audit(
    *,
    pipelines_root: Path,
    project_dir: Path,
    database: str,
    selectors: tuple[str, ...],
    json_output: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    force: bool = False,
) -> int:
    """Run user-defined SQL audits against published logical views."""

    started: tuple[str, str, int] = start_invocation()
    selected_node_count = 0
    try:
        client.validate_metadata_state(database)
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=pipelines_root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
        compiled_pipelines: tuple[CompiledPipeline, ...] = analysis.compiled_project.pipelines
        loaded_audits: tuple[LoadedSqlAudit, ...] = analysis.compiled_project.audits
        selected_audits: tuple[LoadedSqlAudit, ...] = select_loaded_sql_audits(
            loaded_audits=loaded_audits,
            compiled_pipelines=compiled_pipelines,
            selectors=selectors,
        )
        selected_node_count = len(selected_audits)
        result: SqlAuditRunResult = _execute_selected_audits(
            analysis=analysis,
            selected_audits=selected_audits,
            database=database,
            client=client,
            adapter_profile=adapter_profile,
            force=force,
        )
        print(
            render_sql_audit_run_result(
                result=result,
                database=database,
                project_dir=project_dir,
                json_output=json_output,
            )
        )
    except Exception as error:
        failed_invocation: AdapterInvocationRecord = build_invocation_record(
            started=started,
            terminal=TerminalInvocation(
                project_dir=project_dir,
                target_identity=database,
                command="audit",
                mode=None,
                outcome="failed",
                exit_code=1,
                materialized_outcome=None,
                deployment_id=None,
                workflow_id=None,
                selected_node_count=selected_node_count,
                error_message=str(error),
                summary={"failed_before_results": True},
            ),
        )
        _ = persist_terminal_observations(
            client=client,
            database=database,
            invocation=failed_invocation,
            node_results=(),
        )
        raise
    exit_code: int = 1 if result.error_failure_count else 0
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=project_dir,
            target_identity=database,
            command="audit",
            mode=None,
            outcome="failed" if exit_code else "succeeded",
            exit_code=exit_code,
            materialized_outcome=None,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=len(selected_audits),
            error_message=None,
            summary={
                "error_failure_count": result.error_failure_count,
                "warning_failure_count": result.warning_failure_count,
            },
        ),
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = _audit_node_results(
        invocation=invocation,
        audits=selected_audits,
        result=result,
    )
    _ = persist_terminal_observations(
        client=client,
        database=database,
        invocation=invocation,
        node_results=node_results,
    )
    return exit_code


def _execute_selected_audits(
    *,
    analysis: CompileAnalysis,
    selected_audits: tuple[LoadedSqlAudit, ...],
    database: str,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
    force: bool,
) -> SqlAuditRunResult:
    anchors_by_model: dict[str, str] = (
        {}
        if force
        else load_model_anchors(
            client=client,
            metadata_database=database,
            target_database=database,
            model_names=referenced_model_names(selected_audits),
            virtual_environments=analysis.compile_inputs.virtual_environments,
        )
    )
    warmup_states: dict[str, AuditWarmupState] = resolve_audit_warmup_states(
        audits=selected_audits,
        anchors_by_model=anchors_by_model,
        materialized_model_names=load_materialized_model_names(
            client=client,
            database=database,
            relation_name_by_model={
                model.key.name: analysis.realized_project.relation_name_by_logical_key[model.key]
                for model in analysis.compiled_project.models
            },
        ),
        warehouse_now=client.capture_warehouse_timestamp(),
    )
    executable_audits: tuple[LoadedSqlAudit, ...] = tuple(
        audit
        for audit in selected_audits
        if force or warmup_states[audit.name or audit.file_path.stem].eligible
    )
    executed_result: SqlAuditRunResult = execute_sql_audits(
        loaded_audits=executable_audits,
        resolver={
            model.key.name: (
                f"{database}.{analysis.realized_project.relation_name_by_logical_key[model.key]}"
            )
            for model in analysis.compiled_project.models
        },
        client=client,
        dialect=adapter_profile.sql_analysis_dialect,
    )
    executed_by_name: dict[str, SqlAuditResult] = {
        audit.name or audit.file_path.stem: audit_result
        for audit, audit_result in zip(
            executable_audits,
            executed_result.audit_results,
            strict=True,
        )
    }
    return SqlAuditRunResult(
        audit_results=tuple(
            executed_by_name.get(audit.name or audit.file_path.stem)
            or deferred_audit_result(
                audit=audit,
                state=warmup_states[audit.name or audit.file_path.stem],
            )
            for audit in selected_audits
        )
    )


def _audit_node_results(
    *,
    invocation: AdapterInvocationRecord,
    audits: tuple[LoadedSqlAudit, ...],
    result: SqlAuditRunResult,
) -> tuple[AdapterNodeResultRecord, ...]:
    records: list[AdapterNodeResultRecord] = []
    audit: LoadedSqlAudit
    audit_result: SqlAuditResult
    for audit, audit_result in zip(audits, result.audit_results, strict=True):
        status: str = (
            "deferred"
            if audit_result.deferred
            else (
                "error"
                if audit_result.error_message is not None
                else (
                    "passed"
                    if audit_result.passed
                    else ("warning" if audit_result.severity == AuditSeverity.WARNING else "failed")
                )
            )
        )
        records.append(
            build_node_result_record(
                invocation=invocation,
                identity=_native.require_quality_identity(audit.quality_identity),
                context=QualityResultContext(
                    trigger=QualityResultTrigger.MANUAL,
                    cadence_seconds=audit.cadence_seconds,
                    warmup_seconds=audit.warmup_seconds,
                ),
                status=status,
                severity=audit_result.severity,
                failure_count=audit_result.failing_row_count,
                payload={
                    "sample_column_names": list(audit_result.sample_column_names),
                    "sample_rows": [list(row) for row in audit_result.sample_rows[:5]],
                    "eligible_at": audit_result.deferred_until,
                    "missing_relations": list(audit_result.missing_relation_names),
                },
                error_message=audit_result.error_message,
            )
        )
    return tuple(records)
