import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sqltide._native import InMemoryDestructionPlanStore, execute_destruction
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.executor.destruction.exceptions import (
    DestructionDriftError,
    DestructionPlanNotFoundError,
    DestructionRecordingError,
)
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionExecutionResult,
    DestructionPlan,
)
from sqltide.executor.workflow.models import WarehouseStatement
from tests.unit.src.sqltide.executor.destruction._test_types import (
    DestructionExecutionTestCase,
)
from tests.unit.src.sqltide.executor.destruction.helpers import (
    DestructionExecutionConnection,
    DestructionObservationConnection,
    InterruptingDestructionExecutionConnection,
    build_execution_plan,
    build_execution_statements,
)

_NOW: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
_ACTOR: DestructionActor = DestructionActor(actor_id="actor-1", actor_name="Alice")

_INITIALIZE_OBSERVABILITY: str = "sqltide._native.initialize_observability"
_PERSIST_TERMINAL_OBSERVATIONS: str = "sqltide._native.persist_terminal_observations"
_ASSEMBLE_DESTRUCTION_WORKFLOW: str = "sqltide._native.assemble_destruction_workflow"


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="workflow preparation failure retains the reviewed plan",
            expected_pending_sequences=(1, 2),
            expected_remaining_names=("relation_one", "relation_two"),
            expected_residual_status="not_mutated",
            expected_failure_phase="workflow_prepared",
            expected_error_match="injected workflow prepared failure",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_workflow_prepared_failure_when_destroying_then_plan_is_unconsumed_and_no_drop_runs(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = replace(
        build_execution_plan(now=_NOW),
        relation_drop_size_limit=107_374_182_400,
        relation_drop_size_server_limit=50_000_000_000,
        relation_drop_size_override=107_374_182_400,
    )
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    observation.fail_workflow_prepared = True
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(
        _ASSEMBLE_DESTRUCTION_WORKFLOW,
        lambda **_: statements,
    )
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    with pytest.raises(RuntimeError, match=test_case.expected_error_match):
        execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=connection,
            observation_connection=observation,
            project_dir=tmp_path,
            replan=lambda: plan,
        )

    assert store.get(plan_id=plan.plan_id, actor="actor-1") is plan
    assert connection.drop_names == []
    assert connection.tombstone_names == []
    assert len(invocations) == 1
    started_payload: dict[str, object] = json.loads(observation.run_events[0].payload_json)
    started_evidence: dict[str, object] = cast(
        dict[str, object], started_payload["operationEvidence"]
    )
    assert started_evidence["actor"] == {"id": "actor-1", "username": "Alice"}
    assert started_evidence["planId"] == plan.plan_id
    assert started_evidence["operationKind"] == plan.operation.value
    assert started_evidence["originalSelection"] == list(plan.requested_pipeline_names)
    assert started_evidence["includedDependentPipelines"] == list(
        plan.included_dependent_pipeline_names
    )
    assert started_evidence["submittedChallenges"] == list(plan.challenges)
    assert started_evidence["relationDropSizeLimit"] == 107_374_182_400
    assert started_evidence["relationDropSizeServerLimit"] == 50_000_000_000
    assert started_evidence["relationDropSizeOverride"] == 107_374_182_400
    assert started_evidence["completedStatementSequences"] == []
    assert started_evidence["pendingStatementSequences"] == list(
        test_case.expected_pending_sequences
    )
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["completedStatementSequences"] == []
    assert summary["operationKind"] == started_evidence["operationKind"]
    assert summary["originalSelection"] == started_evidence["originalSelection"]
    assert summary["includedDependentPipelines"] == started_evidence["includedDependentPipelines"]
    assert summary["pendingStatementSequences"] == list(test_case.expected_pending_sequences)
    assert summary["remainingObjects"] == list(test_case.expected_remaining_names or ())
    assert summary["residualCatalogStatus"] == test_case.expected_residual_status
    assert summary["failurePhase"] == test_case.expected_failure_phase
    assert summary["relationDropSizeLimit"] == 107_374_182_400
    assert summary["relationDropSizeServerLimit"] == 50_000_000_000
    assert summary["relationDropSizeOverride"] == 107_374_182_400


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="locked drift rejection retains the reviewed plan and exact evidence",
            expected_pending_sequences=(1, 2),
            expected_remaining_names=("relation_one", "relation_two"),
            expected_residual_status="not_mutated",
            expected_failure_phase="locked_replan",
            expected_error_match="Current destruction impact differs",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_locked_drift_when_destroying_then_attempt_is_recorded_without_mutation(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    with pytest.raises(DestructionDriftError, match=test_case.expected_error_match):
        execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=connection,
            observation_connection=observation,
            project_dir=tmp_path,
            replan=lambda: replace(plan, plan_fingerprint="c" * 64),
        )

    assert store.get(plan_id=plan.plan_id, actor="actor-1") is plan
    assert connection.drop_names == []
    assert connection.tombstone_names == []
    assert len(invocations) == 1
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["pendingStatementSequences"] == list(test_case.expected_pending_sequences)
    assert test_case.expected_remaining_names is not None
    assert summary["remainingObjects"] == list(test_case.expected_remaining_names)
    assert summary["residualCatalogStatus"] == test_case.expected_residual_status
    assert summary["failurePhase"] == test_case.expected_failure_phase


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="interrupt after DROP reconciles catalog and terminal evidence",
            expected_pending_sequences=(2,),
            expected_remaining_names=("relation_two",),
            expected_residual_status="observed",
            expected_failure_phase="warehouse_execution",
            expected_error_match="injected destructive interruption",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_interrupt_after_drop_when_destroying_then_terminal_state_is_recorded(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    plan = replace(
        plan,
        relations=(
            replace(plan.relations[0], name="aaa_absent", exists=False),
            *plan.relations,
        ),
    )
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: InterruptingDestructionExecutionConnection = (
        InterruptingDestructionExecutionConnection(
            relation_names=tuple(relation.name for relation in plan.relations[1:])
        )
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    with pytest.raises(KeyboardInterrupt, match=test_case.expected_error_match):
        execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=connection,
            observation_connection=observation,
            project_dir=tmp_path,
            replan=lambda: plan,
        )

    assert connection.drop_names == ["relation_one"]
    assert connection.tombstone_names == []
    assert len(invocations) == 1
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["pendingStatementSequences"] == list(test_case.expected_pending_sequences)
    assert summary["remainingObjects"] == list(test_case.expected_remaining_names or ())
    assert summary["residualCatalogStatus"] == test_case.expected_residual_status
    assert summary["failurePhase"] == test_case.expected_failure_phase


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="drop completion event failure retains the exact DROP prefix",
            expected_outcome="failed",
            expected_completed_sequences=(1,),
            expected_pending_sequences=(2,),
            expected_remaining_names=("relation_two",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_drop_completion_failure_when_destroying_then_exact_prefix_and_residual_are_recorded(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    observation.failing_statements.add(
        "RECORD_RUN_EVENT statement_completed destroy_relation_0001;"
    )
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    result: DestructionExecutionResult = execute_destruction(
        frozen_plan=plan,
        actor=_ACTOR,
        challenge_responses=plan.challenges,
        reviewed_at=reviewed_at,
        store=store,
        connection=connection,
        observation_connection=observation,
        project_dir=tmp_path,
        replan=lambda: plan,
    )

    assert result.outcome == test_case.expected_outcome
    assert result.completed_statement_sequences == test_case.expected_completed_sequences
    assert result.pending_statement_sequences == test_case.expected_pending_sequences
    assert result.remaining_relation_names == test_case.expected_remaining_names
    assert result.residual_catalog_status == "observed"
    assert connection.drop_names == ["relation_one"]
    assert connection.tombstone_names == []
    with pytest.raises(DestructionPlanNotFoundError):
        store.get(plan_id=plan.plan_id, actor="actor-1")
    assert len(invocations) == 1
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["actor"] == {"id": "actor-1", "username": "Alice"}
    assert summary["planId"] == plan.plan_id
    assert summary["submittedChallenges"] == list(plan.challenges)
    assert summary["completedStatementSequences"] == list(test_case.expected_completed_sequences)
    assert summary["pendingStatementSequences"] == list(test_case.expected_pending_sequences)
    assert summary["remainingObjects"] == list(test_case.expected_remaining_names or ())


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="residual catalog failure records an unavailable null residual",
            expected_outcome="failed",
            expected_completed_sequences=(1, 2),
            expected_remaining_names=None,
            expected_residual_status="unavailable",
            expected_failure_phase="residual_catalog",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_residual_catalog_failure_after_drops_when_destroying_then_residual_is_unavailable(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    connection.fail_catalog = True
    observation: DestructionObservationConnection = DestructionObservationConnection()
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    result: DestructionExecutionResult = execute_destruction(
        frozen_plan=plan,
        actor=_ACTOR,
        challenge_responses=plan.challenges,
        reviewed_at=reviewed_at,
        store=store,
        connection=connection,
        observation_connection=observation,
        project_dir=tmp_path,
        replan=lambda: plan,
    )

    assert result.outcome == test_case.expected_outcome
    assert result.completed_statement_sequences == test_case.expected_completed_sequences
    assert result.pending_statement_sequences == ()
    assert result.remaining_relation_names is test_case.expected_remaining_names
    assert result.residual_catalog_status == test_case.expected_residual_status
    assert result.residual_catalog_error == "injected residual catalog failure"
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["remainingObjects"] is None
    assert summary["residualCatalogStatus"] == test_case.expected_residual_status
    assert summary["failurePhase"] == test_case.expected_failure_phase


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="run completion failure still persists the terminal invocation",
            expected_outcome="failed",
            expected_completed_sequences=(1, 2),
            expected_failure_phase="run_completed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_run_completed_failure_when_destroying_then_terminal_invocation_is_still_written(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    observation.failing_statements.add("RECORD_RUN_EVENT run_completed -;")
    invocations: list[AdapterInvocationRecord] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del client, database, node_results
        invocations.append(invocation)
        return None

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    result: DestructionExecutionResult = execute_destruction(
        frozen_plan=plan,
        actor=_ACTOR,
        challenge_responses=plan.challenges,
        reviewed_at=reviewed_at,
        store=store,
        connection=connection,
        observation_connection=observation,
        project_dir=tmp_path,
        replan=lambda: plan,
    )

    assert result.outcome == test_case.expected_outcome
    assert result.completed_statement_sequences == test_case.expected_completed_sequences
    assert result.pending_statement_sequences == ()
    assert len(invocations) == 1
    assert invocations[0].outcome == test_case.expected_outcome
    summary: dict[str, object] = json.loads(invocations[0].summary_json)
    assert summary["failurePhase"] == test_case.expected_failure_phase
    assert summary["runCompletedError"] is not None


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="primary terminal failure uses the execution connection",
            expected_outcome="succeeded",
            expected_terminal_attempt_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_primary_terminal_failure_when_destroying_then_fallback_records_invocation(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    attempts: list[AdapterConnection] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del database, invocation, node_results
        attempts.append(client)
        warning_by_client: dict[AdapterConnection, str | None] = {
            observation: "primary terminal failure",
            connection: None,
        }
        return warning_by_client[client]

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    result: DestructionExecutionResult = execute_destruction(
        frozen_plan=plan,
        actor=_ACTOR,
        challenge_responses=plan.challenges,
        reviewed_at=reviewed_at,
        store=store,
        connection=connection,
        observation_connection=observation,
        project_dir=tmp_path,
        replan=lambda: plan,
    )

    assert result.outcome == test_case.expected_outcome
    assert len(attempts) == test_case.expected_terminal_attempt_count
    assert attempts == [observation, connection]


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionExecutionTestCase(
            description="both terminal connections failing raises a recording error",
            expected_error_match="fallback failed",
            expected_terminal_attempt_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_all_terminal_connections_fail_when_destroying_then_recording_error_is_raised(
    test_case: DestructionExecutionTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan: DestructionPlan = build_execution_plan(now=_NOW)
    statements: tuple[WarehouseStatement, ...] = build_execution_statements()
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=lambda: _NOW)
    store.save(plan=plan, actor="actor-1")
    reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="actor-1")
    connection: DestructionExecutionConnection = DestructionExecutionConnection(
        relation_names=tuple(relation.name for relation in plan.relations)
    )
    observation: DestructionObservationConnection = DestructionObservationConnection()
    attempts: list[AdapterConnection] = []

    def persist_terminal(
        *,
        client: AdapterConnection,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> str | None:
        del database, invocation, node_results
        attempts.append(client)
        return "injected terminal failure"

    monkeypatch.setattr(
        _INITIALIZE_OBSERVABILITY,
        lambda **_: None,
    )
    monkeypatch.setattr(_ASSEMBLE_DESTRUCTION_WORKFLOW, lambda **_: statements)
    monkeypatch.setattr(
        _PERSIST_TERMINAL_OBSERVATIONS,
        persist_terminal,
    )

    with pytest.raises(DestructionRecordingError, match=test_case.expected_error_match):
        execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=connection,
            observation_connection=observation,
            project_dir=tmp_path,
            replan=lambda: plan,
        )

    assert len(attempts) == test_case.expected_terminal_attempt_count
    assert attempts == [observation, connection]
    assert connection.drop_names == ["relation_one", "relation_two"]
    assert connection.tombstone_names == []


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
