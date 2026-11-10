"""Calculate audit due states from compiled policy and warehouse metadata."""

from pathlib import Path

from sqltide._native import (
    audit_schedule_entry,
    read_latest_direct_build_materialization,
    resolve_audit_warmup_states,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.quality.types import QualityNodeKind
from sqltide.dev_server._helpers.server.checks_execution import build_checks_status_payload
from sqltide.dev_server.types import AuditScheduleState
from sqltide.executor.auditing.main.load_materialized_model_names import (
    load_materialized_model_names,
)
from sqltide.executor.auditing.main.load_model_anchors import load_model_anchors
from sqltide.executor.auditing.models import AuditWarmupState
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)
from sqltide.executor.observability.types import MaterializationOutcome


class AuditScheduleCalculator:
    """Build a dry-run scheduler payload for one compiled target."""

    def __init__(
        self,
        *,
        analysis: CompileAnalysis,
        connection: AdapterConnection,
        database: str,
        project_dir: Path,
    ) -> None:
        self._analysis = analysis
        self._connection = connection
        self._database = database
        self._project_dir = project_dir

    def build_payload(self, *, enabled: bool) -> dict[str, object]:
        audits: tuple[LoadedSqlAudit, ...] = tuple(
            audit for audit in self._analysis.compiled_project.audits if audit.scheduled
        )
        warehouse_now: str = self._connection.capture_warehouse_timestamp()
        if not audits:
            return {
                "enabled": enabled,
                "state": AuditScheduleState.IDLE if enabled else AuditScheduleState.DISABLED,
                "warehouseNow": warehouse_now,
                "dueCount": 0,
                "audits": [],
            }
        anchors: dict[str, str] = load_model_anchors(
            client=self._connection,
            metadata_database=self._database,
            target_database=self._database,
            model_names=self._referenced_model_names(audits),
            virtual_environments=self._analysis.compile_inputs.virtual_environments,
        )
        materialized_model_names: frozenset[str] = load_materialized_model_names(
            client=self._connection,
            database=self._database,
            relation_name_by_model={
                model.key.name: self._analysis.realized_project.relation_name_by_logical_key[
                    model.key
                ]
                for model in self._analysis.compiled_project.models
            },
        )
        warmup_states: dict[str, AuditWarmupState] = resolve_audit_warmup_states(
            audits=audits,
            anchors_by_model=anchors,
            materialized_model_names=materialized_model_names,
            warehouse_now=warehouse_now,
        )
        status_by_name: dict[str, dict[str, object]] = {
            str(status["name"]): status
            for status in build_checks_status_payload(
                analysis=self._analysis,
                connection=self._connection,
                database=self._database,
                project_dir=self._project_dir,
            )
            if status["kind"] == QualityNodeKind.AUDIT
        }
        audit_states: list[dict[str, object]] = [
            audit_schedule_entry(
                audit=audit,
                warmup=warmup_states[audit.name or audit.file_path.stem],
                status=status_by_name.get(audit.name or audit.file_path.stem),
                warehouse_now=warehouse_now,
                anchors=anchors,
            )
            for audit in audits
        ]
        blocked_by_failed_build: bool = (
            read_latest_direct_build_materialization(
                connection=self._connection,
                database=self._database,
                project_identity=logical_project_identity(project_dir=self._project_dir),
            )
            == MaterializationOutcome.FAILED
        )
        if blocked_by_failed_build:
            for audit_state in audit_states:
                if audit_state["state"] == AuditScheduleState.NOT_MATERIALIZED:
                    continue
                audit_state["state"] = AuditScheduleState.BLOCKED
                audit_state["blockedReason"] = "failed_build"
        due_count: int = sum(1 for item in audit_states if item["state"] == AuditScheduleState.DUE)
        return {
            "enabled": enabled,
            "state": (
                AuditScheduleState.DISABLED
                if not enabled
                else (
                    AuditScheduleState.BLOCKED
                    if blocked_by_failed_build
                    else (AuditScheduleState.DUE if due_count else AuditScheduleState.IDLE)
                )
            ),
            "warehouseNow": warehouse_now,
            "dueCount": due_count,
            "audits": audit_states,
        }

    @staticmethod
    def _referenced_model_names(audits: tuple[LoadedSqlAudit, ...]) -> tuple[str, ...]:
        model_names: set[str] = set()
        for audit in audits:
            model_names.update(audit.referenced_model_names)
        return tuple(sorted(model_names))
