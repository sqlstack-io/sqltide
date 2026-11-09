from pathlib import Path

import pytest

from sqltide._native import resolve_audit_warmup_states
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.executor.auditing.models import AuditWarmupState
from tests.unit.src.sqltide.executor.auditing._test_types import AuditWarmupStateTestCase


@pytest.mark.parametrize(
    "test_case",
    (
        AuditWarmupStateTestCase(
            description="unmaterialized referenced models defer the audit",
            warmup_seconds=900,
            anchors_by_model={},
            materialized_model_names=frozenset(),
            warehouse_now="2026-08-08 12:00:00.000",
            expected_eligible=False,
            expected_anchor=None,
            expected_eligible_at=None,
            expected_missing_model_names=("orders", "customers"),
        ),
        AuditWarmupStateTestCase(
            description="historical anchors preserve errors for disappeared relations",
            warmup_seconds=0,
            anchors_by_model={
                "orders": "2026-08-08 11:59:59.000",
                "customers": "2026-08-08 11:59:58.000",
            },
            materialized_model_names=frozenset(),
            warehouse_now="2026-08-08 12:00:00.000",
            expected_eligible=True,
            expected_anchor="2026-08-08 11:59:59.000",
            expected_eligible_at="2026-08-08 11:59:59.000",
            expected_missing_model_names=(),
        ),
        AuditWarmupStateTestCase(
            description="zero warmup is immediately eligible at latest anchor",
            warmup_seconds=0,
            anchors_by_model={"orders": "2026-08-08 11:59:59.000"},
            materialized_model_names=frozenset({"orders", "customers"}),
            warehouse_now="2026-08-08 12:00:00.000",
            expected_eligible=True,
            expected_anchor="2026-08-08 11:59:59.000",
            expected_eligible_at="2026-08-08 11:59:59.000",
            expected_missing_model_names=(),
        ),
        AuditWarmupStateTestCase(
            description="newest referenced model restarts positive warmup",
            warmup_seconds=900,
            anchors_by_model={
                "orders": "2026-08-08 11:00:00.000",
                "customers": "2026-08-08 11:55:00.000",
            },
            materialized_model_names=frozenset({"orders", "customers"}),
            warehouse_now="2026-08-08 12:00:00.000",
            expected_eligible=False,
            expected_anchor="2026-08-08 11:55:00.000",
            expected_eligible_at="2026-08-08 12:10:00.000",
            expected_missing_model_names=(),
        ),
        AuditWarmupStateTestCase(
            description="completed warmup is eligible",
            warmup_seconds=300,
            anchors_by_model={"orders": "2026-08-08 11:55:00.000"},
            materialized_model_names=frozenset({"orders", "customers"}),
            warehouse_now="2026-08-08 12:00:00.000",
            expected_eligible=True,
            expected_anchor="2026-08-08 11:55:00.000",
            expected_eligible_at="2026-08-08 12:00:00.000",
            expected_missing_model_names=(),
        ),
    ),
    ids=lambda case: case.description,
)
def test_given_model_anchors_when_resolving_warmup_then_eligibility_is_deterministic(
    test_case: AuditWarmupStateTestCase,
) -> None:
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("/project/audits/orders.sql"),
        query='SELECT * FROM __ref("orders")',
        referenced_model_names=("orders", "customers"),
        name="orders are valid",
        warmup_seconds=test_case.warmup_seconds,
    )

    state: AuditWarmupState = resolve_audit_warmup_states(
        audits=(audit,),
        anchors_by_model=test_case.anchors_by_model,
        materialized_model_names=test_case.materialized_model_names,
        warehouse_now=test_case.warehouse_now,
    )["orders are valid"]

    assert state.eligible is test_case.expected_eligible
    assert state.anchor == test_case.expected_anchor
    assert state.eligible_at == test_case.expected_eligible_at
    assert state.missing_model_names == test_case.expected_missing_model_names


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
