from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import build_audit_scheduler_payload
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.auditing.models import AuditWarmupState
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    AuditScheduleCalculationTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        AuditScheduleCalculationTestCase(
            description="unmaterialized model keeps audit out of the due batch",
            status_payloads=(),
            anchors_by_model={},
            warmup_anchor=None,
            eligible_at=None,
            warmup_eligible=False,
            materialization_outcome=None,
            expected_scheduler_state="idle",
            expected_state="not_materialized",
            expected_scheduled_for=None,
            expected_missing_relations=("orders",),
        ),
        AuditScheduleCalculationTestCase(
            description="first eligible audit is immediately due",
            status_payloads=(),
            anchors_by_model={"orders": "2026-08-08 11:00:00.000"},
            warmup_anchor="2026-08-08 11:00:00.000",
            eligible_at="2026-08-08 11:00:00.000",
            warmup_eligible=True,
            materialization_outcome=None,
            expected_scheduler_state="due",
            expected_state="due",
            expected_scheduled_for="2026-08-08 11:00:00.000",
        ),
        AuditScheduleCalculationTestCase(
            description="matching result schedules from latest completion",
            status_payloads=(
                {
                    "name": "orders are valid",
                    "kind": "audit",
                    "status": "passed",
                    "completedAt": "2026-08-08 11:59:00.000",
                },
            ),
            anchors_by_model={"orders": "2026-08-08 11:00:00.000"},
            warmup_anchor="2026-08-08 11:00:00.000",
            eligible_at="2026-08-08 11:00:00.000",
            warmup_eligible=True,
            materialization_outcome=None,
            expected_scheduler_state="idle",
            expected_state="scheduled",
            expected_scheduled_for="2026-08-08 12:04:00.000",
        ),
        AuditScheduleCalculationTestCase(
            description="identity drift creates one current overdue slot",
            status_payloads=(
                {
                    "name": "orders are valid",
                    "kind": "audit",
                    "status": "execution_changed",
                    "completedAt": "2026-08-08 11:59:00.000",
                },
            ),
            anchors_by_model={"orders": "2026-08-08 11:00:00.000"},
            warmup_anchor="2026-08-08 11:00:00.000",
            eligible_at="2026-08-08 11:00:00.000",
            warmup_eligible=True,
            materialization_outcome=None,
            expected_scheduler_state="due",
            expected_state="due",
            expected_scheduled_for="2026-08-08 12:00:00.000",
        ),
        AuditScheduleCalculationTestCase(
            description="positive warmup blocks execution until eligibility",
            status_payloads=(),
            anchors_by_model={"orders": "2026-08-08 11:55:00.000"},
            warmup_anchor="2026-08-08 11:55:00.000",
            eligible_at="2026-08-08 12:10:00.000",
            warmup_eligible=False,
            materialization_outcome=None,
            expected_scheduler_state="idle",
            expected_state="warming_up",
            expected_scheduled_for="2026-08-08 12:10:00.000",
        ),
        AuditScheduleCalculationTestCase(
            description="failed direct materialization blocks an otherwise due audit",
            status_payloads=(),
            anchors_by_model={"orders": "2026-08-08 11:00:00.000"},
            warmup_anchor="2026-08-08 11:00:00.000",
            eligible_at="2026-08-08 11:00:00.000",
            warmup_eligible=True,
            materialization_outcome="failed",
            expected_scheduler_state="blocked",
            expected_state="blocked",
            expected_scheduled_for="2026-08-08 11:00:00.000",
        ),
        AuditScheduleCalculationTestCase(
            description="first scheduler process quantizes missing-anchor slot",
            status_payloads=(),
            anchors_by_model={},
            warmup_anchor=None,
            eligible_at=None,
            warmup_eligible=True,
            materialization_outcome=None,
            expected_scheduler_state="due",
            expected_state="due",
            expected_scheduled_for="2026-08-08 12:00:00.000",
            warehouse_now="2026-08-08 12:00:01.125",
        ),
        AuditScheduleCalculationTestCase(
            description="audit scheduler process resolves the same missing-anchor slot",
            status_payloads=(),
            anchors_by_model={},
            warmup_anchor=None,
            eligible_at=None,
            warmup_eligible=True,
            materialization_outcome=None,
            expected_scheduler_state="due",
            expected_state="due",
            expected_scheduled_for="2026-08-08 12:00:00.000",
            warehouse_now="2026-08-08 12:00:04.875",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_audit_state_when_calculating_schedule_then_due_slot_is_deterministic(
    test_case: AuditScheduleCalculationTestCase,
) -> None:
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("/project/audits/orders.sql"),
        query='SELECT * FROM __ref("orders")',
        referenced_model_names=("orders",),
        name="orders are valid",
        cadence_seconds=300,
        scheduled=True,
    )
    analysis_mock: MagicMock = MagicMock()
    analysis_mock.compiled_project.audits = (audit,)
    analysis_mock.compile_inputs.virtual_environments = False
    analysis: CompileAnalysis = cast(CompileAnalysis, analysis_mock)
    connection_mock: MagicMock = MagicMock()
    connection_mock.capture_warehouse_timestamp.return_value = test_case.warehouse_now
    connection: AdapterConnection = cast(AdapterConnection, connection_mock)
    with (
        patch(
            "sqltide.executor.auditing.main._load_model_anchors.load_model_anchors",
            return_value=test_case.anchors_by_model,
        ),
        patch(
            "sqltide._native.resolve_audit_warmup_states",
            return_value={
                "orders are valid": AuditWarmupState(
                    eligible=test_case.warmup_eligible,
                    anchor=test_case.warmup_anchor,
                    eligible_at=test_case.eligible_at,
                    missing_model_names=test_case.expected_missing_relations,
                )
            },
        ),
        patch(
            "sqltide._native.build_checks_status_payload",
            return_value=list(test_case.status_payloads),
        ),
        patch(
            "sqltide._native.read_latest_direct_build_materialization",
            return_value=test_case.materialization_outcome,
        ),
    ):
        payload: dict[str, object] = build_audit_scheduler_payload(
            analysis=analysis,
            connection=connection,
            database="analytics",
            project_dir=Path("/project"),
        )

    audit_payload: dict[str, object] = cast(list[dict[str, object]], payload["audits"])[0]
    assert payload["state"] == test_case.expected_scheduler_state
    assert audit_payload["state"] == test_case.expected_state
    assert audit_payload["scheduledFor"] == test_case.expected_scheduled_for
    assert audit_payload["missingRelations"] == list(test_case.expected_missing_relations)


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
