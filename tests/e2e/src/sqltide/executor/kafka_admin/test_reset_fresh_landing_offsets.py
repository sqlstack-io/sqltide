import uuid
from itertools import chain

import pytest
from kafka import KafkaConsumer, KafkaProducer, TopicPartition
from kafka.consumer.fetcher import ConsumerRecord
from kafka.structs import OffsetAndMetadata

from sqltide._native import reset_fresh_landing_offsets
from sqltide.adapter.models import AdapterColumn, AdapterManagedSource
from sqltide.executor.kafka_admin.models import ConsumerGroupOffsetReset
from sqltide.executor.population.models import (
    PopulationManagedSource,
    PopulationSourcePreparation,
)
from tests.e2e.src.sqltide.conftest import E2EKafkaConnectionSettings
from tests.e2e.src.sqltide.executor.kafka_admin._test_types import (
    FreshLandingOffsetResetE2ETestCase,
)

_POLL_TIMEOUT_MS: int = 10_000


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        FreshLandingOffsetResetE2ETestCase(
            description="deleted end offset replays the earliest retained message",
            payload=b"first-message",
            committed_offset=1,
            expected_replayed_offsets=(0,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_stale_committed_offset_when_creating_fresh_landing_then_same_group_replays_earliest(
    test_case: FreshLandingOffsetResetE2ETestCase,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
) -> None:
    bootstrap_server: str = e2e_kafka_connection_settings.bootstrap_server
    suffix: str = uuid.uuid4().hex
    topic: str = f"sqltide-offset-reset-{suffix}"
    configured_group: str = f"sqltide_orders_{suffix}"
    database: str = "analytics-test"
    effective_group: str = f"{configured_group}_analytics_test"
    producer: KafkaProducer = KafkaProducer(bootstrap_servers=bootstrap_server)
    try:
        producer.send(topic, test_case.payload).get(timeout=10)
    finally:
        producer.close()
    partition: TopicPartition = TopicPartition(topic, 0)
    stale_consumer: KafkaConsumer = KafkaConsumer(
        bootstrap_servers=bootstrap_server,
        group_id=effective_group,
        enable_auto_commit=False,
    )
    try:
        stale_consumer.assign([partition])
        stale_consumer.commit({partition: OffsetAndMetadata(test_case.committed_offset, "")})
    finally:
        stale_consumer.close()
    managed_source: AdapterManagedSource = AdapterManagedSource(
        source_kind="kafka",
        name="kafka__orders",
        columns=(AdapterColumn(name="message", type="String"),),
        broker_list=bootstrap_server,
        topic=topic,
        consumer_group=configured_group,
        format="JSONAsString",
    )

    resets: tuple[ConsumerGroupOffsetReset, ...] = reset_fresh_landing_offsets(
        source_preparation=PopulationSourcePreparation(
            preserved_relation_names=(),
            created_relation_names=("raw__orders",),
            landing_views=(),
            managed_sources=(PopulationManagedSource(resource=managed_source, database=database),),
        ),
    )

    replay_consumer: KafkaConsumer = KafkaConsumer(
        bootstrap_servers=bootstrap_server,
        group_id=effective_group,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    try:
        replay_consumer.subscribe([topic])
        records: dict[TopicPartition, list[ConsumerRecord]] = replay_consumer.poll(
            timeout_ms=_POLL_TIMEOUT_MS
        )
    finally:
        replay_consumer.close()
    replayed_offsets: list[int] = [
        record.offset for record in chain.from_iterable(records.values())
    ]
    assert len(resets) == 1
    assert resets[0].deleted is True
    assert resets[0].error is None
    assert tuple(replayed_offsets) == test_case.expected_replayed_offsets


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
