"""The context handed to authored event sensor handlers."""

from __future__ import annotations

from collections.abc import Callable

from sqltide._native import DurableStepRunner, InMemoryStepStore
from sqltide.sensors.types import StepPolicy


class EventSensorContext[EventT]:
    """One event delivery: the derived event plus durable step access."""

    def __init__(
        self,
        *,
        event: EventT,
        target: str = "",
        steps: DurableStepRunner | None = None,
    ) -> None:
        self._event: EventT = event
        self._target: str = target
        self._steps: DurableStepRunner = (
            steps if steps is not None else DurableStepRunner(store=InMemoryStepStore())
        )

    @property
    def event(self) -> EventT:
        return self._event

    @property
    def target(self) -> str:
        return self._target

    def step(
        self,
        key: str,
        fn: Callable[[], object],
        policy: StepPolicy = StepPolicy.AT_LEAST_ONCE,
    ) -> object:
        """Run one durable step; re-runs resume with the memoized result."""

        return self._steps.run(key, fn, policy)
