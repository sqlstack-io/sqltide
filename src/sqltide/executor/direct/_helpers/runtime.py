"""Execute direct templates by realizing replay SQL from current-process captures."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError, AdapterResultError
from sqltide.adapter.models import AdapterQueryResult
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.executor.direct._helpers.workflow import realize_direct_replay_statement
from sqltide.executor.direct.exceptions import DirectWorkflowExecutionError
from sqltide.executor.direct.models import (
    DirectBuildWorkflow,
    DirectReplayCapture,
    DirectReplayRange,
    DirectRuntimeExecution,
    DirectRuntimeReplay,
)
from sqltide.executor.workflow.constants import RECOVER_MODEL_VIEW_STEP_PREFIX
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.main.execute_recovery_statements import (
    execute_recovery_statements,
)
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    WarehouseStatement,
    WorkflowExecutionResult,
    WorkflowStatementResult,
)
from sqltide.executor.workflow.types import WorkflowEventEmitter

_EMPTY_TEXT: str = ""
_PAUSED_SOURCE_STEP_PREFIXES: tuple[str, ...] = ("pause_source_", "clear_staged_source_")


def execute_direct_build_workflow(
    *,
    workflow: DirectBuildWorkflow,
    connection: AdapterConnection,
    emitter: WorkflowEventEmitter | None = None,
) -> DirectRuntimeExecution:
    """Execute a direct template while retaining every exact adapter statement."""

    prepared_statements: list[WarehouseStatement] = list(workflow.statements)
    _emit_workflow_prepared(emitter=emitter, statements=tuple(prepared_statements))
    runtime_by_replay_step: dict[str, DirectRuntimeReplay] = {
        runtime.replay_step_id: runtime for runtime in workflow.runtime_replays
    }
    runtime_by_capture_step: dict[str, DirectRuntimeReplay] = {
        runtime.capture_step_id: runtime for runtime in workflow.runtime_replays
    }
    captures_by_model: dict[str, DirectReplayCapture] = {}
    captures: list[DirectReplayCapture] = []
    attempted: list[WarehouseStatement] = []
    results: list[WorkflowStatementResult] = []
    completed_step_ids: set[str] = set()
    template_statement: WarehouseStatement
    for template_statement in workflow.template.statements:
        statement: WarehouseStatement = template_statement
        runtime_replay: DirectRuntimeReplay | None = runtime_by_replay_step.get(
            template_statement.step_id
        )
        try:
            if runtime_replay is not None:
                statement = realize_direct_replay_statement(
                    template_statement=template_statement,
                    runtime_replay=runtime_replay,
                    capture=captures_by_model[runtime_replay.model_name],
                    client=connection,
                )
                prepared_statements[statement.sequence - 1] = statement
                _emit_workflow_prepared(emitter=emitter, statements=tuple(prepared_statements))
            attempted.append(statement)
            step_execution: WorkflowExecutionResult = execute_warehouse_workflow(
                statements=(statement,),
                connection=connection,
                emitter=emitter,
            )
            results.extend(step_execution.statement_results)
            completed_step_ids.add(statement.step_id)
            capture_runtime: DirectRuntimeReplay | None = runtime_by_capture_step.get(
                statement.step_id
            )
            if capture_runtime is not None:
                capture: DirectReplayCapture = _decode_capture(
                    workflow_id=workflow.workflow_id,
                    target_database=capture_runtime.replay.database,
                    runtime_replay=capture_runtime,
                    result=step_execution.statement_results[0].query_result,
                )
                captures.append(capture)
                captures_by_model[capture.logical_model_name] = capture
        except (AdapterError, KeyError, KeyboardInterrupt, WorkflowExecutionError) as error:
            cause: BaseException = (
                error.cause if isinstance(error, WorkflowExecutionError) else error
            )
            failed_step_id: str = (
                error.failed_step_id
                if isinstance(error, WorkflowExecutionError)
                else statement.step_id
            )
            recovery_statements: tuple[WarehouseStatement, ...] = _recover_managed_sources(
                workflow=workflow,
                connection=connection,
                emitter=emitter,
                cause=cause,
                completed_step_ids=completed_step_ids,
                failed_step_id=failed_step_id,
                start_sequence=len(attempted) + 1,
            )
            attempted.extend(recovery_statements)
            raise DirectWorkflowExecutionError(
                workflow=_exact_workflow(workflow=workflow, statements=tuple(attempted)),
                partial_result=WorkflowExecutionResult(statement_results=tuple(results)),
                captures=tuple(captures),
                failed_step_id=failed_step_id,
                cause=cause,
            ) from cause
    execution: WorkflowExecutionResult = WorkflowExecutionResult(statement_results=tuple(results))
    return DirectRuntimeExecution(
        workflow=_exact_workflow(workflow=workflow, statements=tuple(attempted)),
        execution=execution,
        captures=tuple(captures),
    )


def _recover_managed_sources(
    *,
    workflow: DirectBuildWorkflow,
    connection: AdapterConnection,
    emitter: WorkflowEventEmitter | None,
    cause: BaseException,
    completed_step_ids: set[str],
    failed_step_id: str,
    start_sequence: int,
) -> tuple[WarehouseStatement, ...]:
    restores_views: bool = any(
        statement.step_id.startswith(RECOVER_MODEL_VIEW_STEP_PREFIX)
        for statement in workflow.source_recovery_statements
    )
    source_was_paused: bool = failed_step_id.startswith(_PAUSED_SOURCE_STEP_PREFIXES) or any(
        step_id.startswith(_PAUSED_SOURCE_STEP_PREFIXES) for step_id in completed_step_ids
    )
    if not source_was_paused and not restores_views:
        return ()
    return execute_recovery_statements(
        statements=tuple(
            statement
            for statement in workflow.source_recovery_statements
            if statement.step_id.replace("recover_", "activate_", 1) not in completed_step_ids
        ),
        connection=connection,
        emitter=emitter,
        cause=cause,
        start_sequence=start_sequence,
    )


def _emit_workflow_prepared(
    *, emitter: WorkflowEventEmitter | None, statements: tuple[WarehouseStatement, ...]
) -> None:
    if emitter is None:
        return
    workflow_sql: str = "\n".join(statement.sql for statement in statements)
    emitter.workflow_prepared(
        statements=statements,
        workflow_sha256=sha256(workflow_sql.encode("utf-8")).hexdigest(),
    )


def _decode_capture(
    *,
    workflow_id: str,
    target_database: str,
    runtime_replay: DirectRuntimeReplay,
    result: AdapterQueryResult | None,
) -> DirectReplayCapture:
    if result is None:
        raise AdapterResultError(
            f"Replay capture '{runtime_replay.capture_step_id}' returned no query result"
        )
    rows: tuple[Mapping[str, object], ...] = result.named_rows()
    row: Mapping[str, object]
    for row in rows:
        _validate_capture_row(runtime_replay=runtime_replay, row=row)
    ranges: tuple[DirectReplayRange, ...] = tuple(_decode_range(row=row) for row in rows)
    captured_at: str | None = _optional_text(row=rows[0], column="captured_at") if rows else None
    capture_identity: str = repr((workflow_id, runtime_replay.model_name, captured_at, ranges))
    return DirectReplayCapture(
        capture_id=sha256(capture_identity.encode()).hexdigest(),
        workflow_id=workflow_id,
        target_database=target_database,
        logical_model_name=runtime_replay.model_name,
        driving_input_relation_name=runtime_replay.replay.relations.anchor,
        boundary_mode=runtime_replay.replay.mode,
        captured_at=captured_at,
        ranges=ranges,
    )


def _optional_integer(*, row: Mapping[str, object], column: str) -> int | None:
    value: object = row.get(column)
    return None if value is None else int(str(value))


def _decode_range(*, row: Mapping[str, object]) -> DirectReplayRange:
    return DirectReplayRange(
        partition_value=_optional_text(row=row, column="partition_value"),
        source_partition_column_name=_optional_text(row=row, column="source_partition_column_name"),
        source_position_column_name=_required_text(row=row, column="source_position_column_name"),
        source_timestamp_column_name=_optional_text(row=row, column="source_timestamp_column_name"),
        lower_value=_required_text(row=row, column="lower_value"),
        upper_value=_required_text(row=row, column="upper_value"),
        replay_cutoff_value=_required_text(row=row, column="replay_cutoff_value"),
        cutoff_inclusive=bool(row.get("cutoff_inclusive", True)),
        anchor_row_count=_optional_integer(row=row, column="anchor_row_count"),
    )


def _validate_capture_row(
    *, runtime_replay: DirectRuntimeReplay, row: Mapping[str, object]
) -> None:
    expected_relation: str = runtime_replay.replay.relations.anchor
    actual_relation: str = _required_text(row=row, column="driving_input_relation_name")
    if actual_relation != expected_relation:
        raise AdapterResultError(
            f"Replay capture for '{runtime_replay.model_name}' returned driving input "
            f"'{actual_relation}' instead of '{expected_relation}'"
        )
    expected_mode: str = str(runtime_replay.replay.mode)
    actual_mode: str = _required_text(row=row, column="replay_boundary_mode")
    if actual_mode != expected_mode:
        raise AdapterResultError(
            f"Replay capture for '{runtime_replay.model_name}' returned boundary mode "
            f"'{actual_mode}' instead of '{expected_mode}'"
        )
    partition_value: str | None = _optional_text(row=row, column="partition_value")
    if runtime_replay.replay.mode == AdapterReplayBoundaryMode.OFFSETS:
        if partition_value is None:
            raise AdapterResultError(
                f"Replay capture for '{runtime_replay.model_name}' returned an offset boundary "
                "without a partition"
            )
        expected_key: str = f"_replay_partition={partition_value}"
    else:
        if partition_value is not None:
            raise AdapterResultError(
                f"Replay capture for '{runtime_replay.model_name}' returned scalar boundary mode "
                f"'{expected_mode}' with partition '{partition_value}'"
            )
        expected_key = f"_replay_{expected_mode}"
    actual_key: str = _required_text(row=row, column="boundary_key")
    if actual_key != expected_key:
        raise AdapterResultError(
            f"Replay capture for '{runtime_replay.model_name}' returned boundary key "
            f"'{actual_key}' instead of '{expected_key}'"
        )


def _required_text(*, row: Mapping[str, object], column: str) -> str:
    value: object | None = row.get(column)
    if value is None or str(value) == _EMPTY_TEXT:
        raise AdapterResultError(f"Replay capture row is missing required column '{column}'")
    return str(value)


def _optional_text(*, row: Mapping[str, object], column: str) -> str | None:
    value: object | None = row.get(column)
    return None if value is None or str(value) == _EMPTY_TEXT else str(value)


def _exact_workflow(
    *, workflow: DirectBuildWorkflow, statements: tuple[WarehouseStatement, ...]
) -> BuildWorkflow:
    return BuildWorkflow(
        mode=workflow.template.mode,
        plan_json=workflow.template.plan_json,
        statements=statements,
    )
