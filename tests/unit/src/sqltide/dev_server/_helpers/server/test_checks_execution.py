from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from sqltide._native import run_audit_batch
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterNodeResultRecord
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.executor.auditing.models import (
    AuditWarmupState,
    SqlAuditResult,
    SqlAuditRunResult,
)
from tests.unit.src.sqltide.dev_server._helpers.server._test_types import (
    AuditBatchWorkTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        AuditBatchWorkTestCase(
            description="two audits share one project setup and persistence boundary",
            expected_audit_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multiple_audits_when_running_batch_then_project_work_occurs_once(
    test_case: AuditBatchWorkTestCase,
) -> None:
    audits: tuple[LoadedSqlAudit, ...] = tuple(
        LoadedSqlAudit(
            file_path=Path(f"/project/audits/{name}.sql"),
            query="SELECT 1",
            referenced_model_names=(model_name,),
            name=name,
            severity="error",
            quality_identity=QualityNodeIdentity(
                node_kind="audit",
                node_name=name,
                binding_key=f"{name}-binding",
                definition_fingerprint=f"{name}-definition",
                execution_fingerprint=f"{name}-execution",
            ),
        )
        for name, model_name in (("orders valid", "orders"), ("customers valid", "customers"))
    )
    results: tuple[SqlAuditResult, ...] = tuple(
        SqlAuditResult(
            file_path=audit.file_path,
            referenced_model_names=audit.referenced_model_names,
            severity=audit.severity,
            passed=True,
            failing_row_count=0,
            sample_column_names=(),
            sample_rows=(),
            name=audit.name,
        )
        for audit in audits
    )
    analysis_mock: MagicMock = MagicMock()
    analysis_mock.compiled_project.audits = audits
    analysis_mock.realized_project.relation_name_by_logical_key = {}
    analysis_mock.compile_inputs.virtual_environments = False
    analysis_mock.adapter_profile.sql_analysis_dialect = "clickhouse"
    analysis: CompileAnalysis = cast(CompileAnalysis, analysis_mock)
    connection_mock: MagicMock = MagicMock()
    connection: AdapterConnection = cast(AdapterConnection, connection_mock)
    warmup_states: dict[str, AuditWarmupState] = {
        str(audit.name): AuditWarmupState(eligible=True, anchor=None, eligible_at=None)
        for audit in audits
    }
    with (
        patch(
            "sqltide.executor.auditing.main._load_model_anchors.load_model_anchors",
            return_value={},
        ) as load_anchors,
        patch(
            "sqltide.executor.auditing.main._load_materialized_model_names.load_materialized_model_names",
            return_value=frozenset({"orders", "customers"}),
        ) as load_materialized,
        patch(
            "sqltide._native.resolve_audit_warmup_states",
            return_value=warmup_states,
        ) as resolve_warmup,
        patch(
            "sqltide.executor.auditing.main.execute_sql_audits.execute_sql_audits",
            return_value=SqlAuditRunResult(audit_results=results),
        ) as execute_audits,
        patch(
            "sqltide.executor.observability.main.persist_terminal_observations.persist_terminal_observations",
            return_value=None,
        ) as persist_observations,
    ):
        payloads: list[dict[str, object]] = run_audit_batch(
            analysis=analysis,
            connection=connection,
            names=("orders valid", "customers valid"),
            project_dir=Path("/project"),
            database="analytics",
        )

    node_results: tuple[AdapterNodeResultRecord, ...] = persist_observations.call_args.kwargs[
        "node_results"
    ]
    assert [payload["name"] for payload in payloads] == ["orders valid", "customers valid"]
    assert load_anchors.call_count == 1
    assert load_materialized.call_count == 1
    assert resolve_warmup.call_count == 1
    assert connection_mock.capture_warehouse_timestamp.call_count == 1
    assert execute_audits.call_count == 1
    assert execute_audits.call_args.kwargs["loaded_audits"] == audits
    assert persist_observations.call_count == 1
    assert len(node_results) == test_case.expected_audit_count
    assert (
        persist_observations.call_args.kwargs["invocation"].selected_node_count
        == test_case.expected_audit_count
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
