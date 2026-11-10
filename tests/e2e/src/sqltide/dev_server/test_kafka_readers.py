import uuid
from contextlib import ExitStack

import pytest
from kafka import KafkaConsumer, TopicPartition
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.structs import OffsetAndMetadata

from sqltide._native import (
    build_kafka_client_config,
    build_kafka_topics_snapshot,
    database_scoped_consumer_group,
    kafka_client_key,
)
from sqltide.compiler.discovery.models import KafkaSettings
from sqltide.dev_server.classes.kafka_lag_collector import KafkaLagCollector
from sqltide.dev_server.classes.kafka_topic_collector import KafkaTopicCollector
from sqltide.dev_server.models import KafkaTopicsSnapshot
from sqltide.dev_server.types import KafkaLagOffsets
from tests.e2e.src.sqltide.conftest import E2EKafkaConnectionSettings
from tests.e2e.src.sqltide.dev_server._test_types import KafkaReaderReuseE2ETestCase


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaReaderReuseE2ETestCase(
            description="retained clients remain usable",
            consumer_group="sqltide-reader-test",
            expected_committed_offset=0,
            expected_end_offset=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_broker_when_reading_repeatedly_then_retained_clients_remain_usable(
    test_case: KafkaReaderReuseE2ETestCase,
    e2e_kafka_connection_settings: E2EKafkaConnectionSettings,
) -> None:
    bootstrap_server: str = e2e_kafka_connection_settings.bootstrap_server
    topic: str = f"sqltide-reader-{uuid.uuid4().hex}"
    with ExitStack() as cleanup:
        admin: KafkaAdminClient = KafkaAdminClient(bootstrap_servers=bootstrap_server)
        cleanup.callback(admin.close)
        admin.create_topics(
            new_topics=[NewTopic(name=topic, num_partitions=1, replication_factor=1)]
        )
        cleanup.callback(admin.delete_topics, topics=[topic])
        partition: TopicPartition = TopicPartition(topic, 0)
        consumer: KafkaConsumer = KafkaConsumer(
            bootstrap_servers=bootstrap_server,
            group_id=f"{test_case.consumer_group}_default",
            enable_auto_commit=False,
        )
        try:
            consumer.assign([partition])
            consumer.commit(offsets={partition: OffsetAndMetadata(0, "")})
        finally:
            consumer.close()
        kafka: KafkaSettings = KafkaSettings(
            broker_list=bootstrap_server,
            topic=topic,
            consumer_group=test_case.consumer_group,
        )
        topic_collector: KafkaTopicCollector = KafkaTopicCollector()
        cleanup.callback(topic_collector.close)
        lag_collector: KafkaLagCollector = KafkaLagCollector()
        cleanup.callback(lag_collector.close)
        key: tuple[str, tuple[tuple[str, str], ...]] = kafka_client_key(kafka=kafka)
        config: dict[str, object] = build_kafka_client_config(kafka=kafka)
        consumer_group: str = database_scoped_consumer_group(
            consumer_group=test_case.consumer_group, database="default"
        )

        first_topics: KafkaTopicsSnapshot = build_kafka_topics_snapshot(
            metadata=topic_collector(key=key, config=config)
        )
        second_topics: KafkaTopicsSnapshot = build_kafka_topics_snapshot(
            metadata=topic_collector(key=key, config=config)
        )
        first_offsets: KafkaLagOffsets = lag_collector(
            key=key, config=config, topic=topic, consumer_group=consumer_group
        )
        second_offsets: KafkaLagOffsets = lag_collector(
            key=key, config=config, topic=topic, consumer_group=consumer_group
        )

        assert topic in {item.name for item in first_topics.topics}
        assert first_topics == second_topics
        assert first_offsets == (
            ((topic, 0, test_case.expected_committed_offset),),
            (0,),
            ((0, test_case.expected_end_offset),),
        )
        assert first_offsets == second_offsets


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
