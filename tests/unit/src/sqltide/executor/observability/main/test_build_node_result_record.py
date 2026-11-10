import json

import pytest

from sqltide._native import (
    build_node_result_record,
)
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.executor.observability.constants import MAX_OBSERVATION_ERROR_LENGTH
from sqltide.executor.observability.models import QualityResultContext
from sqltide.executor.observability.types import QualityResultTrigger
from tests.unit.src.sqltide.executor.observability.main._test_types import (
    BoundedNodeResultTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BoundedNodeResultTestCase(
            description="replaces oversized payload and truncates concise error",
            payload_size=20_000,
            error_size=MAX_OBSERVATION_ERROR_LENGTH + 1_000,
            expected_payload_json=(
                '{"missing_count":7,"original_bytes":20050,"payload_truncated":true,'
                '"unexpected_count":9}'
            ),
            expected_error_length=MAX_OBSERVATION_ERROR_LENGTH,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_oversized_node_diagnostics_when_building_result_then_payload_is_bounded(
    test_case: BoundedNodeResultTestCase,
) -> None:
    invocation: AdapterInvocationRecord = AdapterInvocationRecord(
        invocation_id="inv-1",
        project_identity="project",
        target_identity="analytics",
        command="test",
        mode=None,
        outcome="failed",
        exit_code=1,
        materialized_outcome=None,
        deployment_id=None,
        workflow_id=None,
        selected_node_count=1,
        started_at="2026-08-02 12:00:00.000",
        completed_at="2026-08-02 12:00:01.000",
        duration_ms=1_000,
        error_message=None,
        summary_json="{}",
        tool_version="1.2.3",
    )

    result: AdapterNodeResultRecord = build_node_result_record(
        invocation=invocation,
        identity=QualityNodeIdentity(
            node_kind="test",
            node_name="orders return ids",
            binding_key="binding",
            definition_fingerprint="definition",
            execution_fingerprint="execution",
        ),
        context=QualityResultContext(trigger=QualityResultTrigger.MANUAL),
        status="failed",
        severity=None,
        failure_count=1,
        payload={
            "rows": "x" * test_case.payload_size,
            "missing_count": 7,
            "unexpected_count": 9,
        },
        error_message="x" * test_case.error_size,
    )

    assert result.payload_json == test_case.expected_payload_json
    assert len(str(result.error_message)) == test_case.expected_error_length
    assert json.loads(result.payload_json)["payload_truncated"] is True
    assert json.loads(result.payload_json)["missing_count"] == 7
    assert json.loads(result.payload_json)["unexpected_count"] == 9
    assert result.trigger == "manual"
    assert result.node_name == "orders return ids"
    assert result.scheduled_for is None
    assert result.cadence_seconds is None
    assert result.warmup_seconds == 0
