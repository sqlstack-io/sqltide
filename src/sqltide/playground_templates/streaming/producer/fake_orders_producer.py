from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

from kafka import KafkaProducer

SCHEMA_VERSION = 1
REGION_CODES: tuple[str, ...] = ("us-east", "us-west", "eu-west", "ap-south")
CUSTOMER_IDS: tuple[str, ...] = tuple(f"cust_{index:04d}" for index in range(1, 51))
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("paid", "cancelled"),
    "paid": ("shipped", "refunded"),
    "shipped": ("delivered",),
}
TERMINAL_STATUSES: frozenset[str] = frozenset({"delivered", "cancelled", "refunded"})
RandomState = tuple[int, tuple[int, ...], float | None]


@dataclass(frozen=True)
class Product:
    name: str
    category: str
    unit_price_cents: int


PRODUCTS: tuple[Product, ...] = (
    Product("Northstar USB-C Dock", "electronics", 12900),
    Product("Aurora Wireless Headphones", "electronics", 18900),
    Product("Lumen Braided Cable", "electronics", 2400),
    Product("Harbor Trail Jacket", "apparel", 14900),
    Product("Summit Running Shoes", "apparel", 11200),
    Product("Field Notes Hardcover", "office", 1800),
    Product("Studio Gel Pen Set", "office", 1400),
    Product("Atlas Coffee Subscription", "grocery", 3200),
)


class CommerceEvent(TypedDict):
    """The fixed-key, fixed-type JSON contract emitted for every event."""

    event_id: str
    event_type: Literal["order_created", "order_status_changed"]
    schema_version: int
    order_id: str
    customer_id: str
    product: str
    category: str
    quantity: int
    unit_price_cents: int
    currency: str
    status: str
    region_code: str
    event_at: str


@dataclass
class ActiveOrder:
    order_id: str
    customer_id: str
    product: Product
    quantity: int
    region_code: str
    status: str = "created"
    ticks_until_next: int = field(default=0)


@dataclass
class ProducerState:
    """Durable local state so producer restarts continue active order lifecycles."""

    active_orders: list[ActiveOrder] = field(default_factory=list)
    next_order_number: int = 1
    next_event_number: int = 1
    history_seeded: bool = False
    pending_events: list[CommerceEvent] = field(default_factory=list)
    rng_state: RandomState | None = None


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_order_event(
    order: ActiveOrder,
    event_at: datetime,
    *,
    event_id: str,
    event_type: Literal["order_created", "order_status_changed"],
) -> CommerceEvent:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "product": order.product.name,
        "category": order.product.category,
        "quantity": order.quantity,
        "unit_price_cents": order.product.unit_price_cents,
        "currency": "USD",
        "status": order.status,
        "region_code": order.region_code,
        "event_at": utc_text(event_at),
    }


def create_order(rng: random.Random, state: ProducerState) -> ActiveOrder:
    order = ActiveOrder(
        order_id=f"ord_{state.next_order_number:08d}",
        customer_id=rng.choice(CUSTOMER_IDS),
        product=rng.choice(PRODUCTS),
        quantity=rng.randint(1, 5),
        region_code=rng.choice(REGION_CODES),
        ticks_until_next=rng.randint(2, 6),
    )
    state.next_order_number += 1
    return order


def next_event_id(state: ProducerState) -> str:
    event_id = f"evt_{state.next_event_number:010d}"
    state.next_event_number += 1
    return event_id


def required_int(payload: dict[str, object], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"producer state field {key!r} must be an integer")
    return value


def tuple_tree(value: object) -> object:
    if isinstance(value, list):
        return tuple(tuple_tree(item) for item in value)
    return value


