import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sqltide._native import deserialize_destruction_plan, serialize_destruction_plan
from sqltide.executor.destruction.classes.relational_destruction_plan_store import (
    RelationalDestructionPlanStore,
)
from sqltide.executor.destruction.exceptions import (
    DestructionPlanCorruptError,
    DestructionPlanExpiredError,
    DestructionValidationError,
)
from sqltide.executor.destruction.models import DestructionPlan
from sqltide.executor.destruction.types import (
    DestructionOwnership,
    DestructionRelationKind,
)
from tests.unit.src.sqltide.executor.destruction._test_types import (
    CorruptTimestampTestCase,
    DurableStoreTestCase,
    LegacyDestructionPlanTestCase,
)
from tests.unit.src.sqltide.executor.destruction.helpers import (
    MutableClock,
    build_complete_stored_destruction_plan,
    build_stored_destruction_plan,
    consume_saved_plan_once,
    destruction_store_url,
)


@pytest.mark.parametrize(
    "test_case",
    [DurableStoreTestCase(description="reviewed complete plan survives a store restart")],
    ids=lambda case: case.description,
)
def test_given_reviewed_complete_plan_when_store_restarts_then_payload_and_review_persist(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = replace(
        build_complete_stored_destruction_plan(now=now),
        relation_drop_size_limit=107_374_182_400,
        relation_drop_size_server_limit=50_000_000_000,
        relation_drop_size_override=107_374_182_400,
        include_orphans=True,
    )
    url: str = destruction_store_url(tmp_path=tmp_path)
    first: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    first.save(plan=plan, actor=test_case.actor)
    reviewed_at: datetime = first.mark_reviewed(plan_id=plan.plan_id, actor=test_case.actor)
    caller_replacement: DestructionPlan = replace(plan, challenges=("changed-after-save",))
    first.close()

    second: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    reloaded: DestructionPlan = second.get(plan_id=plan.plan_id, actor=test_case.actor)

    assert reloaded == plan
    assert reloaded is not plan
    assert reloaded != caller_replacement
    assert reloaded.relations[0].kind is DestructionRelationKind.TABLE
    assert reloaded.relations[0].ownership == (
        DestructionOwnership.CURRENT_MANIFEST,
        DestructionOwnership.OWNERSHIP_LEDGER,
    )
    assert reloaded.relations[0].dependency_relation_names == test_case.expected_dependency_names
    assert reloaded.include_orphans is True
    assert second.reviewed_at(plan_id=plan.plan_id, actor=test_case.actor) == reviewed_at
    second.close()


@pytest.mark.parametrize(
    "test_case",
    [
        LegacyDestructionPlanTestCase(
            description="legacy plan exposes unknown DROP safety evidence",
            expected_policy_observed=False,
            expected_limit=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_legacy_payload_when_deserializing_then_drop_policy_is_not_reported_unlimited(
    test_case: LegacyDestructionPlanTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    payload: dict[str, object] = json.loads(
        serialize_destruction_plan(build_complete_stored_destruction_plan(now=now))
    )
    plan_payload: dict[str, object] = cast(dict[str, object], payload["plan"])
    for field in (
        "relation_drop_size_limit",
        "relation_drop_size_server_limit",
        "relation_drop_size_override",
        "relation_drop_size_policy_observed",
        "include_orphans",
    ):
        plan_payload.pop(field)

    legacy: DestructionPlan = deserialize_destruction_plan(json.dumps(payload))

    assert legacy.relation_drop_size_policy_observed is test_case.expected_policy_observed
    assert legacy.relation_drop_size_limit == test_case.expected_limit
    assert legacy.include_orphans is False


@pytest.mark.parametrize(
    "test_case",
    [
        CorruptTimestampTestCase(
            description="expiry past year 9999 in UTC",
            created_at="9999-12-31T22:00:00Z",
            expires_at="9999-12-31T23:30:00-01:00",
            expected_message="expires_at is out of range in UTC",
        ),
        CorruptTimestampTestCase(
            description="creation before year 1 in UTC",
            created_at="0001-01-01T00:30:00+01:00",
            expires_at="0001-01-02T00:00:00Z",
            expected_message="created_at is out of range in UTC",
        ),
        CorruptTimestampTestCase(
            description="naive creation time",
            created_at="2026-08-24T12:00:00",
            expires_at="2026-08-25T12:00:00Z",
            expected_message="created_at must include a timezone",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unusable_timestamp_when_deserializing_then_plan_is_corrupt(
    test_case: CorruptTimestampTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    payload: dict[str, object] = json.loads(
        serialize_destruction_plan(build_complete_stored_destruction_plan(now=now))
    )
    plan_payload: dict[str, object] = cast(dict[str, object], payload["plan"])
    plan_payload["created_at"] = test_case.created_at
    plan_payload["expires_at"] = test_case.expires_at

    with pytest.raises(DestructionPlanCorruptError, match=test_case.expected_message):
        deserialize_destruction_plan(json.dumps(payload))


@pytest.mark.parametrize(
    "test_case",
    [DurableStoreTestCase(description="two store instances atomically consume once")],
    ids=lambda case: case.description,
)
def test_given_two_store_instances_when_consuming_concurrently_then_only_one_succeeds(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    url: str = destruction_store_url(tmp_path=tmp_path)
    first: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    second: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    first.save(plan=plan, actor=test_case.actor)
    first.mark_reviewed(plan_id=plan.plan_id, actor=test_case.actor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes: tuple[str, ...] = tuple(
            future.result()
            for future in (
                executor.submit(consume_saved_plan_once, 0, store=first, plan=plan),
                executor.submit(consume_saved_plan_once, 1, store=second, plan=plan),
            )
        )

    with sqlite3.connect(tmp_path / "control.db") as connection:
        lifecycle: tuple[object, ...] | None = connection.execute(
            "SELECT status, consumed_at FROM sqltide_destruction_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
    assert tuple(sorted(outcomes)) == ("consumed", "not_found")
    assert lifecycle is not None
    assert lifecycle[0] == test_case.expected_status
    assert lifecycle[1] is not None
    first.close()
    second.close()


@pytest.mark.parametrize(
    "test_case",
    [DurableStoreTestCase(description="creator binding applies to every store operation")],
    ids=lambda case: case.description,
)
def test_given_other_actor_when_getting_reviewing_or_consuming_then_plan_is_hidden(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    base: DestructionPlan = build_stored_destruction_plan(now=now)
    get_plan: DestructionPlan = replace(base, plan_id="get-plan")
    review_plan: DestructionPlan = replace(base, plan_id="review-plan")
    consume_plan: DestructionPlan = replace(base, plan_id="consume-plan")
    store: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=destruction_store_url(tmp_path=tmp_path), clock=MutableClock(now)
    )
    store.save(plan=get_plan, actor=test_case.actor)
    store.save(plan=review_plan, actor=test_case.actor)
    store.save(plan=consume_plan, actor=test_case.actor)
    store.mark_reviewed(plan_id=consume_plan.plan_id, actor=test_case.actor)

    with pytest.raises(test_case.expected_error):
        store.get(plan_id=get_plan.plan_id, actor=test_case.other_actor)
    with pytest.raises(test_case.expected_error):
        store.mark_reviewed(plan_id=review_plan.plan_id, actor=test_case.other_actor)
    with pytest.raises(test_case.expected_error):
        store.consume(
            plan_id=consume_plan.plan_id,
            challenge_responses=consume_plan.challenges,
            actor=test_case.other_actor,
        )
    assert store.get(plan_id=consume_plan.plan_id, actor=test_case.actor) == consume_plan
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DurableStoreTestCase(
            description="expired plan remains durably unavailable",
            expected_error=DestructionPlanExpiredError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_plan_at_expiry_when_store_restarts_then_expiration_remains_enforced(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    clock: MutableClock = MutableClock(now)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    url: str = destruction_store_url(tmp_path=tmp_path)
    first: RelationalDestructionPlanStore = RelationalDestructionPlanStore(url=url, clock=clock)
    first.save(plan=plan, actor=test_case.actor)
    clock.now = plan.expires_at

    with pytest.raises(test_case.expected_error):
        first.get(plan_id=plan.plan_id, actor=test_case.actor)
    first.close()
    second: RelationalDestructionPlanStore = RelationalDestructionPlanStore(url=url, clock=clock)
    with pytest.raises(test_case.expected_error):
        second.mark_reviewed(plan_id=plan.plan_id, actor=test_case.actor)
    with sqlite3.connect(tmp_path / "control.db") as connection:
        status_row: tuple[object, ...] | None = connection.execute(
            "SELECT status FROM sqltide_destruction_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
    assert status_row is not None
    assert status_row[0] == "expired"
    second.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DurableStoreTestCase(
            description="corrupt payload is rejected after restart",
            expected_error=DestructionPlanCorruptError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_corrupt_payload_when_store_restarts_then_plan_fails_closed(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    url: str = destruction_store_url(tmp_path=tmp_path)
    first: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    first.save(plan=plan, actor=test_case.actor)
    first.close()
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            "UPDATE sqltide_destruction_plans SET payload_json = '{}' WHERE plan_id = ?",
            (plan.plan_id,),
        )

    second: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=url, clock=MutableClock(now)
    )
    with pytest.raises(test_case.expected_error, match="checksum"):
        second.get(plan_id=plan.plan_id, actor=test_case.actor)
    second.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DurableStoreTestCase(
            description="duplicate create and inexact challenge fail safely",
            expected_error=DestructionValidationError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_reviewed_plan_when_recreated_or_misconfirmed_then_state_is_unchanged(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    now: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    store: RelationalDestructionPlanStore = RelationalDestructionPlanStore(
        url=destruction_store_url(tmp_path=tmp_path), clock=MutableClock(now)
    )
    store.save(plan=plan, actor=test_case.actor)
    with pytest.raises(test_case.expected_error, match="already exists"):
        store.save(plan=replace(plan, challenges=("replacement",)), actor=test_case.actor)
    store.mark_reviewed(plan_id=plan.plan_id, actor=test_case.actor)

    with pytest.raises(test_case.expected_challenge_error, match="exactly match"):
        store.consume(
            plan_id=plan.plan_id,
            challenge_responses=("wrong",),
            actor=test_case.actor,
        )
    assert store.get(plan_id=plan.plan_id, actor=test_case.actor) == plan
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        DurableStoreTestCase(
            description="incompatible destruction schema remains rejected",
            other_actor="incompatible",
            expected_error=DestructionValidationError,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_incompatible_schema_component_when_opening_then_bootstrap_rejects_store(
    tmp_path: Path,
    test_case: DurableStoreTestCase,
) -> None:
    url: str = destruction_store_url(tmp_path=tmp_path)
    first: RelationalDestructionPlanStore = RelationalDestructionPlanStore(url=url)
    first.close()
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            "UPDATE sqltide_schema_versions SET version = 2 WHERE component = ?",
            ("destruction_plans",),
        )

    with pytest.raises(test_case.expected_error, match=test_case.other_actor):
        RelationalDestructionPlanStore(url=url)


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
