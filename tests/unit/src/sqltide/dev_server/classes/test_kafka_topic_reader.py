from collections.abc import Callable
from typing import cast

import pytest
from kafka import KafkaAdminClient

from sqltide._native import build_kafka_client_config, build_kafka_topics_snapshot, kafka_client_key
from sqltide.compiler.discovery.models import KafkaSettings
from sqltide.dev_server.classes.kafka_topic_collector import KafkaTopicCollector
from sqltide.dev_server.models import KafkaTopicsSnapshot
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    KafkaClientReuseTestCase,
    KafkaTopicsSnapshotTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaTopicsSnapshotTestCase(
            description="maps topics with partitions, replication, and internal flags sorted",
            metadata=(
                {
                    "topic": "source.orders",
                    "is_internal": False,
                    "partitions": [
                        {"partition": 0, "replicas": [1, 2]},
                        {"partition": 1, "replicas": [2, 3]},
                    ],
                },
                {
                    "topic": "__consumer_offsets",
                    "is_internal": True,
                    "partitions": [{"partition": 0, "replicas": [1]}],
                },
            ),
            expected_topics=(
                ("__consumer_offsets", 1, 1, True),
                ("source.orders", 2, 2, False),
            ),
        ),
        KafkaTopicsSnapshotTestCase(
            description="tolerates topics without partition metadata",
            metadata=({"topic": "empty.topic", "is_internal": False, "partitions": []},),
            expected_topics=(("empty.topic", 0, 0, False),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_broker_metadata_when_building_snapshot_then_maps_topic_inventory(
    test_case: KafkaTopicsSnapshotTestCase,
) -> None:
    snapshot: KafkaTopicsSnapshot = build_kafka_topics_snapshot(metadata=test_case.metadata)

    assert (
        tuple(
            (topic.name, topic.partition_count, topic.replication_factor, topic.internal)
            for topic in snapshot.topics
        )
        == test_case.expected_topics
    )


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaClientReuseTestCase(
            description="reuses one topic admin client",
            broker_list="broker:9092",
            expected_client_count=1,
            expected_query_count=2,
            expected_close_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_repeated_topic_reads_when_collecting_then_reuses_and_closes_admin_client(
    test_case: KafkaClientReuseTestCase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admins: list[_FakeAdminClient] = []

    def admin_factory(**_config: object) -> _FakeAdminClient:
        admin: _FakeAdminClient = _FakeAdminClient()
        admins.append(admin)
        return admin

    collector: KafkaTopicCollector = KafkaTopicCollector(
        admin_factory=cast("Callable[..., KafkaAdminClient]", admin_factory)
    )
    kafka: KafkaSettings = KafkaSettings(broker_list=test_case.broker_list, topic="source.orders")

    key: tuple[str, tuple[tuple[str, str], ...]] = kafka_client_key(kafka=kafka)
    config: dict[str, object] = build_kafka_client_config(kafka=kafka)

    first: object = collector(key=key, config=config)
    second: object = collector(key=key, config=config)
    collector.close()

    assert first == second
    assert len(admins) == test_case.expected_client_count
    assert admins[0].describe_calls == test_case.expected_query_count
    assert admins[0].close_calls == test_case.expected_close_count
    assert (
        "Kafka topic client opened brokers=broker:9092 reads=1 opened=1 closed=0 active=1"
        in caplog.text
    )
    assert "Kafka topic client reused brokers=broker:9092 reads=2" in caplog.text
    assert "Kafka topic client closed reads=2 opened=1 closed=1 active=0" in caplog.text


class _FakeAdminClient:
    def __init__(self) -> None:
        self.describe_calls = 0
        self.close_calls = 0

    def describe_topics(self) -> list[dict[str, object]]:
        self.describe_calls += 1
        return [{"topic": "source.orders", "partitions": []}]

    def close(self) -> None:
        self.close_calls += 1


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