def load_state(path: Path) -> ProducerState:
    if not path.exists():
        return ProducerState()
    payload = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    orders: list[ActiveOrder] = []
    for item_value in cast(list[object], payload.get("active_orders", [])):
        item = cast(dict[str, object], item_value)
        orders.append(
            ActiveOrder(
                order_id=str(item["order_id"]),
                customer_id=str(item["customer_id"]),
                product=Product(
                    name=str(item["product"]),
                    category=str(item["category"]),
                    unit_price_cents=required_int(item, "unit_price_cents"),
                ),
                quantity=required_int(item, "quantity"),
                region_code=str(item["region_code"]),
                status=str(item["status"]),
                ticks_until_next=required_int(item, "ticks_until_next"),
            )
        )
    raw_rng_state = tuple_tree(payload.get("rng_state"))
    return ProducerState(
        active_orders=orders,
        next_order_number=required_int(payload, "next_order_number", default=1),
        next_event_number=required_int(payload, "next_event_number", default=1),
        history_seeded=bool(payload.get("history_seeded", False)),
        pending_events=[
            cast(CommerceEvent, item)
            for item in cast(list[object], payload.get("pending_events", []))
            if isinstance(item, dict)
        ],
        rng_state=cast(RandomState, raw_rng_state) if isinstance(raw_rng_state, tuple) else None,
    )


def save_state(path: Path, state: ProducerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_orders": [
            {
                "order_id": order.order_id,
                "customer_id": order.customer_id,
                "product": order.product.name,
                "category": order.product.category,
                "unit_price_cents": order.product.unit_price_cents,
                "quantity": order.quantity,
                "region_code": order.region_code,
                "status": order.status,
                "ticks_until_next": order.ticks_until_next,
            }
            for order in state.active_orders
        ],
        "next_order_number": state.next_order_number,
        "next_event_number": state.next_event_number,
        "history_seeded": state.history_seeded,
        "pending_events": state.pending_events,
        "rng_state": state.rng_state,
    }
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary_path.replace(path)


def advance_order(order: ActiveOrder, rng: random.Random) -> str | None:
    next_statuses = STATUS_TRANSITIONS.get(order.status)
    if next_statuses is None:
        return None
    weights = tuple(
        0.15 if status in {"cancelled", "refunded"} else 0.85 for status in next_statuses
    )
    return rng.choices(next_statuses, weights=weights, k=1)[0]


def build_producer(bootstrap_servers: str) -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=bootstrap_servers,
        acks="all",
        retries=10,
        value_serializer=lambda value: json.dumps(value, separators=(",", ":")).encode("utf-8"),
    )


def send_event(producer: KafkaProducer, topic: str, key: str, event: CommerceEvent) -> None:
    producer.send(topic, key=key.encode("utf-8"), value=event)


def publish_pending(
    producer: KafkaProducer,
    topic: str,
    state: ProducerState,
    state_path: Path,
    rng: random.Random,
) -> None:
    """Checkpoint first, then publish; a crash can only resend identical events."""

    state.rng_state = rng.getstate()
    save_state(state_path, state)
    if not state.pending_events:
        return
    for event in state.pending_events:
        send_event(producer, topic, event["order_id"], event)
    producer.flush()
    state.pending_events.clear()
    save_state(state_path, state)


def seed_history(
    rng: random.Random,
    state: ProducerState,
) -> int:
    """Publish a bounded, repeatable history so every first-run UI is immediately useful."""

    base_time = datetime.now(tz=UTC) - timedelta(hours=2)
    messages_sent = 0
    for index in range(48):
        order = create_order(rng, state)
        event_at = base_time + timedelta(minutes=index * 2)
        state.pending_events.append(
            build_order_event(
                order,
                event_at,
                event_id=next_event_id(state),
                event_type="order_created",
            )
        )
        messages_sent += 1

        statuses = (
            ("cancelled",)
            if index % 10 == 0
            else (("paid", "refunded") if index % 10 == 1 else ("paid", "shipped", "delivered"))
        )
        for step, status in enumerate(statuses, start=1):
            order.status = status
            state.pending_events.append(
                build_order_event(
                    order,
                    event_at + timedelta(minutes=step * 5),
                    event_id=next_event_id(state),
                    event_type="order_status_changed",
                )
            )
            messages_sent += 1
    state.history_seeded = True
    return messages_sent


