"""CLI command for staged backfill audit."""

import sys
from pathlib import Path

from sqltide._native import analyze_project
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.cli.entry.main._errors import render_expected_warehouse_error
from sqltide.cli.readiness._helpers.audit_execution import (
    execute_audit_quality_checks,
    resolve_deployment_candidate_error,
)
from sqltide.cli.readiness.main.render_deployment_audit_result import (
    render_deployment_audit_result,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import CompiledPipeline, CompilerAdapterProfile
from sqltide.compiler.discovery.models import DeploymentReadinessDefaults, LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.readiness.main.execute_deployment_audit import execute_deployment_audit
from sqltide.executor.readiness.models import (
    DeploymentAuditRequest,
    DeploymentAuditResult,
    DeploymentReadinessThresholds,
)


def run_deployment_audit(
    *,
    pipelines_root: Path | None,
    project_dir: Path | None,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    json_output: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Audit a staged backfill deployment and print the result payload."""

    resolved_metadata_database: str = metadata_database or database
    try:
        analysis: CompileAnalysis | None = (
            analyze_project(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
                adapter_profile=adapter_profile,
            )
            if pipelines_root is not None
            else None
        )
        compiled_pipelines: tuple[CompiledPipeline, ...] = (
            () if analysis is None else analysis.compiled_project.pipelines
        )
        loaded_audits: tuple[LoadedSqlAudit, ...] = (
            () if analysis is None else analysis.compiled_project.audits
        )
        candidate_error: str | None = resolve_deployment_candidate_error(
            deployment_id=deployment_id,
            client=client,
            metadata_database=resolved_metadata_database,
            database=database,
        )
        if candidate_error is not None:
            print(candidate_error, file=sys.stderr)
            return 1
        result: DeploymentAuditResult = execute_deployment_audit(
            request=DeploymentAuditRequest(
                deployment_id=deployment_id,
                metadata_database=resolved_metadata_database,
                default_database=database,
                thresholds=_readiness_thresholds(loaded_project),
            ),
            client=client,
        )
        result = execute_audit_quality_checks(
            result=result,
            project_dir=project_dir,
            compiled_pipelines=compiled_pipelines,
            loaded_audits=loaded_audits,
            client=client,
            metadata_database=resolved_metadata_database,
            database=database,
            dialect=adapter_profile.sql_analysis_dialect,
        )
    except AdapterWarehouseError as error:
        rendered_error: str | None = render_expected_warehouse_error(
            command_name="deployment audit",
            database=database,
            error=error,
        )
        if rendered_error is not None:
            print(rendered_error, file=sys.stderr)
            return 1
        raise
    print(
        render_deployment_audit_result(
            result=result,
            database=database,
            json_output=json_output,
            project_dir=project_dir,
        )
    )
    return 0


def _readiness_thresholds(loaded_project: LoadedProject | None) -> DeploymentReadinessThresholds:
    if loaded_project is None or loaded_project.effective_configuration is None:
        return DeploymentReadinessThresholds()
    defaults: DeploymentReadinessDefaults = (
        loaded_project.effective_configuration.defaults.deployment_readiness
    )
    return DeploymentReadinessThresholds(
        maximum_lag_seconds=defaults.maximum_lag_seconds,
        minimum_staged_row_ratio=defaults.minimum_staged_row_ratio,
    )
