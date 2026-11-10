from collections.abc import Callable
from typing import cast

import pytest
from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
from kafka.structs import OffsetAndMetadata

from sqltide._native import (
    KafkaLagReader,
    build_kafka_client_config,
    build_kafka_lag_snapshot,
    kafka_client_key,
)
from sqltide.compiler.discovery.models import KafkaSettings
from sqltide.dev_server.classes.kafka_lag_collector import KafkaLagCollector
from sqltide.dev_server.models import KafkaLagSnapshot, KafkaPartitionLag
from sqltide.dev_server.types import KafkaLagOffsets
from tests.unit.src.sqltide.dev_server.classes._test_types import (
    KafkaClientReuseTestCase,
    KafkaCredentialIsolationTestCase,
    KafkaLagReaderCacheTestCase,
    KafkaLagSnapshotTestCase,
)
from tests.unit.src.sqltide.dev_server.classes.helpers import (
    ScriptedLagCollector,
    broker_unavailable,
)


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaLagSnapshotTestCase(
            description="committed offsets produce exact partition and total lag",
            partition_ids=frozenset({0, 1}),
            committed_offsets=((0, 95), (1, 110)),
            end_offsets=((0, 100), (1, 125)),
            expected_total_messages=20,
            expected_partition_lags=(5, 15),
        ),
        KafkaLagSnapshotTestCase(
            description="empty uncommitted partition has zero lag",
            partition_ids=frozenset({0}),
            committed_offsets=(),
            end_offsets=((0, 0),),
            expected_total_messages=0,
            expected_partition_lags=(0,),
        ),
        KafkaLagSnapshotTestCase(
            description="nonempty uncommitted partition makes lag unavailable",
            partition_ids=frozenset({0, 1}),
            committed_offsets=((0, 90),),
            end_offsets=((0, 100), (1, 25)),
            expected_total_messages=None,
            expected_partition_lags=(10, None),
        ),
        KafkaLagSnapshotTestCase(
            description="missing topic metadata makes lag unavailable",
            partition_ids=frozenset(),
            committed_offsets=(),
            end_offsets=(),
            expected_total_messages=None,
            expected_partition_lags=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_broker_offsets_when_building_snapshot_then_reports_only_exact_lag(
    test_case: KafkaLagSnapshotTestCase,
) -> None:
    snapshot: KafkaLagSnapshot = build_kafka_lag_snapshot(
        partition_ids=test_case.partition_ids,
        committed_offsets=dict(test_case.committed_offsets),
        end_offsets=dict(test_case.end_offsets),
    )

    assert snapshot.total_messages == test_case.expected_total_messages
    assert tuple(partition.lag_messages for partition in snapshot.partitions) == (
        test_case.expected_partition_lags
    )


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaClientReuseTestCase(
            description="reuses one lag client pair",
            broker_list="broker:9092",
            expected_client_count=1,
            expected_query_count=2,
            expected_close_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multiple_topics_when_collecting_lag_then_reuses_and_closes_broker_clients(
    test_case: KafkaClientReuseTestCase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admins: list[_FakeAdminClient] = []
    consumers: list[_FakeConsumer] = []

    def admin_factory(**_config: object) -> _FakeAdminClient:
        admin: _FakeAdminClient = _FakeAdminClient()
        admins.append(admin)
        return admin

    def consumer_factory(**_config: object) -> _FakeConsumer:
        consumer: _FakeConsumer = _FakeConsumer()
        consumers.append(consumer)
        return consumer

    collector: KafkaLagCollector = KafkaLagCollector(
        admin_factory=cast("Callable[..., KafkaAdminClient]", admin_factory),
        consumer_factory=cast("Callable[..., KafkaConsumer]", consumer_factory),
    )
    kafka: KafkaSettings = KafkaSettings(
        broker_list=test_case.broker_list,
        topic="source.orders",
        consumer_group="sqltide-orders",
    )
    key: tuple[str, tuple[tuple[str, str], ...]] = kafka_client_key(kafka=kafka)
    config: dict[str, object] = build_kafka_client_config(kafka=kafka)

    first: KafkaLagOffsets = collector(
        key=key, config=config, topic="source.orders", consumer_group="sqltide-orders_default"
    )
    second: KafkaLagOffsets = collector(
        key=key, config=config, topic="source.prices", consumer_group="sqltide-prices_default"
    )
    collector.close()

    assert first == ((("source.orders", 0, 5),), (0,), ((0, 10),))
    assert second == ((("source.prices", 0, 5),), (0,), ((0, 10),))
    assert len(admins) == test_case.expected_client_count
    assert len(consumers) == test_case.expected_client_count
    assert admins[0].offset_calls == test_case.expected_query_count
    assert consumers[0].end_offset_calls == test_case.expected_query_count
    assert admins[0].close_calls == test_case.expected_close_count
    assert consumers[0].close_calls == test_case.expected_close_count
    assert (
        "Kafka lag clients opened brokers=broker:9092 opened_pairs=1 closed_pairs=0 "
        "active_pairs=1" in caplog.text
    )
    assert "Kafka lag clients reused brokers=broker:9092 reads=2" in caplog.text
    assert (
        "Kafka lag clients closed reads=2 opened_pairs=1 closed_pairs=1 active_pairs=0"
        in caplog.text
    )


class _FakeAdminClient:
    def __init__(self) -> None:
        self.offset_calls = 0
        self.close_calls = 0

    def list_consumer_group_offsets(
        self, _consumer_group: str
    ) -> dict[TopicPartition, OffsetAndMetadata]:
        self.offset_calls += 1
        topic: str = "source.orders" if self.offset_calls == 1 else "source.prices"
        return {TopicPartition(topic, 0): OffsetAndMetadata(5, "")}

    def close(self) -> None:
        self.close_calls += 1


class _FakeConsumer:
    def __init__(self) -> None:
        self.end_offset_calls = 0
        self.close_calls = 0

    def partitions_for_topic(self, _topic: str) -> set[int]:
        return {0}

    def end_offsets(self, partitions: tuple[TopicPartition, ...]) -> dict[TopicPartition, int]:
        self.end_offset_calls += 1
        return {partitions[0]: 10}

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaLagReaderCacheTestCase(
            description="refresh derives lag from this topic's non-negative commits only",
            respond=lambda: (
                (("source.orders", 0, 95), ("source.orders", 1, -1), ("source.prices", 1, 3)),
                (0, 1),
                ((0, 100), (1, 0)),
            ),
            expected_snapshot=KafkaLagSnapshot(
                total_messages=5,
                partitions=(
                    KafkaPartitionLag(
                        partition=0, committed_offset=95, end_offset=100, lag_messages=5
                    ),
                    KafkaPartitionLag(
                        partition=1, committed_offset=None, end_offset=0, lag_messages=0
                    ),
                ),
            ),
            expected_collector_calls=(("sqltide-orders_analytics_eu", "source.orders"),),
        ),
        KafkaLagReaderCacheTestCase(
            description="failed refresh caches no snapshot and is not retried immediately",
            respond=broker_unavailable,
            expected_snapshot=None,
            expected_collector_calls=(("sqltide-orders_analytics_eu", "source.orders"),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_stale_cache_when_reading_lag_then_returns_cached_value_and_refreshes_in_background(
    test_case: KafkaLagReaderCacheTestCase,
) -> None:
    collector: ScriptedLagCollector = ScriptedLagCollector(respond=test_case.respond)
    reader: KafkaLagReader = KafkaLagReader(clock=lambda: 0.0, collector=collector)
    kafka: KafkaSettings = KafkaSettings(
        broker_list="broker:9092", topic="source.orders", consumer_group="sqltide-orders"
    )

    before_refresh: KafkaLagSnapshot | None = reader.read(kafka=kafka, database="analytics-eu")
    reader.close()
    after_refresh: KafkaLagSnapshot | None = reader.read(kafka=kafka, database="analytics-eu")

    assert before_refresh is None
    assert after_refresh == test_case.expected_snapshot
    assert collector.calls == test_case.expected_collector_calls


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaCredentialIsolationTestCase(
            description="separates SASL usernames",
            first_username="first",
            second_username="second",
            expected_isolated=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_different_credentials_when_keying_clients_then_keeps_connections_isolated(
    test_case: KafkaCredentialIsolationTestCase,
) -> None:
    first: KafkaSettings = KafkaSettings(
        broker_list="broker:9092",
        topic="source.orders",
        settings={"kafka_sasl_username": test_case.first_username},
    )
    second: KafkaSettings = KafkaSettings(
        broker_list="broker:9092",
        topic="source.orders",
        settings={"kafka_sasl_username": test_case.second_username},
    )

    assert (kafka_client_key(kafka=first) != kafka_client_key(kafka=second)) is (
        test_case.expected_isolated
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
