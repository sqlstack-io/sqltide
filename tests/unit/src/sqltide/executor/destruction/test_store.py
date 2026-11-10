from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

import pytest

from sqltide._native import InMemoryDestructionPlanStore
from sqltide.executor.destruction.exceptions import (
    DestructionChallengeError,
    DestructionPlanExpiredError,
    DestructionPlanNotFoundError,
    DestructionPlanNotReviewedError,
)
from sqltide.executor.destruction.models import DestructionPlan
from tests.unit.src.sqltide.executor.destruction._test_types import (
    ConcurrentConsumeTestCase,
    StoreConsumeTwiceTestCase,
    StoreErrorTestCase,
    StoreExpiryTestCase,
)
from tests.unit.src.sqltide.executor.destruction.helpers import (
    MutableClock,
    build_stored_destruction_plan,
    consume_saved_plan_once,
)


@pytest.mark.parametrize(
    "test_case",
    [
        StoreErrorTestCase(
            description="an unreviewed plan cannot be consumed",
            expected_error=DestructionPlanNotReviewedError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unreviewed_plan_when_consuming_then_review_gate_blocks(
    test_case: StoreErrorTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=MutableClock(now))
    store.save(plan=plan, actor="alice")

    with pytest.raises(test_case.expected_error):
        store.consume(
            plan_id=plan.plan_id,
            challenge_responses=plan.challenges,
            actor="alice",
        )


@pytest.mark.parametrize(
    "test_case",
    [
        StoreErrorTestCase(
            description="out of order challenge responses cannot consume a reviewed plan",
            expected_error=DestructionChallengeError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_plan_when_responses_are_out_of_order_then_challenge_gate_blocks(
    test_case: StoreErrorTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    plan = replace(plan, challenges=("alpha", "beta"))
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=MutableClock(now))
    store.save(plan=plan, actor="alice")
    store.mark_reviewed(plan_id=plan.plan_id, actor="alice")

    with pytest.raises(test_case.expected_error):
        store.consume(
            plan_id=plan.plan_id,
            challenge_responses=("beta", "alpha"),
            actor="alice",
        )


@pytest.mark.parametrize(
    "test_case",
    [
        StoreConsumeTwiceTestCase(
            description="a reviewed confirmed plan is consumed only once",
            expected_second_error=DestructionPlanNotFoundError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_confirmed_plan_when_consumed_twice_then_only_first_succeeds(
    test_case: StoreConsumeTwiceTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=MutableClock(now))
    store.save(plan=plan, actor="alice")
    store.mark_reviewed(plan_id=plan.plan_id, actor="alice")

    consumed: DestructionPlan = store.consume(
        plan_id=plan.plan_id,
        challenge_responses=plan.challenges,
        actor="alice",
    )

    assert consumed is plan
    with pytest.raises(test_case.expected_second_error):
        store.consume(
            plan_id=plan.plan_id,
            challenge_responses=plan.challenges,
            actor="alice",
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ConcurrentConsumeTestCase(
            description="concurrent exact confirmations consume a plan once",
            expected_outcomes=("consumed", "not_found"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_concurrent_exact_confirmations_when_consuming_then_plan_is_single_use(
    test_case: ConcurrentConsumeTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=MutableClock(now))
    store.save(plan=plan, actor="alice")
    store.mark_reviewed(plan_id=plan.plan_id, actor="alice")

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes: tuple[str, ...] = tuple(
            executor.map(
                partial(consume_saved_plan_once, store=store, plan=plan),
                range(2),
            )
        )

    assert tuple(sorted(outcomes)) == test_case.expected_outcomes


@pytest.mark.parametrize(
    "test_case",
    [
        StoreExpiryTestCase(
            description="a plan expires exactly at its expiry boundary and is removed",
            expected_expiry_error=DestructionPlanExpiredError,
            expected_removed_error=DestructionPlanNotFoundError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_plan_at_expiry_when_marking_reviewed_then_expiry_gate_blocks(
    test_case: StoreExpiryTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    clock: MutableClock = MutableClock(now)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=clock)
    store.save(plan=plan, actor="alice")
    clock.now = plan.expires_at

    with pytest.raises(test_case.expected_expiry_error):
        store.mark_reviewed(plan_id=plan.plan_id, actor="alice")
    with pytest.raises(test_case.expected_removed_error):
        store.mark_reviewed(plan_id=plan.plan_id, actor="alice")


@pytest.mark.parametrize(
    "test_case",
    [
        StoreErrorTestCase(
            description="a different actor cannot discover a stored plan",
            expected_error=DestructionPlanNotFoundError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_different_actor_when_accessing_plan_then_plan_is_not_disclosed(
    test_case: StoreErrorTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore(clock=MutableClock(now))
    store.save(plan=plan, actor="alice")

    with pytest.raises(test_case.expected_error):
        store.get(plan_id=plan.plan_id, actor="bob")


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
