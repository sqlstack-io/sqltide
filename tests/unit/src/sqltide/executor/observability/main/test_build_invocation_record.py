import json
from pathlib import Path
from time import monotonic_ns

import pytest

from sqltide._native import (
    build_invocation_record,
)
from sqltide.adapter.models import AdapterInvocationRecord
from sqltide.executor.observability.models import TerminalInvocation
from tests.unit.src.sqltide.executor.observability.main._test_types import (
    CompleteDestructionSummaryTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CompleteDestructionSummaryTestCase(
            description="large target reset evidence remains complete",
            command="reset target",
            remaining_object_count=2_000,
            expected_actor="alice",
            expected_payload_truncated=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_large_destruction_summary_when_building_record_then_evidence_is_not_truncated(
    test_case: CompleteDestructionSummaryTestCase,
    tmp_path: Path,
) -> None:
    summary: dict[str, object] = {
        "actor": {"username": test_case.expected_actor},
        "expectedChallenges": ["pl__orders"],
        "remainingObjects": [
            f"table_{index:04d}" for index in range(test_case.remaining_object_count)
        ],
    }
    terminal: TerminalInvocation = TerminalInvocation(
        project_dir=tmp_path,
        target_identity="uat",
        command=test_case.command,
        mode="destructive",
        outcome="failed",
        exit_code=1,
        materialized_outcome="applied",
        deployment_id=None,
        workflow_id="workflow-1",
        selected_node_count=1,
        error_message="partial failure",
        summary=summary,
    )

    record: AdapterInvocationRecord = build_invocation_record(
        started=("invocation-1", "2026-08-24 12:00:00.000", monotonic_ns()),
        terminal=terminal,
    )
    persisted: dict[str, object] = json.loads(record.summary_json)

    assert len(record.summary_json.encode()) > 16_384
    assert persisted["actor"] == {"username": test_case.expected_actor}
    assert len(persisted["remainingObjects"]) == test_case.remaining_object_count
    assert persisted.get("payload_truncated", False) is test_case.expected_payload_truncated


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
