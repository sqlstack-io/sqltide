"""Retained kafka-python-ng clients for topic inventory reads."""

import logging
import threading
from collections.abc import Callable, Mapping

from kafka import KafkaAdminClient

from sqltide.dev_server.exceptions import KafkaCollectorClosedError
from sqltide.dev_server.types import KafkaClientKey

logger: logging.Logger = logging.getLogger(__name__)


class KafkaTopicCollector:
    """Retain kafka-python-ng admin clients shared by broker configuration."""

    def __init__(
        self,
        *,
        admin_factory: Callable[..., KafkaAdminClient] = KafkaAdminClient,
    ) -> None:
        self._admin_factory = admin_factory
        self._lock = threading.Lock()
        self._admins: dict[KafkaClientKey, KafkaAdminClient] = {}
        self._client_locks: dict[KafkaClientKey, threading.Lock] = {}
        self._reads: dict[KafkaClientKey, int] = {}
        self._opened_clients = 0
        self._closed_clients = 0
        self._closed = False

    def __call__(self, *, key: KafkaClientKey, config: Mapping[str, object]) -> object:
        """Return raw describe_topics metadata read on the retained admin client."""

        admin, client_lock = self._acquire_client(key=key, config=config)
        try:
            metadata: object = admin.describe_topics()
            self._reads[key] += 1
            logger.warning(
                "Kafka topic client %s brokers=%s reads=%d opened=%d closed=%d active=%d",
                "opened" if self._reads[key] == 1 else "reused",
                key[0],
                self._reads[key],
                self._opened_clients,
                self._closed_clients,
                self._opened_clients - self._closed_clients,
            )
            return metadata
        finally:
            client_lock.release()

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
                    self._admins[key].close()
                finally:
                    self._closed_clients += 1
                    logger.warning(
                        "Kafka topic client closed reads=%d opened=%d closed=%d active=%d",
                        self._reads[key],
                        self._opened_clients,
                        self._closed_clients,
                        self._opened_clients - self._closed_clients,
                    )

    def _acquire_client(
        self, *, key: KafkaClientKey, config: Mapping[str, object]
    ) -> tuple[KafkaAdminClient, threading.Lock]:
        with self._lock:
            if self._closed:
                raise KafkaCollectorClosedError("Kafka topic collector is closed")
            if key not in self._admins:
                self._admins[key] = self._admin_factory(**config)
                self._client_locks[key] = threading.Lock()
                self._reads[key] = 0
                self._opened_clients += 1
            client_lock: threading.Lock = self._client_locks[key]
            client_lock.acquire()
            return self._admins[key], client_lock
