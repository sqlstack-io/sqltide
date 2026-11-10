from pathlib import Path
from typing import cast

import pytest

from sqltide._native import execute_sql_audits
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.executor.auditing.models import SqlAuditRunResult
from tests.unit.src.sqltide.executor.auditing._test_types import ExecuteSqlAuditsTestCase
from tests.unit.src.sqltide.executor.auditing.helpers import FakeAuditClickHouseClient


@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteSqlAuditsTestCase(
            description="passes when audit query returns zero rows",
            audit_query='SELECT order_id FROM __ref("order_items") WHERE line_total < 0',
            resolver={"order_items": "analytics.tbl__order_items"},
            count_result_rows=((0,),),
            sample_column_names=(),
            sample_rows=(),
            expected_passed=True,
            expected_failing_row_count=0,
        ),
        ExecuteSqlAuditsTestCase(
            description="returns failing sample rows when audit finds violations",
            audit_query=(
                'SELECT order_id, line_total FROM __ref("order_items") WHERE line_total < 0'
            ),
            resolver={"order_items": "analytics.tbl__order_items"},
            count_result_rows=((2,),),
            sample_column_names=("order_id", "line_total"),
            sample_rows=(("ord_1", -5.0), ("ord_2", -12.3)),
            expected_passed=False,
            expected_failing_row_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_audits_when_executing_then_it_returns_expected_results(
    test_case: ExecuteSqlAuditsTestCase,
) -> None:
    loaded_audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("audits/order_events/audit.sql"),
        query=test_case.audit_query,
        referenced_model_names=("order_items",),
    )
    client: FakeAuditClickHouseClient = FakeAuditClickHouseClient(
        count_result_rows=test_case.count_result_rows,
        sample_column_names=test_case.sample_column_names,
        sample_rows=test_case.sample_rows,
    )

    result: SqlAuditRunResult = execute_sql_audits(
        loaded_audits=(loaded_audit,),
        resolver=test_case.resolver,
        client=cast(AdapterConnection, client),
        dialect="clickhouse",
    )

    assert result.audit_results[0].passed == test_case.expected_passed
    assert result.audit_results[0].failing_row_count == test_case.expected_failing_row_count
    assert "analytics.tbl__order_items" in client.queries[0]
    assert result.audit_results[0].sample_rows == test_case.sample_rows
