"""Sequential sensor dispatch: derive events, evaluate handlers, persist ticks."""

from __future__ import annotations

from uuid import uuid4

from sqltide._native import events_from_invocation, events_from_node_result
from sqltide.adapter.models import AdapterSensorTickRecord
from sqltide.events.models import InvocationObservation, NodeResultObservation
from sqltide.events.types import SensorEvent
from sqltide.executor.auditing.types import QualityResultStatus
from sqltide.provider.models import DiscoveredProvider
from sqltide.sensors._helpers.dispatch import (
    effective_sensor_status,
    event_declaration_of,
    event_source_for,
    matches_sensor,
    summarize_outcomes,
)
from sqltide.sensors._helpers.evaluation import evaluate_sensor_handler
from sqltide.sensors._helpers.queries import shift_timestamp_text, timestamp_is_before
from sqltide.sensors.classes.durable_step_runner import DurableStepRunner
from sqltide.sensors.classes.event_sensor_context import EventSensorContext
from sqltide.sensors.classes.polling_sensor_context import PollingSensorContext
from sqltide.sensors.classes.repository_step_store import RepositoryStepStore
from sqltide.sensors.classes.sensor_state_repository import SensorStateRepository
from sqltide.sensors.constants import (
    DEFAULT_DISPATCH_LEASE_TTL_SECONDS,
    DEFAULT_EVENT_BATCH_LIMIT,
    NODE_RESULTS_EVENT_SOURCE,
)
from sqltide.sensors.exceptions import SensorError
from sqltide.sensors.models import (
    EventSensorDeclaration,
    LoadedSensor,
    PollingSensorDeclaration,
    PollingTickState,
    SensorDeliveryOutcome,
    SensorDispatchSummary,
    SensorEvaluation,
    SensorEventDelivery,
    SensorRegistry,
    SensorStreamPosition,
    TickAttemptState,
)
from sqltide.sensors.types import (
    SensorKind,
    SensorOverrideStatus,
    SensorTickStatus,
)

_EPOCH_POSITION: SensorStreamPosition = SensorStreamPosition(
    completed_at="1970-01-01 00:00:00.000", result_id=""
)
_LEASE_TIMEOUT_BUFFER_SECONDS: float = 10.0


def _position_key(position: SensorStreamPosition) -> tuple[str, str]:
    return position.completed_at, position.result_id


