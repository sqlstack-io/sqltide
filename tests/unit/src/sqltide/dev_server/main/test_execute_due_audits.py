from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import execute_due_audits
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterNodeResultRecord
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.quality.models import QualityNodeIdentity
from tests.unit.src.sqltide.dev_server.main._test_types import (
    ScheduledBatchFailureTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ScheduledBatchFailureTestCase(
            description="batch exception marks every selected logical slot as attempted",
            error_message="result shape unavailable",
            scheduled_for="2026-08-08 12:00:00.000",
            expected_status="error",
            expected_result_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_selected_due_audit_when_batch_fails_then_error_result_keeps_original_slot(
    test_case: ScheduledBatchFailureTestCase,
) -> None:
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("/project/audits/orders.sql"),
        query="SELECT 1",
        referenced_model_names=(),
        name="orders are valid",
        severity="error",
        cadence_seconds=300,
        scheduled=True,
        quality_identity=QualityNodeIdentity(
            node_kind="audit",
            node_name="orders are valid",
            binding_key="binding",
            definition_fingerprint="definition",
            execution_fingerprint="execution",
        ),
    )
    analysis_mock: MagicMock = MagicMock()
    analysis_mock.compiled_project.audits = (audit,)
    analysis_mock.compiled_project.models = ()
    analysis: CompileAnalysis = cast(CompileAnalysis, analysis_mock)
    connection: AdapterConnection = cast(AdapterConnection, MagicMock())
    with (
        patch(
            "sqltide.executor.auditing.main.execute_sql_audits.execute_sql_audits",
            side_effect=RuntimeError(test_case.error_message),
        ),
        patch(
            "sqltide.executor.observability.main.persist_terminal_observations.persist_terminal_observations",
            return_value=None,
        ) as persist_observations,
    ):
        result_count: int = execute_due_audits(
            analysis=analysis,
            connection=connection,
            database="analytics",
            project_dir=Path("/project"),
            due=(
                {
                    "name": "orders are valid",
                    "scheduledFor": test_case.scheduled_for,
                },
            ),
        )

    node_results: tuple[AdapterNodeResultRecord, ...] = persist_observations.call_args.kwargs[
        "node_results"
    ]
    assert result_count == test_case.expected_result_count
    assert len(node_results) == test_case.expected_result_count
    assert node_results[0].status == test_case.expected_status
    assert node_results[0].scheduled_for == test_case.scheduled_for


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
