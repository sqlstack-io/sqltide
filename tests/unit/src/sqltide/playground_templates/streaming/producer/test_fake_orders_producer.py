from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import cast

import pytest
from kafka import KafkaProducer

from sqltide.playground_templates.streaming.producer.fake_orders_producer import (
    ActiveOrder,
    CommerceEvent,
    ProducerState,
    Product,
    build_order_event,
    create_order,
    load_state,
    publish_pending,
    save_state,
)
from tests.unit.src.sqltide.playground_templates.streaming.producer._test_types import (
    CrashReplayTestCase,
    PendingPublishTestCase,
    RandomContinuationTestCase,
)


class RecordingProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, object]] = []
        self.flush_count: int = 0

    def send(self, topic: str, *, key: bytes, value: object) -> None:
        self.sent.append((topic, key, value))

    def flush(self) -> None:
        self.flush_count += 1


class CrashingProducer(RecordingProducer):
    def flush(self) -> None:
        super().flush()
        raise RuntimeError("simulated crash after broker send")


@pytest.mark.parametrize(
    "test_case",
    [
        PendingPublishTestCase(
            description="checkpointed pending event publishes once after restart",
            expected_send_count=1,
            expected_flush_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_checkpointed_pending_event_when_restarting_then_exact_event_is_published_once(
    test_case: PendingPublishTestCase,
    tmp_path: Path,
) -> None:
    state_path: Path = tmp_path / "state.json"
    event: CommerceEvent = build_order_event(
        ActiveOrder(
            order_id="ord_00000001",
            customer_id="cust_0001",
            product=Product("Northstar USB-C Dock", "electronics", 12900),
            quantity=2,
            region_code="us-east",
        ),
        datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        event_id="evt_0000000001",
        event_type="order_created",
    )
    save_state(
        state_path, ProducerState(next_order_number=2, next_event_number=2, pending_events=[event])
    )
    restarted: ProducerState = load_state(state_path)
    producer: RecordingProducer = RecordingProducer()
    rng: Random = Random(86)

    publish_pending(
        cast(KafkaProducer, producer),
        "source.order_events.live",
        restarted,
        state_path,
        rng,
    )
    publish_pending(
        cast(KafkaProducer, producer),
        "source.order_events.live",
        load_state(state_path),
        state_path,
        rng,
    )

    assert len(producer.sent) == test_case.expected_send_count
    assert producer.sent == [("source.order_events.live", b"ord_00000001", event)]
    assert producer.flush_count == test_case.expected_flush_count
    assert load_state(state_path).pending_events == []


@pytest.mark.parametrize(
    "test_case",
    [
        CrashReplayTestCase(
            description="crash after send resends the identical checkpointed event",
            expected_crash_send_count=1,
            expected_restart_send_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_crash_after_send_when_restarting_then_resends_identical_checkpointed_event(
    test_case: CrashReplayTestCase,
    tmp_path: Path,
) -> None:
    state_path: Path = tmp_path / "state.json"
    event: CommerceEvent = build_order_event(
        ActiveOrder(
            order_id="ord_00000001",
            customer_id="cust_0001",
            product=Product("Northstar USB-C Dock", "electronics", 12900),
            quantity=2,
            region_code="us-east",
        ),
        datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        event_id="evt_0000000001",
        event_type="order_created",
    )
    state: ProducerState = ProducerState(
        next_order_number=2, next_event_number=2, pending_events=[event]
    )
    crashing: CrashingProducer = CrashingProducer()
    rng: Random = Random(86)

    with pytest.raises(RuntimeError, match="simulated crash"):
        publish_pending(
            cast(KafkaProducer, crashing),
            "source.order_events.live",
            state,
            state_path,
            rng,
        )

    restarted: RecordingProducer = RecordingProducer()
    publish_pending(
        cast(KafkaProducer, restarted),
        "source.order_events.live",
        load_state(state_path),
        state_path,
        rng,
    )

    expected: list[tuple[str, bytes, object]] = [
        ("source.order_events.live", b"ord_00000001", event)
    ]
    assert len(crashing.sent) == test_case.expected_crash_send_count
    assert len(restarted.sent) == test_case.expected_restart_send_count
    assert crashing.sent == expected
    assert restarted.sent == expected
    assert load_state(state_path).pending_events == []


@pytest.mark.parametrize(
    "test_case",
    [
        RandomContinuationTestCase(
            description="clean restart continues the persisted random sequence",
            expected_order_id="ord_00000002",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_clean_restart_when_creating_next_order_then_random_sequence_continues(
    test_case: RandomContinuationTestCase,
    tmp_path: Path,
) -> None:
    state_path: Path = tmp_path / "state.json"
    uninterrupted_rng: Random = Random(86)
    state: ProducerState = ProducerState()
    _ = create_order(uninterrupted_rng, state)
    publish_pending(
        cast(KafkaProducer, RecordingProducer()),
        "source.order_events.live",
        state,
        state_path,
        uninterrupted_rng,
    )
    expected: ActiveOrder = create_order(uninterrupted_rng, state)

    restarted_state: ProducerState = load_state(state_path)
    restarted_rng: Random = Random(86)
    assert restarted_state.rng_state is not None
    restarted_rng.setstate(restarted_state.rng_state)
    actual: ActiveOrder = create_order(restarted_rng, restarted_state)

    assert actual.order_id == test_case.expected_order_id
    assert actual == expected
