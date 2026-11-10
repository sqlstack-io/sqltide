from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from sqltide._native import build_audit_deployment_candidates, load_audit_deployment
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.cli.readiness._helpers.audit_selection import (
    backfill_audit_resolver,
    selected_backfill_sql_audits,
)
from sqltide.cli.readiness._helpers.candidates import (
    candidate_root_names,
    enrich_candidates,
)
from sqltide.cli.readiness.main._render_no_deployment_candidates_message import (
    render_no_deployment_candidates_message,
)
from sqltide.cli.readiness.main.render_ambiguous_deployment_message import (
    render_ambiguous_deployment_message,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.executor.auditing.main.execute_sql_audits import execute_sql_audits
from sqltide.executor.auditing.models import SqlAuditRunResult
from sqltide.executor.readiness.models import (
    AuditDeploymentCandidate,
    DeploymentAuditResult,
    LoadedAuditDeployment,
)
from sqltide.executor.readiness.types import AuditAssessment


def resolve_deployment_candidate_error(
    *,
    deployment_id: str | None,
    client: AdapterConnection,
    metadata_database: str,
    database: str,
) -> str | None:
    if deployment_id is not None:
        return None
    candidates: tuple[AuditDeploymentCandidate, ...] = build_audit_deployment_candidates(
        client=client,
        metadata_database=metadata_database,
        default_database=database,
    )
    if not candidates:
        return render_no_deployment_candidates_message(
            command_name="deployment audit",
            database=database,
        )
    if len(candidates) == 1:
        return None
    inspected_state: InspectedManagedTableState = client.inspect_managed_table_state(database)
    return render_ambiguous_deployment_message(
        command_name="deployment audit",
        database=database,
        root_names=candidate_root_names(inspected_state),
        candidates=enrich_candidates(
            client=client,
            metadata_database=metadata_database,
            candidates=candidates,
        ),
    )


def execute_audit_quality_checks(
    *,
    result: DeploymentAuditResult,
    project_dir: Path | None,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    loaded_audits: tuple[LoadedSqlAudit, ...],
    client: AdapterConnection,
    metadata_database: str,
    database: str,
    dialect: str,
) -> DeploymentAuditResult:
    if project_dir is None or not compiled_pipelines:
        return result
    loaded_deployment: LoadedAuditDeployment = load_audit_deployment(
        client=client,
        metadata_database=metadata_database,
        deployment_id=result.deployment_id,
    )
    quality_check_result: SqlAuditRunResult = execute_sql_audits(
        loaded_audits=selected_backfill_sql_audits(
            compiled_pipelines=compiled_pipelines,
            loaded_audits=loaded_audits,
            staged_logical_table_names=frozenset(
                logical_key.name
                for logical_key, _physical_name in loaded_deployment.prepared_object_mappings
            ),
        ),
        resolver=backfill_audit_resolver(
            database=database,
            compiled_pipelines=compiled_pipelines,
            loaded_deployment=loaded_deployment,
        ),
        client=client,
        dialect=dialect,
    )
    assessment: AuditAssessment | str = (
        AuditAssessment.NOT_READY if quality_check_result.error_failure_count else result.assessment
    )
    return replace(
        result,
        assessment=assessment,
        quality_check_results=quality_check_result.audit_results,
    )
