"""Run, persist, and recall audit/test outcomes for the UI."""

from __future__ import annotations

import json
from pathlib import Path

import sqltide._native as _native
from sqltide._native import resolve_audit_warmup_states
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import METADATA_NODE_RESULTS_TABLE_NAME
from sqltide.adapter.models import (
    AdapterCurrentQualityNode,
    AdapterInvocationRecord,
    AdapterNodeResultRecord,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.compiler.testing.models import SqlTestCase
from sqltide.dev_server.exceptions import DevServerError
from sqltide.executor.auditing.main.deferred_audit_result import deferred_audit_result
from sqltide.executor.auditing.main.execute_sql_audits import execute_sql_audits
from sqltide.executor.auditing.main.load_materialized_model_names import (
    load_materialized_model_names,
)
from sqltide.executor.auditing.main.load_model_anchors import load_model_anchors
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult
from sqltide.executor.auditing.types import AuditSeverity
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.build_node_result_record import (
    build_node_result_record,
)
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import QualityResultContext, TerminalInvocation
from sqltide.executor.observability.types import QualityResultTrigger
from sqltide.executor.testing.main.execute_sql_tests import execute_sql_tests
from sqltide.executor.testing.models import (
    SqlTestExecutionResult,
    SqlTestTargetExecutionResult,
)

_NEVER_RUN_STATUS: str = "never_run"
_SUCCEEDED_OUTCOME: str = "succeeded"
_FAILED_OUTCOME: str = "failed"
_PASSED_STATUS: str = "passed"
_WARNING_STATUS: str = "warning"
_FAILED_STATUS: str = "failed"
_ERROR_STATUS: str = "error"


def _audit_status(result: SqlAuditResult) -> str:
    if result.deferred:
        return "deferred"
    if result.error_message is not None:
        return _ERROR_STATUS
    if result.passed:
        return _PASSED_STATUS
    if str(result.severity) == AuditSeverity.WARNING:
        return _WARNING_STATUS
    return _FAILED_STATUS


def _test_status(result: SqlTestExecutionResult) -> str:
    if result.error_message is not None:
        return _ERROR_STATUS
    return _PASSED_STATUS if result.passed else _FAILED_STATUS


def _recorded_target_payloads(result: SqlTestExecutionResult) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    target: SqlTestTargetExecutionResult
    for target in result.target_results:
        payloads.append(
            {
                "target_model_name": target.target_model_name,
                "passed": target.passed,
                "columns": list(target.column_names),
                "missing_rows": [list(item) for item in target.missing_rows[:5]],
                "unexpected_rows": [list(item) for item in target.unexpected_rows[:5]],
            }
        )
    return payloads


def run_one_audit(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    name: str,
    project_dir: Path,
    database: str,
) -> dict[str, object]:
    """Execute one named audit and record its outcome like a CLI run would."""

    return run_audit_batch(
        analysis=analysis,
        connection=connection,
        names=(name,),
        project_dir=project_dir,
        database=database,
    )[0]


def run_audit_batch(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    names: tuple[str, ...],
    project_dir: Path,
    database: str,
) -> list[dict[str, object]]:
    """Execute named audits with one shared setup and persistence boundary."""

    audits_by_name: dict[str, LoadedSqlAudit] = {
        audit.name or audit.file_path.stem: audit for audit in analysis.compiled_project.audits
    }
    unknown_names: list[str] = [name for name in names if name not in audits_by_name]
    if unknown_names:
        raise DevServerError(f"Unknown audit '{unknown_names[0]}'")
    audits: tuple[LoadedSqlAudit, ...] = tuple(audits_by_name[name] for name in names)
    if not audits:
        return []
    started: tuple[str, str, int] = start_invocation()
    resolver: dict[str, str] = _model_resolver(analysis)
    referenced_model_names: tuple[str, ...] = _referenced_model_names(audits)
    warmup_state: dict[str, AuditWarmupState] = resolve_audit_warmup_states(
        audits=audits,
        anchors_by_model=load_model_anchors(
            client=connection,
            metadata_database=database,
            target_database=database,
            model_names=referenced_model_names,
            virtual_environments=analysis.compile_inputs.virtual_environments,
        ),
        materialized_model_names=load_materialized_model_names(
            client=connection,
            database=database,
            relation_name_by_model=resolver,
        ),
        warehouse_now=connection.capture_warehouse_timestamp(),
    )
    eligible_audits: tuple[LoadedSqlAudit, ...] = tuple(
        audit for audit in audits if warmup_state[audit.name or audit.file_path.stem].eligible
    )
    executed_results: tuple[SqlAuditResult, ...] = (
        execute_sql_audits(
            loaded_audits=eligible_audits,
            resolver=resolver,
            client=connection,
            dialect=analysis.adapter_profile.sql_analysis_dialect,
        ).audit_results
        if eligible_audits
        else ()
    )
    executed_by_name: dict[str, SqlAuditResult] = {
        audit.name or audit.file_path.stem: result
        for audit, result in zip(eligible_audits, executed_results, strict=True)
    }
    results: tuple[SqlAuditResult, ...] = tuple(
        executed_by_name.get(audit.name or audit.file_path.stem)
        or deferred_audit_result(
            audit=audit,
            state=warmup_state[audit.name or audit.file_path.stem],
        )
        for audit in audits
    )
    _persist_audit_observations(
        connection=connection,
        database=database,
        project_dir=project_dir,
        started=started,
        audits=audits,
        results=results,
    )
    return [
        _audit_result_payload(name=name, result=result)
        for name, result in zip(names, results, strict=True)
    ]


def _audit_result_payload(*, name: str, result: SqlAuditResult) -> dict[str, object]:
    return {
        "name": name,
        "kind": "audit",
        "passed": result.passed,
        "severity": result.severity,
        "failingRowCount": result.failing_row_count,
        "sampleColumns": list(result.sample_column_names),
        "sampleRows": [list(row) for row in result.sample_rows],
        "errorMessage": result.error_message,
        "deferredUntil": result.deferred_until,
        "missingRelations": list(result.missing_relation_names),
    }


def _referenced_model_names(audits: tuple[LoadedSqlAudit, ...]) -> tuple[str, ...]:
    model_names: set[str] = set()
    for audit in audits:
        model_names.update(audit.referenced_model_names)
    return tuple(sorted(model_names))


def run_one_test(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    name: str,
    project_dir: Path,
    database: str,
) -> dict[str, object]:
    """Execute one named SQL test and record its outcome like a CLI run would."""

    test_case: SqlTestCase | None = _test_by_name(analysis=analysis, name=name)
    if test_case is None:
        raise DevServerError(f"Unknown test '{name}'")
    started: tuple[str, str, int] = start_invocation()
    results: tuple[SqlTestExecutionResult, ...] = execute_sql_tests(
        test_cases=(test_case,), client=connection
    )
    result: SqlTestExecutionResult = results[0]
    _persist_test_observation(
        connection=connection,
        database=database,
        project_dir=project_dir,
        started=started,
        test_case=test_case,
        result=result,
    )
    return {
        "name": name,
        "kind": "test",
        "passed": result.passed,
        "targets": [_target_payload(target) for target in result.target_results],
        "errorMessage": result.error_message,
    }


def build_checks_status_payload(
    *,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    database: str,
    project_dir: Path,
) -> list[dict[str, object]]:
    """Last-known outcome per audit/test from the observability tables."""

    nodes: list[AdapterCurrentQualityNode] = []
    model_resolver: dict[str, str] = _model_resolver(analysis)
    audit_model_name_set: set[str] = set()
    for audit in analysis.compiled_project.audits:
        audit_model_name_set.update(audit.referenced_model_names)
    audit_model_names: tuple[str, ...] = tuple(sorted(audit_model_name_set))
    anchors_by_model: dict[str, str] = load_model_anchors(
        client=connection,
        metadata_database=database,
        target_database=database,
        model_names=audit_model_names,
        virtual_environments=analysis.compile_inputs.virtual_environments,
    )
    materialized_model_names: frozenset[str] = load_materialized_model_names(
        client=connection,
        database=database,
        relation_name_by_model=model_resolver,
    )
    missing_by_audit: dict[str, list[str]] = {}
    for audit in analysis.compiled_project.audits:
        missing_by_audit[audit.name or audit.file_path.stem] = [
            model_name
            for model_name in audit.referenced_model_names
            if model_name not in anchors_by_model and model_name not in materialized_model_names
        ]
    for audit in analysis.compiled_project.audits:
        identity: QualityNodeIdentity = _native.require_quality_identity(audit.quality_identity)
        nodes.append(
            AdapterCurrentQualityNode(
                node_kind=identity.node_kind,
                node_name=identity.node_name,
                binding_key=identity.binding_key,
                definition_fingerprint=identity.definition_fingerprint,
                execution_fingerprint=identity.execution_fingerprint,
                cadence_seconds=audit.cadence_seconds,
                warmup_seconds=audit.warmup_seconds,
            )
        )
    for test_case in analysis.compiled_project.test_cases:
        identity: QualityNodeIdentity = _native.require_quality_identity(test_case.quality_identity)
        nodes.append(
            AdapterCurrentQualityNode(
                node_kind=identity.node_kind,
                node_name=identity.node_name,
                binding_key=identity.binding_key,
                definition_fingerprint=identity.definition_fingerprint,
                execution_fingerprint=identity.execution_fingerprint,
            )
        )
    if not connection.metadata_columns(
        database=database,
        table=METADATA_NODE_RESULTS_TABLE_NAME,
    ):
        return [
            {
                "kind": node.node_kind,
                "name": node.node_name,
                "status": (
                    "deferred" if missing_by_audit.get(node.node_name) else _NEVER_RUN_STATUS
                ),
                "driftReasons": [],
                "severity": None,
                "failureCount": 0,
                "completedAt": None,
                "payload": None,
                "errorMessage": None,
                "missingRelations": missing_by_audit.get(node.node_name, []),
            }
            for node in nodes
        ]
    query: str = connection.render_latest_node_status_query(
        database=database,
        project_identity=logical_project_identity(project_dir=project_dir),
        target_identity=database,
        nodes=tuple(nodes),
    )
    statuses: list[dict[str, object]] = []
    for row in connection.query(query).named_rows():
        row_payload: dict[str, object] = _status_row_payload(row=dict(row))
        missing_relations: list[str] = missing_by_audit.get(str(row_payload["name"]), [])
        if missing_relations:
            row_payload["status"] = "deferred"
        row_payload["missingRelations"] = missing_relations
        statuses.append(row_payload)
    return statuses


def _status_row_payload(*, row: dict[str, object]) -> dict[str, object]:
    kind: str = str(row["node_kind"])
    node_name: str = str(row["node_name"])
    status: str = str(row["current_status"])
    recorded: bool = status != _NEVER_RUN_STATUS
    raw_drift_reasons: object = row.get("drift_reasons")
    drift_reasons: list[str] = (
        [str(reason) for reason in raw_drift_reasons]
        if isinstance(raw_drift_reasons, list | tuple)
        else []
    )
    return {
        "kind": kind,
        "name": node_name,
        "status": status,
        "driftReasons": drift_reasons,
        "severity": row["severity"],
        "failureCount": int(str(row["failure_count"])) if recorded else 0,
        "completedAt": str(row["completed_at"]) if recorded else None,
        "payload": _parsed_payload(row["payload_json"]) if recorded else None,
        "errorMessage": row["error_message"],
    }


def _parsed_payload(payload_json: object) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(str(payload_json))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _persist_audit_observations(
    *,
    connection: AdapterConnection,
    database: str,
    project_dir: Path,
    started: tuple[str, str, int],
    audits: tuple[LoadedSqlAudit, ...],
    results: tuple[SqlAuditResult, ...],
) -> None:
    hard_failure: bool = any(
        not result.deferred
        and (result.error_message is not None or not result.passed)
        and str(result.severity) != AuditSeverity.WARNING
        for result in results
    )
    error_message: str | None = next(
        (result.error_message for result in results if result.error_message is not None), None
    )
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=project_dir,
            target_identity=database,
            command="audit",
            mode=None,
            outcome=_FAILED_OUTCOME if hard_failure else _SUCCEEDED_OUTCOME,
            exit_code=1 if hard_failure else 0,
            materialized_outcome=None,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=len(audits),
            error_message=error_message,
            summary={"source": "dev_server"},
        ),
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = tuple(
        _audit_node_result(invocation=invocation, audit=audit, result=result)
        for audit, result in zip(audits, results, strict=True)
    )
    _ = persist_terminal_observations(
        client=connection,
        database=database,
        invocation=invocation,
        node_results=node_results,
    )


