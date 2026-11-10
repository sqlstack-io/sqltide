from pathlib import Path

import pytest

from sqltide._native import (
    persist_terminal_observations,
)
from sqltide.adapter.models import AdapterInvocationRecord
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.observability.main._test_types import (
    ObservationArtifactTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ObservationArtifactTestCase(
            description="publishes exact numbered and combined SQL before gateway execution",
            statements=(
                "INSERT INTO metadata.invocations VALUES (1);",
                "INSERT INTO metadata.results VALUES (2);",
            ),
            expected_workflow_sql=(
                "INSERT INTO metadata.invocations VALUES (1);\n"
                "INSERT INTO metadata.results VALUES (2);"
            ),
            expected_step_names=(
                "0001_record_terminal_observation_1.sql",
                "0002_record_terminal_observation_2.sql",
            ),
            expected_artifact_seen_before_execution=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_terminal_observations_when_persisting_then_artifacts_match_gateway_sql(
    test_case: ObservationArtifactTestCase,
    tmp_path: Path,
) -> None:
    artifact_root: Path = tmp_path / "target" / "run" / "observations" / "inv-1"
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        observation_statements=test_case.statements,
        required_artifact_path=artifact_root / "workflow.sql",
    )
    invocation: AdapterInvocationRecord = AdapterInvocationRecord(
        invocation_id="inv-1",
        project_identity=str(tmp_path),
        target_identity="analytics",
        command="test",
        mode=None,
        outcome="succeeded",
        exit_code=0,
        materialized_outcome=None,
        deployment_id=None,
        workflow_id=None,
        selected_node_count=0,
        started_at="2026-08-02 12:00:00.000",
        completed_at="2026-08-02 12:00:01.000",
        duration_ms=1_000,
        error_message=None,
        summary_json="{}",
        tool_version="1.2.3",
    )

    persist_terminal_observations(
        client=connection,
        database="metadata",
        invocation=invocation,
        node_results=(),
    )

    step_names: tuple[str, ...] = tuple(
        path.name for path in sorted((artifact_root / "steps").iterdir())
    )
    step_sql: tuple[str, ...] = tuple(
        path.read_text(encoding="utf-8") for path in sorted((artifact_root / "steps").iterdir())
    )
    assert (artifact_root / "workflow.sql").read_text(
        encoding="utf-8"
    ) == test_case.expected_workflow_sql
    assert step_names == test_case.expected_step_names
    assert step_sql == test_case.statements
    assert tuple(connection.workflow_mutation_statements) == test_case.statements
    assert (
        connection.artifact_seen_before_execution
        is test_case.expected_artifact_seen_before_execution
    )