def run_stream(producer: KafkaProducer, topic: str, rng: random.Random, state_path: Path) -> None:
    tick_interval_seconds = float(os.getenv("TICK_INTERVAL_SECONDS", "3"))
    new_orders_per_tick = int(os.getenv("NEW_ORDERS_PER_TICK", "2"))
    state = load_state(state_path)
    if state.rng_state is not None:
        rng.setstate(state.rng_state)
    publish_pending(producer, topic, state, state_path, rng)

    if not state.history_seeded:
        seeded_count = seed_history(rng, state)
        publish_pending(producer, topic, state, state_path, rng)
        print(f"published {seeded_count} deterministic historical order events", flush=True)

    while True:
        now = datetime.now(tz=UTC)
        messages_sent = 0

        for _ in range(new_orders_per_tick):
            order = create_order(rng, state)
            state.pending_events.append(
                build_order_event(
                    order,
                    now,
                    event_id=next_event_id(state),
                    event_type="order_created",
                )
            )
            state.active_orders.append(order)
            messages_sent += 1

        still_active: list[ActiveOrder] = []
        for order in state.active_orders:
            order.ticks_until_next -= 1
            if order.ticks_until_next > 0:
                still_active.append(order)
                continue

            next_status = advance_order(order, rng)
            if next_status is None:
                continue
            order.status = next_status
            state.pending_events.append(
                build_order_event(
                    order,
                    now,
                    event_id=next_event_id(state),
                    event_type="order_status_changed",
                )
            )
            messages_sent += 1
            if order.status not in TERMINAL_STATUSES:
                order.ticks_until_next = rng.randint(2, 6)
                still_active.append(order)

        state.active_orders = still_active
        publish_pending(producer, topic, state, state_path, rng)
        print(
            f"tick: sent {messages_sent} events, {len(state.active_orders)} active orders "
            f"at {utc_text(now)}",
            flush=True,
        )
        time.sleep(tick_interval_seconds)


def inject_future_event(
    producer: KafkaProducer,
    topic: str,
    rng: random.Random,
    *,
    future_seconds: int,
) -> None:
    injection_number = time.time_ns()
    order = ActiveOrder(
        order_id=f"ord_injected_{injection_number}",
        customer_id=rng.choice(CUSTOMER_IDS),
        product=rng.choice(PRODUCTS),
        quantity=rng.randint(1, 5),
        region_code=rng.choice(REGION_CODES),
    )
    future_at = datetime.now(tz=UTC) + timedelta(seconds=future_seconds)
    event = build_order_event(
        order,
        future_at,
        event_id=f"evt_injected_{injection_number}",
        event_type="order_created",
    )
    send_event(producer, topic, order.order_id, event)
    producer.flush()
    print(
        f"injected {event['event_id']} at {event['event_at']}; warning should recover after about "
        f"{future_seconds} seconds",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Produce deterministic-schema commerce events.")
    parser.add_argument("command", choices=("run", "inject-future"), nargs="?", default="run")
    args = parser.parse_args()

    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
    topic = os.getenv("KAFKA_TOPIC", "source.order_events.live")
    rng = random.Random(int(os.getenv("PRODUCER_RANDOM_SEED", "86")))
    state_path = Path(os.getenv("PRODUCER_STATE_PATH", "/var/lib/producer/state.json"))
    producer = build_producer(bootstrap_servers)

    try:
        if args.command == "inject-future":
            inject_future_event(
                producer,
                topic,
                rng,
                future_seconds=int(os.getenv("FUTURE_EVENT_SECONDS", "30")),
            )
            return
        run_stream(producer, topic, rng, state_path)
    finally:
        producer.close()


if __name__ == "__main__":
    main()