def _audit_node_result(
    *,
    invocation: AdapterInvocationRecord,
    audit: LoadedSqlAudit,
    result: SqlAuditResult,
) -> AdapterNodeResultRecord:
    return build_node_result_record(
        invocation=invocation,
        identity=_native.require_quality_identity(audit.quality_identity),
        context=QualityResultContext(
            trigger=QualityResultTrigger.MANUAL,
            cadence_seconds=audit.cadence_seconds,
            warmup_seconds=audit.warmup_seconds,
        ),
        status=_audit_status(result),
        severity=result.severity,
        failure_count=result.failing_row_count,
        payload={
            "sample_column_names": list(result.sample_column_names),
            "sample_rows": [list(item) for item in result.sample_rows[:5]],
            "eligible_at": result.deferred_until,
            "missing_relations": list(result.missing_relation_names),
        },
        error_message=result.error_message,
    )


def _persist_test_observation(
    *,
    connection: AdapterConnection,
    database: str,
    project_dir: Path,
    started: tuple[str, str, int],
    test_case: SqlTestCase,
    result: SqlTestExecutionResult,
) -> None:
    missing_count: int = sum(len(target.missing_rows) for target in result.target_results)
    unexpected_count: int = sum(len(target.unexpected_rows) for target in result.target_results)
    failed: bool = result.error_message is not None or not result.passed
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=project_dir,
            target_identity=database,
            command="test",
            mode=None,
            outcome=_FAILED_OUTCOME if failed else _SUCCEEDED_OUTCOME,
            exit_code=1 if failed else 0,
            materialized_outcome=None,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=1,
            error_message=result.error_message,
            summary={"source": "dev_server"},
        ),
    )
    status: str = _test_status(result)
    _ = persist_terminal_observations(
        client=connection,
        database=database,
        invocation=invocation,
        node_results=(
            build_node_result_record(
                invocation=invocation,
                identity=_native.require_quality_identity(test_case.quality_identity),
                context=QualityResultContext(trigger=QualityResultTrigger.MANUAL),
                status=status,
                severity=None,
                failure_count=missing_count
                + unexpected_count
                + int(result.error_message is not None),
                payload={
                    "missing_count": missing_count,
                    "unexpected_count": unexpected_count,
                    "targets": _recorded_target_payloads(result),
                },
                error_message=result.error_message,
            ),
        ),
    )


def _target_payload(target: SqlTestTargetExecutionResult) -> dict[str, object]:
    return {
        "targetModelName": target.target_model_name,
        "passed": target.passed,
        "columns": list(target.column_names),
        "missingRows": [list(row) for row in target.missing_rows],
        "unexpectedRows": [list(row) for row in target.unexpected_rows],
    }


def _audit_by_name(*, analysis: CompileAnalysis, name: str) -> LoadedSqlAudit | None:
    for audit in analysis.compiled_project.audits:
        if (audit.name or audit.file_path.stem) == name:
            return audit
    return None


def _test_by_name(*, analysis: CompileAnalysis, name: str) -> SqlTestCase | None:
    for test_case in analysis.compiled_project.test_cases:
        if (test_case.name or test_case.file_path.stem) == name:
            return test_case
    return None


def _model_resolver(analysis: CompileAnalysis) -> dict[str, str]:
    resolver: dict[str, str] = {}
    for key, relation in analysis.realized_project.relation_name_by_logical_key.items():
        if key.resource_type == LogicalResourceType.MODEL:
            resolver[key.name] = relation
    return resolver
