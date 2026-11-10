"""Retained Kafka clients for consumer-group lag reads."""

import logging
import threading
from collections.abc import Callable, Mapping

from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
from kafka.structs import OffsetAndMetadata

from sqltide._native import database_scoped_consumer_group
from sqltide.compiler.discovery.models import KafkaSettings
from sqltide.dev_server._helpers.broker.client import (
    build_kafka_client_config,
    kafka_client_key,
)
from sqltide.dev_server._helpers.broker.snapshots import build_kafka_lag_snapshot
from sqltide.dev_server.constants import (
    KAFKA_REUSE_LOG_FIRST_READ_COUNT,
    KAFKA_REUSE_LOG_INTERVAL,
)
from sqltide.dev_server.exceptions import KafkaCollectorClosedError
from sqltide.dev_server.models import KafkaLagSnapshot
from sqltide.dev_server.types import KafkaClientKey

logger: logging.Logger = logging.getLogger(__name__)


class KafkaLagCollector:
    """Collect lag through retained clients shared by topics on the same brokers."""

    def __init__(
        self,
        *,
        admin_factory: Callable[..., KafkaAdminClient] = KafkaAdminClient,
        consumer_factory: Callable[..., KafkaConsumer] = KafkaConsumer,
    ) -> None:
        self._admin_factory = admin_factory
        self._consumer_factory = consumer_factory
        self._lock = threading.Lock()
        self._admins: dict[KafkaClientKey, KafkaAdminClient] = {}
        self._consumers: dict[KafkaClientKey, KafkaConsumer] = {}
        self._client_locks: dict[KafkaClientKey, threading.Lock] = {}
        self._reads: dict[KafkaClientKey, int] = {}
        self._opened_client_pairs = 0
        self._closed_client_pairs = 0
        self._closed = False

    def __call__(self, *, kafka: KafkaSettings, database: str) -> KafkaLagSnapshot:
        """Read one topic's lag while keeping its broker connections open."""

        if kafka.consumer_group is None:
            return KafkaLagSnapshot(total_messages=None, partitions=())
        key, admin, consumer, client_lock = self._acquire_clients(kafka=kafka)
        consumer_group: str = database_scoped_consumer_group(
            consumer_group=kafka.consumer_group,
            database=database,
        )
        try:
            raw_committed: Mapping[TopicPartition, OffsetAndMetadata] = (
                admin.list_consumer_group_offsets(consumer_group)
            )
            discovered_partitions: set[int] | None = consumer.partitions_for_topic(kafka.topic)
            topic_partitions: frozenset[int] = frozenset(discovered_partitions or ())
            partition_objects: tuple[TopicPartition, ...] = tuple(
                TopicPartition(kafka.topic, partition) for partition in sorted(topic_partitions)
            )
            raw_ends: Mapping[TopicPartition, int] = consumer.end_offsets(partition_objects)
            self._reads[key] += 1
            reads: int = self._reads[key]
            if reads == KAFKA_REUSE_LOG_FIRST_READ_COUNT or reads % KAFKA_REUSE_LOG_INTERVAL == 0:
                logger.warning(
                    "Kafka lag clients reused brokers=%s reads=%d opened_pairs=%d "
                    "closed_pairs=%d active_pairs=%d",
                    kafka.broker_list,
                    reads,
                    self._opened_client_pairs,
                    self._closed_client_pairs,
                    self._opened_client_pairs - self._closed_client_pairs,
                )
        finally:
            client_lock.release()
        committed_offsets: dict[int, int] = {
            topic_partition.partition: int(metadata.offset)
            for topic_partition, metadata in raw_committed.items()
            if topic_partition.topic == kafka.topic
            and metadata.offset is not None
            and int(metadata.offset) >= 0
        }
        end_offsets: dict[int, int] = {
            topic_partition.partition: int(offset) for topic_partition, offset in raw_ends.items()
        }
        return build_kafka_lag_snapshot(
            partition_ids=topic_partitions,
            committed_offsets=committed_offsets,
            end_offsets=end_offsets,
        )

    def close(self) -> None:
        """Close all retained broker connections."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            keys: tuple[KafkaClientKey, ...] = tuple(self._admins)
        for key in keys:
            with self._client_locks[key]:
                try:
                    try:
                        self._consumers[key].close()
                    finally:
                        self._admins[key].close()
                finally:
                    self._closed_client_pairs += 1
                    logger.warning(
                        "Kafka lag clients closed reads=%d opened_pairs=%d closed_pairs=%d "
                        "active_pairs=%d",
                        self._reads[key],
                        self._opened_client_pairs,
                        self._closed_client_pairs,
                        self._opened_client_pairs - self._closed_client_pairs,
                    )

    def _acquire_clients(
        self, *, kafka: KafkaSettings
    ) -> tuple[KafkaClientKey, KafkaAdminClient, KafkaConsumer, threading.Lock]:
        key: KafkaClientKey = kafka_client_key(kafka=kafka)
        with self._lock:
            if self._closed:
                raise KafkaCollectorClosedError("Kafka lag collector is closed")
            if key not in self._admins:
                config: dict[str, object] = build_kafka_client_config(kafka=kafka)
                admin: KafkaAdminClient = self._admin_factory(**config)
                try:
                    consumer: KafkaConsumer = self._consumer_factory(
                        enable_auto_commit=False, **config
                    )
                except Exception:
                    admin.close()
                    raise
                self._admins[key] = admin
                self._consumers[key] = consumer
                self._client_locks[key] = threading.Lock()
                self._reads[key] = 0
                self._opened_client_pairs += 1
                logger.warning(
                    "Kafka lag clients opened brokers=%s opened_pairs=%d closed_pairs=%d "
                    "active_pairs=%d",
                    kafka.broker_list,
                    self._opened_client_pairs,
                    self._closed_client_pairs,
                    self._opened_client_pairs - self._closed_client_pairs,
                )
            client_lock: threading.Lock = self._client_locks[key]
            client_lock.acquire()
            return key, self._admins[key], self._consumers[key], client_lock
