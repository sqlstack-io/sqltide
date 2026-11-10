import pytest

from sqltide._native import execute_repair_active_view
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.executor.repair.models import RepairActiveViewRequest, RepairActiveViewResult
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.repair.main._test_types import (
    RepairBindingTestCase,
    RepairCapabilityTestCase,
)
from tests.unit.src.sqltide.executor.repair.main.helpers import (
    RepairWorkflowAdapterConnection,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RepairBindingTestCase(
            description="replaces exactly one requested stable binding",
            request=RepairActiveViewRequest(
                default_database="analytics",
                table_name="tbl__orders_enriched",
                deployment_id="dep_b",
            ),
            expected_statement=(
                "CREATE OR REPLACE VIEW analytics.tbl__orders_enriched AS\n"
                "SELECT * FROM analytics.tbl__orders_enriched__dep_b;"
            ),
            expected_result=RepairActiveViewResult(
                table_name="tbl__orders_enriched",
                target_table_name="tbl__orders_enriched__dep_b",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_repair_target_when_rebinding_then_exact_workflow_sql_reaches_gateway(
    test_case: RepairBindingTestCase,
) -> None:
    connection: RepairWorkflowAdapterConnection = RepairWorkflowAdapterConnection()

    result: RepairActiveViewResult = execute_repair_active_view(
        request=test_case.request,
        client=connection,
    )

    assert connection.statements == [test_case.expected_statement]
    assert tuple(event[:2] for event in connection.target_mutation_lock_events) == (
        ("acquire", test_case.request.default_database),
        ("release", test_case.request.default_database),
    )
    assert result == test_case.expected_result


@pytest.mark.parametrize(
    "test_case",
    [
        RepairCapabilityTestCase(
            description="rejects repair before writes when stable bindings are unsupported",
            expected_error_fragment=(
                "Adapter 'clickhouse' does not support stable logical bindings"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adapter_without_stable_bindings_when_repairing_then_it_fails_before_writes(
    test_case: RepairCapabilityTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        stable_logical_bindings=False
    )

    with pytest.raises(AdapterCapabilityError, match=test_case.expected_error_fragment):
        execute_repair_active_view(
            request=RepairActiveViewRequest(
                default_database="analytics",
                table_name="tbl__orders_enriched",
                deployment_id="dep_b",
            ),
            client=connection,
        )

    assert connection.statements == []
