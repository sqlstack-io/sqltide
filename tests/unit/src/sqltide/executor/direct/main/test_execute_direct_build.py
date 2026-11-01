import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from sqltide.adapter.exceptions import AdapterResultError
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterReplayOffsetProgressRequest,
)
from sqltide.cli.plan.main._render_direct_plan_json import render_direct_plan_json
from sqltide.cli.workflow_artifacts.main._publish_build_workflow import publish_build_workflow
from sqltide.compiler.compile.models import CompiledModel, CompiledTableModel
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.executor.direct.exceptions import DirectBuildError, DirectWorkflowExecutionError
from sqltide.executor.direct.main.assemble_direct_build_workflow import (
    assemble_direct_build_workflow,
)
from sqltide.executor.direct.main.execute_direct_build_workflow import (
    execute_direct_build_workflow,
)
from sqltide.executor.direct.main.persist_direct_fingerprints import (
    persist_direct_fingerprints,
)
from sqltide.executor.direct.models import (
    DirectBuildRequest,
    DirectBuildWorkflow,
    DirectRuntimeExecution,
)
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    PublishedBuildWorkflow,
    WarehouseStatement,
)
from sqltide.executor.workflow.types import StatementIntent, WorkflowMode, WorkflowPhase
from tests.unit.src.sqltide.compiler.planner.helpers import build_direct_snapshot
from tests.unit.src.sqltide.executor.direct.main._test_types import (
    DirectCaptureValidationTestCase,
    DirectDistinctCaptureTestCase,
    DirectFingerprintMetadataTestCase,
    DirectFingerprintPersistenceTestCase,
    DirectNoOpWorkflowTestCase,
    DirectPersistenceFailureTestCase,
    DirectSourceRecoveryTestCase,
    DirectSourceScopeTestCase,
    DirectWorkflowTestCase,
)
from tests.unit.src.sqltide.executor.direct.main.helpers import (
    DeniedFingerprintRenderingConnection,
    DistinctCaptureDirectBuildConnection,
    FailingPausedSourceConnection,
    InvalidOffsetCaptureDirectBuildConnection,
    MismatchedCaptureDirectBuildConnection,
    RecordingDirectBuildConnection,
    RecordingFingerprintConnection,
    build_direct_execution_request,
    build_direct_execution_request_with_unrelated_pipeline,
    build_direct_execution_snapshot,
)
from tests.unit.src.sqltide.executor.workflow.helpers import (
    FailingPreparationEmitter,
    RecordingWorkflowEmitter,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectSourceScopeTestCase(
            description="selected closure prepares only its managed source",
            expected_source_step_ids=(
                "prepare_source_kafka__orders",
                "prepare_source_raw__orders",
                "bootstrap_source_mv__orders",
                "pause_source_mv__orders",
                "activate_source_mv__orders",
            ),
            unexpected_source_step_ids=(
                "prepare_source_kafka__unrelated",
                "prepare_source_raw__unrelated",
                "activate_source_mv__unrelated",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_selected_direct_closure_when_assembling_then_unrelated_sources_are_omitted(
    test_case: DirectSourceScopeTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request_with_unrelated_pipeline(
        project_root=tmp_path,
        selected_model_names=("alpha",),
    )
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=RecordingDirectBuildConnection(),
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    step_ids: set[str] = {statement.step_id for statement in workflow.statements}
    ordered_step_ids: tuple[str, ...] = tuple(
        statement.step_id for statement in workflow.statements
    )
    assert set(test_case.expected_source_step_ids) <= step_ids
    assert set(test_case.unexpected_source_step_ids).isdisjoint(step_ids)
    assert ordered_step_ids.index("activate_source_mv__orders") > ordered_step_ids.index(
        "replay_alpha"
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DirectNoOpWorkflowTestCase(
            description="an empty changed plan creates no warehouse statements",
            expected_statement_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_changed_models_when_assembling_then_workflow_is_empty(
    test_case: DirectNoOpWorkflowTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=(),
        selection_mode=DirectSelectionMode.CHANGED,
    )

    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=RecordingDirectBuildConnection(),
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    assert len(workflow.statements) == test_case.expected_statement_count


@pytest.mark.parametrize(
    "test_case",
    [
        DirectWorkflowTestCase(
            description="direct workflow publishes and executes exact ordered statement bytes",
            expected_first_phase=WorkflowPhase.PREPARATION,
            expected_last_phase=WorkflowPhase.REPLAY,
            expected_replay_count=2,
            expected_boundary_model_segments=("beta", "delta"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_plan_when_assembling_then_complete_exact_workflow_is_authoritative(
    test_case: DirectWorkflowTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    connection: RecordingDirectBuildConnection = DistinctCaptureDirectBuildConnection()
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )
    emitter: RecordingWorkflowEmitter = RecordingWorkflowEmitter()
    runtime_execution: DirectRuntimeExecution = execute_direct_build_workflow(
        workflow=workflow,
        connection=connection,
        emitter=emitter,
    )
    exact_workflow: BuildWorkflow = runtime_execution.workflow
    published: PublishedBuildWorkflow = publish_build_workflow(
        target_dir=tmp_path / "target",
        workflow=exact_workflow,
    )

    statements: tuple[WarehouseStatement, ...] = exact_workflow.statements
    all_sql: tuple[str, ...] = tuple(statement.sql for statement in statements)
    step_bytes: tuple[str, ...] = tuple(
        path.read_text(encoding="utf-8")
        for path in sorted((published.artifact_root / "steps").iterdir())
    )
    assert statements[0].phase == test_case.expected_first_phase
    assert statements[-1].phase == test_case.expected_last_phase
    assert tuple(statement.sequence for statement in statements) == tuple(
        range(1, len(statements) + 1)
    )
    assert all(
        statement.sql.endswith(";") and not statement.sql.endswith(";;") for statement in statements
    )
    assert sum(statement.step_id.startswith("replay_") for statement in statements) == (
        test_case.expected_replay_count
    )
    expected_boundary_windows: tuple[tuple[str, ...], ...] = tuple(
        (
            f"capture_replay_{model_segment}",
            f"replay_{model_segment}",
        )
        for model_segment in test_case.expected_boundary_model_segments
    )
    expected_runtime_step_ids: tuple[str, ...] = sum(expected_boundary_windows, ())
    actual_runtime_step_ids: tuple[str, ...] = tuple(
        statement.step_id for statement in statements[-(len(expected_runtime_step_ids) + 1) : -1]
    )
    runtime_statements: tuple[WarehouseStatement, ...] = statements[
        -(len(expected_runtime_step_ids) + 1) : -1
    ]
    replay_progress_requests: tuple[AdapterReplayOffsetProgressRequest | None, ...] = tuple(
        statement.replay_offset_progress for statement in runtime_statements[1::2]
    )
    assert actual_runtime_step_ids == expected_runtime_step_ids
    assert all(request is not None and request.ranges for request in replay_progress_requests)
    assert step_bytes == tuple(statement.sql for statement in statements)
    assert (published.artifact_root / "workflow.sql").read_text(encoding="utf-8") == (
        "\n".join(statement.sql for statement in statements)
    )
    assert connection.catalog_databases == []
    assert set(connection.workflow_mutation_statements) <= set(all_sql)
    assert emitter.prepared_workflows[0] == workflow.statements
    assert emitter.prepared_workflows[-1] == exact_workflow.statements


@pytest.mark.parametrize(
    "test_case",
    [
        DirectPersistenceFailureTestCase(
            description="statement persistence failure aborts before warehouse mutations",
            selected_model_names=("beta",),
            expected_error_fragment="statement persistence failed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_persistence_failure_when_executing_direct_build_then_warehouse_is_untouched(
    test_case: DirectPersistenceFailureTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=test_case.selected_model_names,
    )
    connection: RecordingDirectBuildConnection = RecordingDirectBuildConnection()
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    with pytest.raises(RuntimeError, match=test_case.expected_error_fragment):
        _ = execute_direct_build_workflow(
            workflow=workflow,
            connection=connection,
            emitter=FailingPreparationEmitter(),
        )

    assert connection.workflow_mutation_statements == []


@pytest.mark.parametrize(
    "test_case",
    [
        DirectSourceRecoveryTestCase(
            description="paused managed source is recovered after a build failure",
            expected_failed_step_id="fail_after_source_pause",
            expected_attempted_mutations=(
                "DROP TABLE IF EXISTS analytics.mv__orders SYNC;",
                "SELECT invalid;",
                "CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv__orders "
                "TO analytics.raw__orders AS SELECT 1;",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_paused_managed_source_when_direct_build_fails_then_source_is_recovered(
    test_case: DirectSourceRecoveryTestCase,
) -> None:
    pause_statement: WarehouseStatement = WarehouseStatement(
        sequence=1,
        step_id="pause_source_mv__orders",
        phase=WorkflowPhase.PREPARATION,
        intent=StatementIntent.MUTATION,
        sql="DROP TABLE IF EXISTS analytics.mv__orders SYNC;",
    )
    failing_statement: WarehouseStatement = WarehouseStatement(
        sequence=2,
        step_id="fail_after_source_pause",
        phase=WorkflowPhase.TEARDOWN,
        intent=StatementIntent.MUTATION,
        sql="SELECT invalid;",
    )
    recovery_statement: WarehouseStatement = WarehouseStatement(
        sequence=1,
        step_id="recover_source_mv__orders",
        phase=WorkflowPhase.FINALIZATION,
        intent=StatementIntent.MUTATION,
        sql=(
            "CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv__orders "
            "TO analytics.raw__orders AS SELECT 1;"
        ),
    )
    workflow: DirectBuildWorkflow = DirectBuildWorkflow(
        template=BuildWorkflow(
            mode=WorkflowMode.DIRECT,
            plan_json="{}",
            statements=(pause_statement, failing_statement),
        ),
        runtime_replays=(),
        workflow_id="workflow-1",
        source_recovery_statements=(recovery_statement,),
    )
    connection: FailingPausedSourceConnection = FailingPausedSourceConnection()

    with pytest.raises(DirectWorkflowExecutionError) as raised:
        execute_direct_build_workflow(workflow=workflow, connection=connection)

    assert tuple(connection.attempted_mutations) == test_case.expected_attempted_mutations
    assert raised.value.failed_step_id == test_case.expected_failed_step_id
    assert raised.value.workflow.statements[-1].step_id == "recover_source_mv__orders"


@pytest.mark.parametrize(
    "test_case",
    [
        DirectDistinctCaptureTestCase(
            description="each replay root consumes its own process-owned capture",
            expected_capture_models=("beta", "delta"),
            expected_replay_sql_fragments=(
                "anchor._replay_offset <= 11",
                "anchor._replay_offset <= 21",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_distinct_root_captures_when_executing_then_each_replay_uses_matching_capture(
    test_case: DirectDistinctCaptureTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    connection: DistinctCaptureDirectBuildConnection = DistinctCaptureDirectBuildConnection()
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    runtime_execution: DirectRuntimeExecution = execute_direct_build_workflow(
        workflow=workflow,
        connection=connection,
    )

    statements_by_id: dict[str, WarehouseStatement] = {
        statement.step_id: statement for statement in runtime_execution.workflow.statements
    }
    replay_sql: tuple[str, ...] = tuple(
        statements_by_id[f"replay_{model_name}"].sql
        for model_name in test_case.expected_capture_models
    )
    assert tuple(capture.logical_model_name for capture in runtime_execution.captures) == (
        test_case.expected_capture_models
    )
    assert tuple(
        fragment in sql
        for fragment, sql in zip(test_case.expected_replay_sql_fragments, replay_sql, strict=True)
    ) == (True, True)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectCaptureValidationTestCase(
            description="capture identity must match the replay root",
            expected_error_fragment="tbl__wrong_root' instead of 'tbl__alpha",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mismatched_capture_identity_when_executing_then_replay_fails_closed(
    test_case: DirectCaptureValidationTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    connection: MismatchedCaptureDirectBuildConnection = MismatchedCaptureDirectBuildConnection()
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    with pytest.raises(DirectWorkflowExecutionError) as raised:
        execute_direct_build_workflow(workflow=workflow, connection=connection)

    assert isinstance(raised.value.cause, AdapterResultError)
    assert test_case.expected_error_fragment in str(raised.value.cause)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectCaptureValidationTestCase(
            description="offset capture must identify its partition",
            expected_error_fragment="offset boundary without a partition",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_partitionless_offset_capture_when_executing_then_replay_fails_closed(
    test_case: DirectCaptureValidationTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    connection: InvalidOffsetCaptureDirectBuildConnection = (
        InvalidOffsetCaptureDirectBuildConnection()
    )
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=build_direct_execution_snapshot(),
        plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
    )

    with pytest.raises(DirectWorkflowExecutionError) as raised:
        execute_direct_build_workflow(workflow=workflow, connection=connection)

    assert isinstance(raised.value.cause, AdapterResultError)
    assert test_case.expected_error_fragment in str(raised.value.cause)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectCaptureValidationTestCase(
            description="forced start time requires the replay input to exist",
            expected_error_fragment=(
                "Direct --start-time requires existing replay input 'tbl__alpha' for model 'beta'"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_forced_start_time_without_replay_input_when_assembling_then_build_is_refused(
    test_case: DirectCaptureValidationTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    bounded_request: DirectBuildRequest = replace(
        request, plan=replace(request.plan, effective_start_time="2026-04-08 13:00:00")
    )

    with pytest.raises(DirectBuildError, match=re.escape(test_case.expected_error_fragment)):
        _ = assemble_direct_build_workflow(
            request=bounded_request,
            client=RecordingDirectBuildConnection(),
            snapshot=build_direct_snapshot(relation_names=("mv__alpha",)),
            plan_json=render_direct_plan_json(plan=request.plan, adapter_name="clickhouse"),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        DirectFingerprintPersistenceTestCase(
            description="adapter rendering denial remains an optional fingerprint warning",
            expected_warning_fragment="injected fingerprint rendering denial",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_denied_fingerprint_rendering_when_persisting_then_warning_is_returned(
    test_case: DirectFingerprintPersistenceTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=("beta",),
    )
    connection: DeniedFingerprintRenderingConnection = DeniedFingerprintRenderingConnection()

    warning: str | None = persist_direct_fingerprints(request=request, connection=connection)

    assert warning is not None
    assert test_case.expected_warning_fragment in warning


@pytest.mark.parametrize(
    "test_case",
    [
        DirectFingerprintMetadataTestCase(
            description="records MODEL storage in the direct baseline",
            model_name="beta",
            expected_storage_key="storage",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_table_model_when_persisting_fingerprint_then_storage_identity_is_recorded(
    test_case: DirectFingerprintMetadataTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_direct_execution_request(
        project_root=tmp_path,
        selected_model_names=(test_case.model_name,),
    )
    connection: RecordingFingerprintConnection = RecordingFingerprintConnection()

    warning: str | None = persist_direct_fingerprints(request=request, connection=connection)

    fingerprint_by_identity: dict[str, AdapterDirectFingerprintRecord] = {
        record.logical_model_identity: record for record in connection.fingerprints
    }
    model_by_name: dict[str, CompiledModel] = {
        model.key.name: model for model in request.realized_project.project.models
    }
    metadata: dict = json.loads(
        fingerprint_by_identity[f"analytics.{test_case.model_name}"].identity_metadata
    )
    assert warning is None
    model: CompiledModel = model_by_name[test_case.model_name]
    assert isinstance(model, CompiledTableModel)
    assert metadata[test_case.expected_storage_key] == {
        "engine": model.transform.engine,
        "order_by": list(model.transform.order_by),
        "partition_by": model.transform.partition_by,
        "ttl": model.transform.ttl,
        "settings": model.transform.settings,
    }
