"""Behavior tests for one sequential sensor dispatch pass."""

from pathlib import Path

import pytest

from sqltide._native import SensorDispatcher
from sqltide.events.models import NodeResultObservation
from sqltide.sensors.models import (
    PollingTickState,
    SensorDispatchSummary,
    SensorRegistry,
    SensorStreamPosition,
    TickAttemptState,
)
from sqltide.sensors.types import SensorOverrideStatus, SensorTickStatus
from tests.unit.src.sqltide.events.helpers import (
    build_invocation_observation,
    build_node_result_observation,
)
from tests.unit.src.sqltide.sensors._helpers.helpers import load_registry, write_project_files
from tests.unit.src.sqltide.sensors.classes._test_types import (
    CompiledSensorDispatchTestCase,
    DeadLetterActionTestCase,
    DispatcherScenarioTestCase,
    EventCheckpointInitializationTestCase,
    LeaseLossTestCase,
    MultiBatchDrainTestCase,
    StreamHeadBoundTestCase,
)
from tests.unit.src.sqltide.sensors.classes.helpers import (
    CATALOG_EVENT_HANDLERS_SOURCE,
    EPOCH_POSITION,
    ROW_POSITION,
    backoff_sensor,
    failing_sensor,
    fresh_events_sensor,
    polling_lag,
    prod_only_sensor,
    skipping_sensor,
    stopped_sensor,
    succeeding_sensor,
)
from tests.unit.src.sqltide.sensors.helpers import (
    FAKE_WAREHOUSE_NOW,
    FakeSensorStateRepository,
    build_loaded_sensor,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DispatcherScenarioTestCase(
            description="fresh failing audit event evaluates and advances the checkpoint",
            sensor=build_loaded_sensor(declaration=succeeding_sensor),
            checkpoints={("succeeding_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            expected_tick_statuses=("started", "succeeded"),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(evaluated=1, succeeded=1),
        ),
        DispatcherScenarioTestCase(
            description="handler failure records a failed tick and blocks the checkpoint",
            sensor=build_loaded_sensor(declaration=failing_sensor),
            checkpoints={("failing_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            expected_tick_statuses=("started", "failed"),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(evaluated=1, failed=1),
        ),
        DispatcherScenarioTestCase(
            description="exhausted attempts dead-letter the event and unblock the stream",
            sensor=build_loaded_sensor(declaration=failing_sensor),
            checkpoints={("failing_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("failing_sensor", "result-1"): TickAttemptState(
                    failed_attempts=2,
                    last_failed_at="2024-01-01 00:00:02.000",
                    last_error_message="RuntimeError: boom",
                    resolved=False,
                )
            },
            expected_tick_statuses=("dead_lettered",),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(dead_lettered=1),
        ),
        DispatcherScenarioTestCase(
            description="pending backoff blocks the stream without re-evaluating",
            sensor=build_loaded_sensor(declaration=backoff_sensor),
            checkpoints={("backoff_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("backoff_sensor", "result-1"): TickAttemptState(
                    failed_attempts=1,
                    last_failed_at=FAKE_WAREHOUSE_NOW,
                    last_error_message="RuntimeError: boom",
                    resolved=False,
                )
            },
            expected_tick_statuses=(),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(),
        ),
        DispatcherScenarioTestCase(
            description="resolved events advance without new ticks",
            sensor=build_loaded_sensor(declaration=succeeding_sensor),
            checkpoints={("succeeding_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("succeeding_sensor", "result-1"): TickAttemptState(
                    failed_attempts=0,
                    last_failed_at=None,
                    last_error_message=None,
                    resolved=True,
                )
            },
            expected_tick_statuses=(),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(),
        ),
        DispatcherScenarioTestCase(
            description="first enable initializes the checkpoint at the newest observation",
            sensor=build_loaded_sensor(declaration=succeeding_sensor),
            node_results=(build_node_result_observation(),),
            newest_position=ROW_POSITION,
            expected_tick_statuses=(),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(),
        ),
        DispatcherScenarioTestCase(
            description="sensors stopped by declared default are not dispatched",
            sensor=build_loaded_sensor(declaration=stopped_sensor),
            checkpoints={("stopped_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            expected_tick_statuses=(),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(),
        ),
        DispatcherScenarioTestCase(
            description="a running override enables a sensor stopped in code",
            sensor=build_loaded_sensor(declaration=stopped_sensor),
            checkpoints={("stopped_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            overrides={"stopped_sensor": SensorOverrideStatus.RUNNING},
            expected_tick_statuses=("started", "succeeded"),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(evaluated=1, succeeded=1),
        ),
        DispatcherScenarioTestCase(
            description="skip reasons resolve events with a recorded skipped tick",
            sensor=build_loaded_sensor(declaration=skipping_sensor),
            checkpoints={("skipping_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            expected_tick_statuses=("started", "skipped"),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(evaluated=1, skipped=1),
        ),
        DispatcherScenarioTestCase(
            description="named event target is independent from physical target identity",
            sensor=build_loaded_sensor(declaration=prod_only_sensor),
            checkpoints={("prod_only_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(target_identity="default"),),
            expected_tick_statuses=("started", "succeeded"),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(evaluated=1, succeeded=1),
        ),
        DispatcherScenarioTestCase(
            description="nonmatching named target advances without evaluation",
            sensor=build_loaded_sensor(declaration=prod_only_sensor),
            checkpoints={("prod_only_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(target_identity="default"),),
            expected_tick_statuses=(),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(),
            event_target="uat",
        ),
        DispatcherScenarioTestCase(
            description="expired retry advances without stale handler evaluation",
            sensor=build_loaded_sensor(declaration=fresh_events_sensor),
            checkpoints={("fresh_events_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("fresh_events_sensor", "result-1"): TickAttemptState(
                    failed_attempts=1,
                    last_failed_at="2024-01-01 00:00:02.000",
                    last_error_message="RuntimeError: slack unavailable",
                    resolved=False,
                )
            },
            expected_tick_statuses=(),
            expected_advanced_count=1,
            expected_summary=SensorDispatchSummary(),
        ),
        DispatcherScenarioTestCase(
            description="a lease held elsewhere skips the entire pass",
            sensor=build_loaded_sensor(declaration=succeeding_sensor),
            checkpoints={("succeeding_sensor", "node_results"): EPOCH_POSITION},
            node_results=(build_node_result_observation(),),
            lease_acquired=False,
            expected_tick_statuses=(),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(lease_acquired=False),
        ),
        DispatcherScenarioTestCase(
            description="due polling sensors evaluate and persist their cursor",
            sensor=build_loaded_sensor(declaration=polling_lag),
            expected_tick_statuses=("started", "succeeded"),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(evaluated=1, succeeded=1),
        ),
        DispatcherScenarioTestCase(
            description="polling sensors within their interval are not evaluated",
            sensor=build_loaded_sensor(declaration=polling_lag),
            polling_states={
                "polling_lag": PollingTickState(
                    last_started_at=FAKE_WAREHOUSE_NOW,
                    last_success_at=FAKE_WAREHOUSE_NOW,
                    cursor="41",
                )
            },
            expected_tick_statuses=(),
            expected_advanced_count=0,
            expected_summary=SensorDispatchSummary(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_scenario_when_dispatching_once_then_ticks_and_checkpoints_match(
    test_case: DispatcherScenarioTestCase,
) -> None:
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints=test_case.checkpoints,
        node_results=test_case.node_results,
        invocations=test_case.invocations,
        previous_statuses=test_case.previous_statuses,
        attempt_states=test_case.attempt_states,
        overrides=test_case.overrides,
        polling_states=test_case.polling_states,
        newest_position=test_case.newest_position,
        lease_acquired=test_case.lease_acquired,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(
        repository=repository, event_target=test_case.event_target
    )
    registry: SensorRegistry = SensorRegistry(sensors={test_case.sensor.name: test_case.sensor})

    summary: SensorDispatchSummary = dispatcher.dispatch_once(registry=registry, target="prod")

    assert summary == test_case.expected_summary
    assert tuple(tick.status for tick in repository.recorded_ticks) == (
        test_case.expected_tick_statuses
    )
    assert len(repository.advanced) == test_case.expected_advanced_count


@pytest.mark.parametrize(
    "test_case",
    [
        MultiBatchDrainTestCase(
            description="five results drain through two-row pages",
            result_count=5,
            batch_limit=2,
            expected_result_id="result-5",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_more_results_than_one_batch_when_dispatching_then_drains_to_one_checkpoint(
    test_case: MultiBatchDrainTestCase,
) -> None:
    rows: tuple[NodeResultObservation, ...] = tuple(
        build_node_result_observation(
            result_id=f"result-{index}",
            completed_at=f"2024-01-01 00:00:0{index}.000",
        )
        for index in range(1, test_case.result_count + 1)
    )
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints={("succeeding_sensor", "node_results"): EPOCH_POSITION},
        node_results=rows,
        newest_position=SensorStreamPosition(
            completed_at=rows[-1].completed_at,
            result_id=rows[-1].result_id,
        ),
    )
    dispatcher: SensorDispatcher = SensorDispatcher(
        repository=repository,
        event_target="prod",
        batch_limit=test_case.batch_limit,
    )
    registry: SensorRegistry = SensorRegistry(
        sensors={"succeeding_sensor": build_loaded_sensor(declaration=succeeding_sensor)}
    )

    summary: SensorDispatchSummary = dispatcher.dispatch_once(registry=registry, target="prod")

    assert summary == SensorDispatchSummary(evaluated=5, succeeded=5)
    assert tuple(position.result_id for _, _, position in repository.advanced) == (
        test_case.expected_result_id,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        StreamHeadBoundTestCase(
            description="a result after the captured head waits for the next pass",
            batch_limit=1,
            stream_head=ROW_POSITION,
            expected_result_id="result-1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_new_results_arrive_during_dispatch_when_draining_then_stops_at_initial_head(
    test_case: StreamHeadBoundTestCase,
) -> None:
    rows: tuple[NodeResultObservation, ...] = (
        build_node_result_observation(result_id="result-1", completed_at="2024-01-01 00:00:01.000"),
        build_node_result_observation(result_id="result-2", completed_at="2024-01-01 00:00:02.000"),
    )
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints={("succeeding_sensor", "node_results"): EPOCH_POSITION},
        node_results=rows,
        newest_position=test_case.stream_head,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(
        repository=repository,
        event_target="prod",
        batch_limit=test_case.batch_limit,
    )
    registry: SensorRegistry = SensorRegistry(
        sensors={"succeeding_sensor": build_loaded_sensor(declaration=succeeding_sensor)}
    )

    summary: SensorDispatchSummary = dispatcher.dispatch_once(registry=registry, target="prod")

    assert summary == SensorDispatchSummary(evaluated=1, succeeded=1)
    assert tuple(position.result_id for _, _, position in repository.advanced) == (
        test_case.expected_result_id,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        LeaseLossTestCase(
            description="renewal loss after two results aborts remaining sensors",
            result_count=3,
            batch_limit=2,
            lease_results=(True, True, True, False),
            expected_result_id="result-2",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_lease_is_lost_when_draining_then_checkpoints_and_stops_other_sensors(
    test_case: LeaseLossTestCase,
) -> None:
    rows: tuple[NodeResultObservation, ...] = tuple(
        build_node_result_observation(
            result_id=f"result-{index}",
            completed_at=f"2024-01-01 00:00:0{index}.000",
        )
        for index in range(1, test_case.result_count + 1)
    )
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints={
            ("first_sensor", "node_results"): EPOCH_POSITION,
            ("second_sensor", "node_results"): EPOCH_POSITION,
        },
        node_results=rows,
        newest_position=SensorStreamPosition(
            completed_at=rows[-1].completed_at,
            result_id=rows[-1].result_id,
        ),
        lease_results=test_case.lease_results,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(
        repository=repository,
        event_target="prod",
        batch_limit=test_case.batch_limit,
    )
    registry: SensorRegistry = SensorRegistry(
        sensors={
            "first_sensor": build_loaded_sensor(declaration=succeeding_sensor, name="first_sensor"),
            "second_sensor": build_loaded_sensor(
                declaration=succeeding_sensor, name="second_sensor"
            ),
        }
    )

    summary: SensorDispatchSummary = dispatcher.dispatch_once(registry=registry, target="prod")

    assert summary == SensorDispatchSummary(
        lease_acquired=False,
        evaluated=2,
        succeeded=2,
    )
    assert tuple(sensor_name for sensor_name, _, _ in repository.advanced) == ("first_sensor",)
    assert repository.advanced[0][2].result_id == test_case.expected_result_id


@pytest.mark.parametrize(
    "test_case",
    [
        EventCheckpointInitializationTestCase(
            description="only new running event sensors receive startup checkpoints",
            sensors=(
                build_loaded_sensor(declaration=succeeding_sensor),
                build_loaded_sensor(declaration=succeeding_sensor, name="existing_event_sensor"),
                build_loaded_sensor(declaration=polling_lag),
            ),
            checkpoints={("existing_event_sensor", "node_results"): EPOCH_POSITION},
            newest_position=ROW_POSITION,
            expected_advanced=(("succeeding_sensor", "node_results", ROW_POSITION),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_sensors_when_initializing_then_only_new_event_checkpoints_advance(
    test_case: EventCheckpointInitializationTestCase,
) -> None:
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints=test_case.checkpoints,
        newest_position=test_case.newest_position,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="prod")
    registry: SensorRegistry = SensorRegistry(
        sensors={sensor.name: sensor for sensor in test_case.sensors}
    )

    dispatcher.initialize_event_checkpoints(registry=registry, target="prod")

    assert tuple(repository.advanced) == test_case.expected_advanced
    assert repository.recorded_ticks == []


@pytest.mark.parametrize(
    "test_case",
    [
        DeadLetterActionTestCase(
            description="retrying a dead letter re-evaluates and records the outcome",
            sensor=build_loaded_sensor(declaration=succeeding_sensor),
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("succeeding_sensor", "result-1"): TickAttemptState(
                    failed_attempts=2,
                    last_failed_at="2024-01-01 00:00:02.000",
                    last_error_message="RuntimeError: boom",
                    resolved=False,
                )
            },
            event_id="result-1",
            expected_status=SensorTickStatus.SUCCEEDED,
            expected_tick_statuses=("started", "succeeded"),
            expected_tick_attempts=(3, 3),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dead_letter_when_retrying_then_event_is_rebuilt_and_re_evaluated(
    test_case: DeadLetterActionTestCase,
) -> None:
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        node_results=test_case.node_results,
        attempt_states=test_case.attempt_states,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="prod")
    registry: SensorRegistry = SensorRegistry(sensors={test_case.sensor.name: test_case.sensor})

    status: SensorTickStatus = dispatcher.retry_dead_letter(
        registry=registry,
        sensor_name=test_case.sensor.name,
        event_id=test_case.event_id,
    )

    assert status is test_case.expected_status
    assert tuple(tick.status for tick in repository.recorded_ticks) == (
        test_case.expected_tick_statuses
    )
    assert tuple(tick.attempt for tick in repository.recorded_ticks) == (
        test_case.expected_tick_attempts
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DeadLetterActionTestCase(
            description="skipping a dead letter records the reason and resolves it",
            sensor=build_loaded_sensor(declaration=failing_sensor),
            node_results=(build_node_result_observation(),),
            attempt_states={
                ("failing_sensor", "result-1"): TickAttemptState(
                    failed_attempts=2,
                    last_failed_at="2024-01-01 00:00:02.000",
                    last_error_message="RuntimeError: boom",
                    resolved=False,
                )
            },
            event_id="result-1",
            expected_status=SensorTickStatus.SKIPPED,
            expected_tick_statuses=("skipped",),
            expected_tick_attempts=(2,),
            expected_skip_reason="acknowledged manually",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dead_letter_when_skipping_then_a_skipped_tick_resolves_it(
    test_case: DeadLetterActionTestCase,
) -> None:
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        node_results=test_case.node_results,
        attempt_states=test_case.attempt_states,
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="prod")
    registry: SensorRegistry = SensorRegistry(sensors={test_case.sensor.name: test_case.sensor})

    dispatcher.skip_dead_letter(
        registry=registry,
        sensor_name=test_case.sensor.name,
        event_id=test_case.event_id,
        reason="acknowledged manually",
    )

    assert tuple(tick.status for tick in repository.recorded_ticks) == (
        test_case.expected_tick_statuses
    )
    assert tuple(tick.attempt for tick in repository.recorded_ticks) == (
        test_case.expected_tick_attempts
    )
    assert repository.recorded_ticks[0].skip_reason == test_case.expected_skip_reason


@pytest.mark.parametrize(
    "test_case",
    [
        CompiledSensorDispatchTestCase(
            description="sensors on events imported from sqltide.events receive those events",
            sensor_files=(("sensors/catalog.py", CATALOG_EVENT_HANDLERS_SOURCE),),
            checkpoints={
                ("audit_watch", "node_results"): EPOCH_POSITION,
                ("run_watch", "invocations"): EPOCH_POSITION,
            },
            expected_ticks=(
                ("audit_watch", "started"),
                ("audit_watch", "succeeded"),
                ("run_watch", "started"),
                ("run_watch", "succeeded"),
            ),
            expected_summary=SensorDispatchSummary(evaluated=2, succeeded=2),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_catalog_event_sensors_when_dispatching_then_each_receives_its_event(
    test_case: CompiledSensorDispatchTestCase, tmp_path: Path
) -> None:
    write_project_files(project_dir=tmp_path, files=test_case.sensor_files)
    registry: SensorRegistry = load_registry(project_dir=tmp_path)
    repository: FakeSensorStateRepository = FakeSensorStateRepository(
        checkpoints=test_case.checkpoints,
        node_results=(build_node_result_observation(),),
        invocations=(build_invocation_observation(),),
        newest_position=SensorStreamPosition(completed_at="2024-01-01 00:00:09.000", result_id="z"),
    )
    dispatcher: SensorDispatcher = SensorDispatcher(repository=repository, event_target="prod")

    summary: SensorDispatchSummary = dispatcher.dispatch_once(registry=registry, target="prod")

    assert summary == test_case.expected_summary
    assert (
        tuple((tick.sensor_name, tick.status) for tick in repository.recorded_ticks)
        == test_case.expected_ticks
    )
