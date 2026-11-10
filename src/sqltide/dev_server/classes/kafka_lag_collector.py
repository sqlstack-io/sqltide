"""Retained kafka-python-ng clients for consumer-group offset reads."""

import logging
import threading
from collections.abc import Callable, Mapping

from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
from kafka.structs import OffsetAndMetadata

from sqltide.dev_server.constants import (
    KAFKA_REUSE_LOG_FIRST_READ_COUNT,
    KAFKA_REUSE_LOG_INTERVAL,
)
from sqltide.dev_server.exceptions import KafkaCollectorClosedError
from sqltide.dev_server.types import KafkaClientKey, KafkaLagOffsets

logger: logging.Logger = logging.getLogger(__name__)


class KafkaLagCollector:
    """Retain kafka-python-ng clients shared by topics on the same brokers and credentials."""

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

    def __call__(
        self,
        *,
        key: KafkaClientKey,
        config: Mapping[str, object],
        topic: str,
        consumer_group: str,
    ) -> KafkaLagOffsets:
        """Read committed offsets, partitions and end offsets on the retained clients."""

        admin, consumer, client_lock = self._acquire_clients(key=key, config=config)
        try:
            raw_committed: Mapping[TopicPartition, OffsetAndMetadata] = (
                admin.list_consumer_group_offsets(consumer_group)
            )
            partitions: tuple[int, ...] = tuple(sorted(consumer.partitions_for_topic(topic) or ()))
            raw_ends: Mapping[TopicPartition, int] = consumer.end_offsets(
                tuple(TopicPartition(topic, partition) for partition in partitions)
            )
            self._reads[key] += 1
            reads: int = self._reads[key]
            if reads == KAFKA_REUSE_LOG_FIRST_READ_COUNT or reads % KAFKA_REUSE_LOG_INTERVAL == 0:
                logger.warning(
                    "Kafka lag clients reused brokers=%s reads=%d opened_pairs=%d "
                    "closed_pairs=%d active_pairs=%d",
                    key[0],
                    reads,
                    self._opened_client_pairs,
                    self._closed_client_pairs,
                    self._opened_client_pairs - self._closed_client_pairs,
                )
        finally:
            client_lock.release()
        return (
            tuple(
                (topic_partition.topic, topic_partition.partition, metadata.offset)
                for topic_partition, metadata in raw_committed.items()
            ),
            partitions,
            tuple(
                (topic_partition.partition, offset) for topic_partition, offset in raw_ends.items()
            ),
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
        self, *, key: KafkaClientKey, config: Mapping[str, object]
    ) -> tuple[KafkaAdminClient, KafkaConsumer, threading.Lock]:
        with self._lock:
            if self._closed:
                raise KafkaCollectorClosedError("Kafka lag collector is closed")
            if key not in self._admins:
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
                    key[0],
                    self._opened_client_pairs,
                    self._closed_client_pairs,
                    self._opened_client_pairs - self._closed_client_pairs,
                )
            client_lock: threading.Lock = self._client_locks[key]
            client_lock.acquire()
            return self._admins[key], self._consumers[key], client_lock
