import json

import pytest

from sqltide._native import (
    BuildCancellationCoordinator,
)
from sqltide.adapter.models import AdapterQueryCancellation
from sqltide.dev_server.models import BuildCancellationOutcome
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    BuildCancellationPersistenceTestCase,
)
from tests.unit.src.sqltide.dev_server.classes.helpers import (
    CancellationRecordingConnection,
    CancellationWarehouseRuntime,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BuildCancellationPersistenceTestCase(
            description="confirmed query cancellation is retained after process exit",
            invocation_id="run-123",
            query_id="query-456",
            outcome_status="cancelled",
            termination_confirmed=True,
            error_message=None,
            expected_event_kind="cancellation_completed",
            expected_sequence=8,
        ),
        BuildCancellationPersistenceTestCase(
            description="unconfirmed query cancellation failure is retained after process exit",
            invocation_id="run-789",
            query_id="query-012",
            outcome_status="cancellation_failed",
            termination_confirmed=False,
            error_message="ClickHouse query remained active",
            expected_event_kind="cancellation_failed",
            expected_sequence=12,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_query_cancellation_outcome_when_process_exits_then_evidence_is_retained(
    test_case: BuildCancellationPersistenceTestCase,
) -> None:
    connection: CancellationRecordingConnection = CancellationRecordingConnection(
        next_sequence=test_case.expected_sequence
    )
    coordinator: BuildCancellationCoordinator = BuildCancellationCoordinator(
        warehouse=CancellationWarehouseRuntime(connection).as_runtime(),
        database="analytics",
    )

    cancellation: AdapterQueryCancellation = coordinator.cancel_query(test_case.query_id)
    coordinator.record_outcome(
        BuildCancellationOutcome(
            invocation_id=test_case.invocation_id,
            query_id=test_case.query_id,
            requested_at="2026-09-07T10:00:00.000+00:00",
            status=test_case.outcome_status,
            process_exit_code=130,
            warehouse_supported=cancellation.supported,
            query_found=cancellation.query_found,
            warehouse_termination_confirmed=test_case.termination_confirmed,
            error_message=test_case.error_message,
        )
    )

    assert connection.cancelled_query_ids == [test_case.query_id]
    assert len(connection.run_events) == 2
    requested, completed = connection.run_events
    assert requested.sequence == test_case.expected_sequence
    assert requested.event_kind == "cancellation_requested"
    assert completed.sequence == test_case.expected_sequence + 1
    assert completed.event_kind == test_case.expected_event_kind
    payload: dict[str, object] = json.loads(completed.payload_json)
    assert payload["queryId"] == test_case.query_id
    assert payload["errorMessage"] == test_case.error_message


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