class SensorDispatcher:
    """One sequential dispatch pass over all running sensors."""

    def __init__(
        self,
        *,
        repository: SensorStateRepository,
        event_target: str,
        providers: tuple[DiscoveredProvider, ...] = (),
        dispatcher_id: str | None = None,
        batch_limit: int = DEFAULT_EVENT_BATCH_LIMIT,
        lease_ttl_seconds: float = DEFAULT_DISPATCH_LEASE_TTL_SECONDS,
        maximum_event_age_seconds: float | None = None,
    ) -> None:
        self._repository: SensorStateRepository = repository
        self._event_target: str = event_target
        self._providers: tuple[DiscoveredProvider, ...] = providers
        self._dispatcher_id: str = dispatcher_id if dispatcher_id is not None else uuid4().hex
        self._batch_limit: int = batch_limit
        self._lease_ttl_seconds: float = lease_ttl_seconds
        self._maximum_event_age_seconds: float | None = maximum_event_age_seconds

    def dispatch_once(self, *, registry: SensorRegistry, target: str) -> SensorDispatchSummary:
        """Evaluate every running sensor once against new observations."""

        self._repository.ensure_ready()
        if not self._repository.acquire_dispatch_lease(
            owner_id=self._dispatcher_id, ttl_seconds=self._lease_ttl_seconds
        ):
            return summarize_outcomes(outcomes=(), lease_acquired=False)
        overrides: dict[str, SensorOverrideStatus] = self._repository.override_statuses()
        outcomes: list[SensorDeliveryOutcome] = []
        for sensor in registry.ordered():
            status: SensorOverrideStatus = effective_sensor_status(
                sensor=sensor, overrides=overrides
            )
            if status is not SensorOverrideStatus.RUNNING:
                continue
            if sensor.kind is SensorKind.EVENT:
                event_outcomes, lease_held = self._dispatch_event_sensor(
                    sensor=sensor, target=target
                )
                outcomes.extend(event_outcomes)
                if not lease_held:
                    return summarize_outcomes(outcomes=tuple(outcomes), lease_acquired=False)
                continue
            if not self._renew_dispatch_lease(sensor=sensor):
                return summarize_outcomes(outcomes=tuple(outcomes), lease_acquired=False)
            polling_outcome: SensorDeliveryOutcome | None = self._dispatch_polling_sensor(
                sensor=sensor
            )
            if polling_outcome is not None:
                outcomes.append(polling_outcome)
        return summarize_outcomes(outcomes=tuple(outcomes), lease_acquired=True)

    def initialize_event_checkpoints(self, *, registry: SensorRegistry, target: str) -> None:
        """Checkpoint newly enabled event sensors without evaluating their handlers."""

        self._repository.ensure_ready()
        overrides: dict[str, SensorOverrideStatus] = self._repository.override_statuses()
        for sensor in registry.ordered():
            if sensor.kind is not SensorKind.EVENT:
                continue
            status: SensorOverrideStatus = effective_sensor_status(
                sensor=sensor, overrides=overrides
            )
            if status is not SensorOverrideStatus.RUNNING:
                continue
            declaration: EventSensorDeclaration = event_declaration_of(sensor)
            source: str = event_source_for(declaration.event_type)
            checkpoint: SensorStreamPosition | None = self._repository.read_checkpoint(
                sensor_name=sensor.name, source=source
            )
            if checkpoint is None:
                self._initialize_checkpoint(sensor=sensor, source=source, target=target)

    def retry_dead_letter(
        self, *, registry: SensorRegistry, sensor_name: str, event_id: str
    ) -> SensorTickStatus:
        """Re-evaluate one dead-lettered event immediately; the outcome is recorded."""

        sensor: LoadedSensor = self._require_event_sensor(
            registry=registry, sensor_name=sensor_name
        )
        event: SensorEvent = self._rebuild_event(sensor=sensor, event_id=event_id)
        state: TickAttemptState = self._repository.tick_attempt_state(
            sensor_name=sensor_name, event_id=event_id
        )
        evaluation: SensorEvaluation = self._evaluate_event(
            sensor=sensor,
            event=event,
            attempt=state.failed_attempts + 1,
        )
        return evaluation.status

    def skip_dead_letter(
        self, *, registry: SensorRegistry, sensor_name: str, event_id: str, reason: str
    ) -> None:
        """Resolve one dead-lettered event with an explicit recorded reason."""

        sensor: LoadedSensor = self._require_event_sensor(
            registry=registry, sensor_name=sensor_name
        )
        state: TickAttemptState = self._repository.tick_attempt_state(
            sensor_name=sensor_name, event_id=event_id
        )
        now: str = self._repository.warehouse_now()
        self._repository.record_ticks(
            ticks=(
                AdapterSensorTickRecord(
                    tick_id=uuid4().hex,
                    sensor_name=sensor.name,
                    definition_fingerprint=sensor.identity_fingerprint,
                    kind=str(sensor.kind),
                    event_id=event_id,
                    event_kind=None,
                    attempt=state.failed_attempts,
                    status=str(SensorTickStatus.SKIPPED),
                    started_at=now,
                    completed_at=now,
                    error_message=None,
                    skip_reason=reason,
                    cursor=None,
                ),
            )
        )

    def _require_event_sensor(self, *, registry: SensorRegistry, sensor_name: str) -> LoadedSensor:
        sensor: LoadedSensor | None = registry.sensors.get(sensor_name)
        if sensor is None:
            raise SensorError(f"Unknown sensor '{sensor_name}'")
        if sensor.kind is not SensorKind.EVENT:
            raise SensorError(f"Sensor '{sensor_name}' is not an event sensor")
        return sensor

    def _rebuild_event(self, *, sensor: LoadedSensor, event_id: str) -> SensorEvent:
        declaration: EventSensorDeclaration = event_declaration_of(sensor)
        source: str = event_source_for(declaration.event_type)
        events: tuple[SensorEvent, ...] = ()
        if source == NODE_RESULTS_EVENT_SOURCE:
            row: NodeResultObservation | None = self._repository.fetch_node_result(
                result_id=event_id
            )
            if row is not None:
                events = events_from_node_result(
                    row=row,
                    previous_status=self._previous_status(row=row),
                    target=self._event_target,
                )
        else:
            invocation: InvocationObservation | None = self._repository.fetch_invocation(
                invocation_id=event_id
            )
            if invocation is not None:
                events = events_from_invocation(row=invocation, target=self._event_target)
        for event in events:
            if event.id == event_id:
                return event
        raise SensorError(
            f"Event '{event_id}' for sensor '{sensor.name}' cannot be rebuilt from "
            "persisted observations"
        )

    def _dispatch_event_sensor(
        self, *, sensor: LoadedSensor, target: str
    ) -> tuple[tuple[SensorDeliveryOutcome, ...], bool]:
        declaration: EventSensorDeclaration = event_declaration_of(sensor)
        source: str = event_source_for(declaration.event_type)
        checkpoint: SensorStreamPosition | None = self._repository.read_checkpoint(
            sensor_name=sensor.name, source=source
        )
        if checkpoint is None:
            self._initialize_checkpoint(sensor=sensor, source=source, target=target)
            return (), True
        stream_head: SensorStreamPosition | None = self._newest_position(
            source=source, target=target
        )
        if stream_head is None:
            return (), True
        outcomes: list[SensorDeliveryOutcome] = []
        current_position: SensorStreamPosition = checkpoint
        advanced_position: SensorStreamPosition | None = None
        while True:
            warehouse_now: str | None = (
                self._repository.warehouse_now()
                if self._event_age_limit(declaration=declaration) is not None
                else None
            )
            deliveries: tuple[SensorEventDelivery, ...] = self._deliveries_after(
                source=source,
                position=current_position,
                target=target,
            )
            deliveries = tuple(
                delivery
                for delivery in deliveries
                if _position_key(delivery.position) <= _position_key(stream_head)
            )
            if not deliveries:
                break
            for delivery in deliveries:
                blocked: bool = False
                for event in delivery.events:
                    if not matches_sensor(declaration=declaration, event=event):
                        continue
                    if warehouse_now is not None and self._event_is_expired(
                        declaration=declaration,
                        event=event,
                        warehouse_now=warehouse_now,
                    ):
                        continue
                    if not self._renew_dispatch_lease(sensor=sensor):
                        if advanced_position is not None:
                            self._repository.advance_checkpoint(
                                sensor_name=sensor.name,
                                source=source,
                                position=advanced_position,
                            )
                        return tuple(outcomes), False
                    outcome: SensorDeliveryOutcome = self._deliver_event(sensor=sensor, event=event)
                    outcomes.append(outcome)
                    if not outcome.resolved:
                        blocked = True
                        break
                if blocked:
                    if advanced_position is not None:
                        self._repository.advance_checkpoint(
                            sensor_name=sensor.name,
                            source=source,
                            position=advanced_position,
                        )
                    return tuple(outcomes), True
                advanced_position = delivery.position
            current_position = deliveries[-1].position
            if len(deliveries) < self._batch_limit:
                break
        if advanced_position is not None:
            self._repository.advance_checkpoint(
                sensor_name=sensor.name,
                source=source,
                position=advanced_position,
            )
        return tuple(outcomes), True

    def _newest_position(self, *, source: str, target: str) -> SensorStreamPosition | None:
        if source == NODE_RESULTS_EVENT_SOURCE:
            return self._repository.newest_node_result_position(target=target)
        return self._repository.newest_invocation_position(target=target)

    def _event_age_limit(self, *, declaration: EventSensorDeclaration) -> float | None:
        return (
            declaration.maximum_event_age_seconds
            if declaration.maximum_event_age_seconds is not None
            else self._maximum_event_age_seconds
        )

    def _event_is_expired(
        self,
        *,
        declaration: EventSensorDeclaration,
        event: SensorEvent,
        warehouse_now: str,
    ) -> bool:
        maximum_age: float | None = self._event_age_limit(declaration=declaration)
        if maximum_age is None:
            return False
        expires_at: str = shift_timestamp_text(value=event.completed_at, seconds=maximum_age)
        return timestamp_is_before(value=expires_at, other=warehouse_now)

    def _renew_dispatch_lease(self, *, sensor: LoadedSensor) -> bool:
        return self._repository.acquire_dispatch_lease(
            owner_id=self._dispatcher_id,
            ttl_seconds=max(
                self._lease_ttl_seconds,
                sensor.timeout_seconds + _LEASE_TIMEOUT_BUFFER_SECONDS,
            ),
        )

    def _initialize_checkpoint(self, *, sensor: LoadedSensor, source: str, target: str) -> None:
        newest: SensorStreamPosition | None
        if source == NODE_RESULTS_EVENT_SOURCE:
            newest = self._repository.newest_node_result_position(target=target)
        else:
            newest = self._repository.newest_invocation_position(target=target)
        self._repository.advance_checkpoint(
            sensor_name=sensor.name,
            source=source,
            position=newest if newest is not None else _EPOCH_POSITION,
        )

    def _deliveries_after(
        self, *, source: str, position: SensorStreamPosition, target: str
    ) -> tuple[SensorEventDelivery, ...]:
        if source == NODE_RESULTS_EVENT_SOURCE:
            return tuple(
                SensorEventDelivery(
                    position=SensorStreamPosition(
                        completed_at=row.completed_at, result_id=row.result_id
                    ),
                    events=events_from_node_result(
                        row=row,
                        previous_status=self._previous_status(row=row),
                        target=self._event_target,
                    ),
                )
                for row in self._repository.fetch_node_results_after(
                    position=position, target=target, limit=self._batch_limit
                )
            )
        return tuple(
            SensorEventDelivery(
                position=SensorStreamPosition(
                    completed_at=row.completed_at, result_id=row.invocation_id
                ),
                events=events_from_invocation(row=row, target=self._event_target),
            )
            for row in self._repository.fetch_invocations_after(
                position=position, target=target, limit=self._batch_limit
            )
        )

    def _previous_status(self, *, row: NodeResultObservation) -> QualityResultStatus | None:
        return self._repository.previous_node_status(
            binding_key=row.binding_key,
            target=row.target_identity,
            position=SensorStreamPosition(completed_at=row.completed_at, result_id=row.result_id),
        )

    def _deliver_event(self, *, sensor: LoadedSensor, event: SensorEvent) -> SensorDeliveryOutcome:
        """Deliver one matched event; unresolved outcomes block the stream here."""

        state: TickAttemptState = self._repository.tick_attempt_state(
            sensor_name=sensor.name, event_id=event.id
        )
        if state.resolved:
            return SensorDeliveryOutcome(resolved=True)
        if state.failed_attempts >= sensor.retry_policy.max_attempts:
            self._record_dead_letter(sensor=sensor, event=event, state=state)
            return SensorDeliveryOutcome(resolved=True, dead_lettered=True)
        if state.failed_attempts > 0 and state.last_failed_at is not None:
            ready_at: str = shift_timestamp_text(
                value=state.last_failed_at, seconds=sensor.retry_policy.backoff_seconds
            )
            if timestamp_is_before(value=self._repository.warehouse_now(), other=ready_at):
                return SensorDeliveryOutcome(resolved=False)
        evaluation: SensorEvaluation = self._evaluate_event(
            sensor=sensor,
            event=event,
            attempt=state.failed_attempts + 1,
        )
        return SensorDeliveryOutcome(
            resolved=evaluation.status is not SensorTickStatus.FAILED,
            evaluation=evaluation,
        )

    def _record_dead_letter(
        self, *, sensor: LoadedSensor, event: SensorEvent, state: TickAttemptState
    ) -> None:
        now: str = self._repository.warehouse_now()
        self._repository.record_ticks(
            ticks=(
                AdapterSensorTickRecord(
                    tick_id=uuid4().hex,
                    sensor_name=sensor.name,
                    definition_fingerprint=sensor.identity_fingerprint,
                    kind=str(sensor.kind),
                    event_id=event.id,
                    event_kind=type(event).__name__,
                    attempt=state.failed_attempts,
                    status=str(SensorTickStatus.DEAD_LETTERED),
                    started_at=now,
                    completed_at=now,
                    error_message=state.last_error_message,
                    skip_reason=None,
                    cursor=None,
                ),
            )
        )

    def _evaluate_event(
        self,
        *,
        sensor: LoadedSensor,
        event: SensorEvent,
        attempt: int,
    ) -> SensorEvaluation:
        tick_id: str = uuid4().hex
        started_at: str = self._repository.warehouse_now()
        self._record_tick_row(
            sensor=sensor,
            tick_id=tick_id,
            event=event,
            attempt=attempt,
            status=SensorTickStatus.STARTED,
            started_at=started_at,
            completed_at=None,
            evaluation=None,
        )
        context: EventSensorContext[SensorEvent] = EventSensorContext(
            event=event,
            target=self._event_target,
            steps=DurableStepRunner(
                store=RepositoryStepStore(
                    repository=self._repository,
                    sensor_name=sensor.name,
                    event_id=event.id,
                )
            ),
        )
        evaluation: SensorEvaluation = evaluate_sensor_handler(
            sensor=sensor, context=context, providers=self._providers
        )
        self._record_tick_row(
            sensor=sensor,
            tick_id=tick_id,
            event=event,
            attempt=attempt,
            status=evaluation.status,
            started_at=started_at,
            completed_at=self._repository.warehouse_now(),
            evaluation=evaluation,
        )
        return evaluation

    def _dispatch_polling_sensor(self, *, sensor: LoadedSensor) -> SensorDeliveryOutcome | None:
        declaration: object = sensor.declaration
        interval_seconds: float = (
            declaration.minimum_interval_seconds
            if isinstance(declaration, PollingSensorDeclaration)
            else 0.0
        )
        state: PollingTickState = self._repository.polling_tick_state(sensor_name=sensor.name)
        now: str = self._repository.warehouse_now()
        if state.last_started_at is not None and timestamp_is_before(
            value=now,
            other=shift_timestamp_text(value=state.last_started_at, seconds=interval_seconds),
        ):
            return None
        tick_id: str = uuid4().hex
        self._record_tick_row(
            sensor=sensor,
            tick_id=tick_id,
            event=None,
            attempt=1,
            status=SensorTickStatus.STARTED,
            started_at=now,
            completed_at=None,
            evaluation=None,
        )
        context: PollingSensorContext = PollingSensorContext(
            cursor=state.cursor,
            last_success_at=state.last_success_at,
            target=self._event_target,
        )
        evaluation: SensorEvaluation = evaluate_sensor_handler(
            sensor=sensor, context=context, providers=self._providers
        )
        self._record_tick_row(
            sensor=sensor,
            tick_id=tick_id,
            event=None,
            attempt=1,
            status=evaluation.status,
            started_at=now,
            completed_at=self._repository.warehouse_now(),
            evaluation=evaluation,
        )
        return SensorDeliveryOutcome(resolved=True, evaluation=evaluation)

    def _record_tick_row(
        self,
        *,
        sensor: LoadedSensor,
        tick_id: str,
        event: SensorEvent | None,
        attempt: int,
        status: SensorTickStatus,
        started_at: str,
        completed_at: str | None,
        evaluation: SensorEvaluation | None,
    ) -> None:
        self._repository.record_ticks(
            ticks=(
                AdapterSensorTickRecord(
                    tick_id=tick_id,
                    sensor_name=sensor.name,
                    definition_fingerprint=sensor.identity_fingerprint,
                    kind=str(sensor.kind),
                    event_id=event.id if event is not None else None,
                    event_kind=type(event).__name__ if event is not None else None,
                    attempt=attempt,
                    status=str(status),
                    started_at=started_at,
                    completed_at=completed_at,
                    error_message=evaluation.error_message if evaluation is not None else None,
                    skip_reason=evaluation.skip_reason if evaluation is not None else None,
                    cursor=evaluation.cursor if evaluation is not None else None,
                ),
            )
        )
