"""Integration tests for durable sensor state and dispatch against ClickHouse."""

import pytest

from sqltide._native import SensorDispatcher, SensorStateRepository
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterSensorStepRecord
from sqltide.events import AuditCompleted
from sqltide.sensors import DefaultSensorStatus, SensorRetryPolicy, event_sensor
from sqltide.sensors.models import (
    EventSensorDeclaration,
    LoadedSensor,
    SensorDispatchSummary,
    SensorRegistry,
    SensorStreamPosition,
    SensorTickView,
    StepMarker,
)
from sqltide.sensors.types import SensorOverrideStatus, SensorTickStatus
from tests.integration.src.sqltide.sensors._test_types import (
    DeadLetterFlowTestCase,
    DispatchDeliveryTestCase,
    RedeliveryTestCase,
    RepositoryRoundtripTestCase,
)
from tests.integration.src.sqltide.sensors.helpers import (
    AlwaysFailingHandler,
    RecordingHandler,
    StepCrashHandler,
    build_loaded_sensor,
    build_registry,
    seed_node_result,
)

_SAMPLE_PAYLOAD_JSON: str = (
    '{"sample_column_names":["race_source_key","expected_runners","actual_runners"],'
    '"sample_rows":[["1672326",13,12],["1672261",12,11]]}'
)
_SAMPLE_COLUMN_NAMES: tuple[str, ...] = (
    "race_source_key",
    "expected_runners",
    "actual_runners",
)
_SAMPLE_ROWS: tuple[tuple[object, ...], ...] = (("1672326", 13, 12), ("1672261", 12, 11))


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RepositoryRoundtripTestCase(
            description="checkpoints, steps, overrides, and leases persist latest-wins",
            expected_checkpoint=("2024-01-01 00:00:02.000", "result-2"),
            expected_step_status="succeeded",
            expected_step_result_json='"ticket-1"',
            expected_override_status="stopped",
            expected_first_lease=True,
            expected_competing_lease=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sensor_state_when_persisting_then_reads_reduce_latest_wins(
    test_case: RepositoryRoundtripTestCase,
    sensor_warehouse_client: AdapterConnection,
    clickhouse_database: str,
) -> None:
    repository: SensorStateRepository = SensorStateRepository(
        connection=sensor_warehouse_client, database=clickhouse_database
    )
    repository.ensure_ready()

    repository.advance_checkpoint(
        sensor_name="quality_alerts",
        source="node_results",
        position=SensorStreamPosition(completed_at="2024-01-01 00:00:01.000", result_id="result-1"),
    )
    repository.advance_checkpoint(
        sensor_name="quality_alerts",
        source="node_results",
        position=SensorStreamPosition(completed_at="2024-01-01 00:00:02.000", result_id="result-2"),
    )
    repository.record_step(
        step=AdapterSensorStepRecord(
            sensor_name="quality_alerts",
            event_id="result-1",
            step_key="jira",
            policy="at_least_once",
            status="succeeded",
            attempt=1,
            result_json='"ticket-1"',
            error_message=None,
        )
    )
    repository.record_override(
        sensor_name="quality_alerts", status=SensorOverrideStatus.RUNNING, actor="kevin"
    )
    repository.record_override(
        sensor_name="quality_alerts", status=SensorOverrideStatus.STOPPED, actor="kevin"
    )
    first_lease: bool = repository.acquire_dispatch_lease(owner_id="dispatcher-a", ttl_seconds=60)
    competing_lease: bool = repository.acquire_dispatch_lease(
        owner_id="dispatcher-b", ttl_seconds=60
    )

    checkpoint: SensorStreamPosition | None = repository.read_checkpoint(
        sensor_name="quality_alerts", source="node_results"
    )
    assert checkpoint is not None
    assert (checkpoint.completed_at, checkpoint.result_id) == test_case.expected_checkpoint
    marker: StepMarker | None = repository.read_step(
        sensor_name="quality_alerts", event_id="result-1", step_key="jira"
    )
    assert marker is not None
    assert marker.status == test_case.expected_step_status
    assert marker.result_json == test_case.expected_step_result_json
    assert repository.override_statuses()["quality_alerts"] == (test_case.expected_override_status)
    assert first_lease is test_case.expected_first_lease
    assert competing_lease is test_case.expected_competing_lease


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DispatchDeliveryTestCase(
            description="a persisted failure reaches the handler with its transition",
            expected_transition="new_failure",
            expected_event_id="result-2",
            expected_tick_statuses=("started", "succeeded"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_new_observation_when_dispatching_then_event_reaches_the_handler(
    test_case: DispatchDeliveryTestCase,
    sensor_warehouse_client: AdapterConnection,
    clickhouse_database: str,
) -> None:
    handler: RecordingHandler = RecordingHandler()
    declaration: EventSensorDeclaration = event_sensor(
        on=AuditCompleted, name="recorder", default_status=DefaultSensorStatus.RUNNING
    )(handler)
    sensor: LoadedSensor = build_loaded_sensor(declaration=declaration)
    registry: SensorRegistry = build_registry(sensors=(sensor,))
    repository: SensorStateRepository = SensorStateRepository(
        connection=sensor_warehouse_client, database=clickhouse_database
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="uat")
    seed_node_result(
        connection=sensor_warehouse_client,
        database=clickhouse_database,
        result_id="result-1",
        status="passed",
        completed_at="2024-01-01 00:00:01.000",
        target_identity=clickhouse_database,
    )
    _ = dispatcher.dispatch_once(registry=registry, target=clickhouse_database)
    seed_node_result(
        connection=sensor_warehouse_client,
        database=clickhouse_database,
        result_id="result-2",
        status="failed",
        completed_at="2024-01-01 00:00:02.000",
        target_identity=clickhouse_database,
        payload_json=_SAMPLE_PAYLOAD_JSON,
    )

    summary: SensorDispatchSummary = dispatcher.dispatch_once(
        registry=registry, target=clickhouse_database
    )

    assert summary.succeeded == 1
    assert tuple(str(event.id) for event in handler.events) == (test_case.expected_event_id,)
    assert tuple(str(event.transition) for event in handler.events) == (
        test_case.expected_transition,
    )
    assert tuple(str(event.target) for event in handler.events) == ("uat",)
    assert handler.events[0].sample_column_names == _SAMPLE_COLUMN_NAMES
    assert handler.events[0].sample_rows == _SAMPLE_ROWS
    ticks: tuple[SensorTickView, ...] = repository.list_ticks(sensor_name="recorder", limit=10)
    assert tuple(tick.status for tick in ticks) == test_case.expected_tick_statuses[1:]
    assert tuple(tick.event_id for tick in ticks) == (test_case.expected_event_id,)
    windowed: tuple[SensorTickView, ...] = repository.list_ticks(
        sensor_name="recorder",
        limit=10,
        after=ticks[0].started_at,
        before=ticks[0].started_at,
    )
    assert tuple(tick.tick_id for tick in windowed) == (ticks[0].tick_id,)
    outside: tuple[SensorTickView, ...] = repository.list_ticks(
        sensor_name="recorder", limit=10, before="2000-01-01 00:00:00.000"
    )
    assert outside == ()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        RedeliveryTestCase(
            description="unadvanced checkpoints redeliver the same event id with memoized steps",
            expected_event_ids=("result-1", "result-1"),
            expected_final_statuses=("failed", "succeeded"),
            expected_step_invocations=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_failed_attempt_when_dispatching_again_then_same_event_resumes_at_steps(
    test_case: RedeliveryTestCase,
    sensor_warehouse_client: AdapterConnection,
    clickhouse_database: str,
) -> None:
    handler: StepCrashHandler = StepCrashHandler()
    declaration: EventSensorDeclaration = event_sensor(
        on=AuditCompleted,
        name="step_resumer",
        default_status=DefaultSensorStatus.RUNNING,
        retry_policy=SensorRetryPolicy(max_attempts=3, backoff_seconds=0),
    )(handler)
    sensor: LoadedSensor = build_loaded_sensor(declaration=declaration)
    registry: SensorRegistry = build_registry(sensors=(sensor,))
    repository: SensorStateRepository = SensorStateRepository(
        connection=sensor_warehouse_client, database=clickhouse_database
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="uat")
    _ = dispatcher.dispatch_once(registry=registry, target=clickhouse_database)
    seed_node_result(
        connection=sensor_warehouse_client,
        database=clickhouse_database,
        result_id="result-1",
        status="failed",
        completed_at="2024-01-01 00:00:01.000",
        target_identity=clickhouse_database,
    )

    first: SensorDispatchSummary = dispatcher.dispatch_once(
        registry=registry, target=clickhouse_database
    )
    second: SensorDispatchSummary = dispatcher.dispatch_once(
        registry=registry, target=clickhouse_database
    )

    assert (first.failed, second.succeeded) == (1, 1)
    assert tuple(handler.step_values) == ("ticket-1", "ticket-1")
    assert handler.step_action.calls == test_case.expected_step_invocations
    ticks: tuple[SensorTickView, ...] = tuple(
        reversed(repository.list_ticks(sensor_name="step_resumer", limit=10))
    )
    assert tuple(tick.event_id for tick in ticks) == test_case.expected_event_ids
    assert tuple(tick.status for tick in ticks) == test_case.expected_final_statuses
    assert tuple(tick.attempt for tick in ticks) == (1, 2)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DeadLetterFlowTestCase(
            description="exhausted retries dead-letter the event and unblock the stream",
            expected_dead_letters_after_exhaustion=1,
            expected_dead_letters_after_skip=0,
            expected_handler_calls=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_poisoned_event_when_attempts_exhaust_then_dead_letter_unblocks_stream(
    test_case: DeadLetterFlowTestCase,
    sensor_warehouse_client: AdapterConnection,
    clickhouse_database: str,
) -> None:
    handler: AlwaysFailingHandler = AlwaysFailingHandler()
    declaration: EventSensorDeclaration = event_sensor(
        on=AuditCompleted,
        name="poisoned",
        default_status=DefaultSensorStatus.RUNNING,
        retry_policy=SensorRetryPolicy(max_attempts=1, backoff_seconds=0),
    )(handler)
    sensor: LoadedSensor = build_loaded_sensor(declaration=declaration)
    registry: SensorRegistry = build_registry(sensors=(sensor,))
    repository: SensorStateRepository = SensorStateRepository(
        connection=sensor_warehouse_client, database=clickhouse_database
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="uat")
    _ = dispatcher.dispatch_once(registry=registry, target=clickhouse_database)
    seed_node_result(
        connection=sensor_warehouse_client,
        database=clickhouse_database,
        result_id="result-1",
        status="failed",
        completed_at="2024-01-01 00:00:01.000",
        target_identity=clickhouse_database,
        payload_json=_SAMPLE_PAYLOAD_JSON,
    )

    first: SensorDispatchSummary = dispatcher.dispatch_once(
        registry=registry, target=clickhouse_database
    )
    second: SensorDispatchSummary = dispatcher.dispatch_once(
        registry=registry, target=clickhouse_database
    )

    assert (first.failed, second.dead_lettered) == (1, 1)
    assert handler.calls == test_case.expected_handler_calls
    dead_letters: tuple[SensorTickView, ...] = repository.list_dead_letters()
    assert len(dead_letters) == test_case.expected_dead_letters_after_exhaustion
    assert dead_letters[0].event_id == "result-1"
    checkpoint: SensorStreamPosition | None = repository.read_checkpoint(
        sensor_name="poisoned", source="node_results"
    )
    assert checkpoint is not None
    assert checkpoint.result_id == "result-1"

    retry_status: SensorTickStatus = dispatcher.retry_dead_letter(
        registry=registry, sensor_name="poisoned", event_id="result-1"
    )

    assert str(retry_status) == "failed"
    assert handler.calls == test_case.expected_handler_calls + 1
    assert handler.events[-1].sample_column_names == _SAMPLE_COLUMN_NAMES
    assert handler.events[-1].sample_rows == _SAMPLE_ROWS

    dispatcher.skip_dead_letter(
        registry=registry,
        sensor_name="poisoned",
        event_id="result-1",
        reason="acknowledged in incident review",
    )

    assert len(repository.list_dead_letters()) == test_case.expected_dead_letters_after_skip
    assert repository.tick_attempt_state(sensor_name="poisoned", event_id="result-1").resolved
    assert dispatcher.dispatch_once(registry=registry, target=clickhouse_database).evaluated == 0


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
