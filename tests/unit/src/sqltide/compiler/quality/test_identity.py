from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from tests.unit.src.sqltide.compiler.quality._test_types import (
    AuditIdentityComparisonTestCase,
    QualityExecutionIdentityTestCase,
)
from tests.unit.src.sqltide.compiler.quality.helpers import audit, build_test_case, loaded_test


@pytest.mark.parametrize(
    "test_case",
    (
        AuditIdentityComparisonTestCase(
            description="file move and whitespace-only SQL change preserve identity",
            first_name="order ids are present",
            first_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            first_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            first_severity="error",
            second_name="order ids are present",
            second_query=' SELECT  *\nFROM __ref("orders")\nWHERE order_id IS NULL ',
            second_resolved_query=" SELECT *\nFROM analytics.orders WHERE order_id IS NULL ",
            second_severity="error",
            expected_binding_match=True,
            expected_definition_match=True,
            expected_execution_match=True,
        ),
        AuditIdentityComparisonTestCase(
            description="name change creates a new logical identity",
            first_name="order ids are present",
            first_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            first_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            first_severity="error",
            second_name="orders require ids",
            second_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            second_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            second_severity="error",
            expected_binding_match=False,
            expected_definition_match=True,
            expected_execution_match=True,
        ),
        AuditIdentityComparisonTestCase(
            description="semantic SQL change updates definition and execution only",
            first_name="order ids are present",
            first_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            first_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            first_severity="error",
            second_name="order ids are present",
            second_query='SELECT * FROM __ref("orders") WHERE order_id = 0',
            second_resolved_query="SELECT * FROM analytics.orders WHERE order_id = 0",
            second_severity="error",
            expected_binding_match=True,
            expected_definition_match=False,
            expected_execution_match=False,
        ),
        AuditIdentityComparisonTestCase(
            description="target relation change updates execution only",
            first_name="order ids are present",
            first_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            first_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            first_severity="error",
            second_name="order ids are present",
            second_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            second_resolved_query="SELECT * FROM production.orders WHERE order_id IS NULL",
            second_severity="error",
            expected_binding_match=True,
            expected_definition_match=True,
            expected_execution_match=False,
        ),
        AuditIdentityComparisonTestCase(
            description="severity change updates binding without changing SQL fingerprints",
            first_name="order ids are present",
            first_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            first_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            first_severity="error",
            second_name="order ids are present",
            second_query='SELECT * FROM __ref("orders") WHERE order_id IS NULL',
            second_resolved_query="SELECT * FROM analytics.orders WHERE order_id IS NULL",
            second_severity="warning",
            expected_binding_match=False,
            expected_definition_match=True,
            expected_execution_match=True,
        ),
    ),
    ids=lambda case: case.description,
)
def test_given_audit_variants_when_building_identity_then_classifies_changes(
    test_case: AuditIdentityComparisonTestCase,
) -> None:
    first: QualityNodeIdentity = _native.build_audit_quality_identity(
        audit=audit(
            path=Path("/project/audits/original.sql"),
            name=test_case.first_name,
            query=test_case.first_query,
            severity=test_case.first_severity,
        ),
        resolved_query=test_case.first_resolved_query,
        dialect="clickhouse",
    )
    second: QualityNodeIdentity = _native.build_audit_quality_identity(
        audit=audit(
            path=Path("/project/audits/moved.sql"),
            name=test_case.second_name,
            query=test_case.second_query,
            severity=test_case.second_severity,
        ),
        resolved_query=test_case.second_resolved_query,
        dialect="clickhouse",
    )

    assert (first.binding_key == second.binding_key) is test_case.expected_binding_match
    assert (
        first.definition_fingerprint == second.definition_fingerprint
    ) is test_case.expected_definition_match
    assert (
        first.execution_fingerprint == second.execution_fingerprint
    ) is test_case.expected_execution_match


@pytest.mark.parametrize(
    "test_case",
    [
        QualityExecutionIdentityTestCase(
            description="resolved model SQL changes execution identity only",
            first_model_query="SELECT order_id FROM model_orders_v1",
            second_model_query="SELECT order_id FROM model_orders_v2",
            expected_binding_match=True,
            expected_definition_match=True,
            expected_execution_match=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unchanged_test_when_model_sql_changes_then_only_execution_changes(
    test_case: QualityExecutionIdentityTestCase,
) -> None:
    loaded: LoadedSqlTest = loaded_test()
    first: QualityNodeIdentity = _native.build_test_quality_identity(
        loaded_test=loaded,
        test_case=build_test_case(test_case.first_model_query),
        dialect="clickhouse",
    )
    second: QualityNodeIdentity = _native.build_test_quality_identity(
        loaded_test=loaded,
        test_case=build_test_case(test_case.second_model_query),
        dialect="clickhouse",
    )

    assert (first.binding_key == second.binding_key) is test_case.expected_binding_match
    assert (
        first.definition_fingerprint == second.definition_fingerprint
    ) is test_case.expected_definition_match
    assert (
        first.execution_fingerprint == second.execution_fingerprint
    ) is test_case.expected_execution_match


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
