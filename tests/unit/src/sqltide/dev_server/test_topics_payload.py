from pathlib import Path
from typing import cast

import pytest

from sqltide.adapter.models import AdapterQueryResult
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server._helpers.payloads.state_payload import (
    build_relation_stats_query,
    build_topics_payload,
)
from sqltide.dev_server.models import (
    KafkaLagSnapshot,
    KafkaPartitionLag,
    KafkaTopicInfo,
    KafkaTopicsSnapshot,
)
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeTestClient
from tests.unit.src.sqltide.dev_server._test_types import (
    TopicsColdCachePayloadTestCase,
    TopicsMergedPayloadTestCase,
    TopicsRouteTestCase,
    TopicsUnavailableTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    FakeKafkaLagReader,
    FakeKafkaTopicReader,
    build_compile_callable,
    build_fake_state_connection,
    build_message_test_client,
    write_adopted_dev_server_project,
    write_dev_server_project,
)

_BROKER_SNAPSHOT: KafkaTopicsSnapshot = KafkaTopicsSnapshot(
    topics=(
        KafkaTopicInfo(
            name="__consumer_offsets", partition_count=1, replication_factor=1, internal=True
        ),
        KafkaTopicInfo(
            name="source.orders", partition_count=3, replication_factor=2, internal=False
        ),
        KafkaTopicInfo(
            name="unmanaged.topic", partition_count=6, replication_factor=2, internal=False
        ),
    )
)
_LAG_SNAPSHOT: KafkaLagSnapshot = KafkaLagSnapshot(
    total_messages=42,
    partitions=(
        KafkaPartitionLag(partition=0, committed_offset=8, end_offset=50, lag_messages=42),
    ),
)


@pytest.mark.parametrize(
    "test_case",
    [
        TopicsMergedPayloadTestCase(
            description="merges broker inventory with managed lag and retained stats",
            expected_topic_names=frozenset(
                {"__consumer_offsets", "source.orders", "unmanaged.topic"}
            ),
            expected_managed_sources=({"name": "orders", "relationName": "raw__orders"},),
            expected_lag_messages=42,
            expected_retained_rows=1000,
            expected_retained_bytes=4096,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_broker_and_managed_state_when_merging_then_topics_carry_both(
    test_case: TopicsMergedPayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()

    payload: dict[str, object] = build_topics_payload(
        analysis=analysis,
        connection=build_fake_state_connection(),
        database="analytics",
        topic_reader=FakeKafkaTopicReader(snapshot=_BROKER_SNAPSHOT),
        kafka_lag_reader=FakeKafkaLagReader(snapshot=_LAG_SNAPSHOT),
    )

    topics: dict[str, dict[str, object]] = {
        str(topic["name"]): topic for topic in cast("list[dict[str, object]]", payload["topics"])
    }
    assert payload["available"] is True
    assert payload["pendingBrokers"] == []
    assert frozenset(topics) == test_case.expected_topic_names
    managed: dict[str, object] = topics["source.orders"]
    assert managed["partitions"] == 3
    assert managed["replicationFactor"] == 2
    assert managed["sources"] == list(test_case.expected_managed_sources)
    assert managed["lagMessages"] == test_case.expected_lag_messages
    assert managed["retainedRows"] == test_case.expected_retained_rows
    assert managed["retainedBytes"] == test_case.expected_retained_bytes
    assert topics["unmanaged.topic"]["sources"] == []
    assert topics["unmanaged.topic"]["retainedRows"] is None
    assert topics["__consumer_offsets"]["internal"] is True


@pytest.mark.parametrize(
    "test_case",
    [
        TopicsColdCachePayloadTestCase(
            description="managed topics still appear while broker caches warm",
            expected_pending_brokers=("kafka:9092",),
            expected_topic_names=("source.orders",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cold_broker_cache_when_merging_then_managed_topics_still_appear(
    test_case: TopicsColdCachePayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()

    payload: dict[str, object] = build_topics_payload(
        analysis=analysis,
        connection=None,
        database=None,
        topic_reader=FakeKafkaTopicReader(snapshot=None),
        kafka_lag_reader=FakeKafkaLagReader(snapshot=None),
    )

    topics: list[dict[str, object]] = cast("list[dict[str, object]]", payload["topics"])
    assert payload["available"] is True
    assert payload["pendingBrokers"] == list(test_case.expected_pending_brokers)
    assert tuple(str(topic["name"]) for topic in topics) == test_case.expected_topic_names
    assert topics[0]["partitions"] is None
    assert topics[0]["sources"] == [{"name": "orders", "relationName": "raw__orders"}]


@pytest.mark.parametrize(
    "test_case",
    [
        TopicsUnavailableTestCase(
            description="states the missing broker credentials for adopted-only projects",
            expected_reason_fragment="no managed Kafka sources",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_only_adopted_sources_when_merging_then_states_the_missing_credentials(
    test_case: TopicsUnavailableTestCase,
    tmp_path: Path,
) -> None:
    write_adopted_dev_server_project(project_dir=tmp_path)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()

    payload: dict[str, object] = build_topics_payload(
        analysis=analysis,
        connection=None,
        database=None,
        topic_reader=FakeKafkaTopicReader(snapshot=None),
        kafka_lag_reader=FakeKafkaLagReader(snapshot=None),
    )

    assert payload["available"] is False
    assert test_case.expected_reason_fragment in str(payload["reason"])
    assert payload["topics"] == []


@pytest.mark.parametrize(
    "test_case",
    [
        TopicsRouteTestCase(
            description="serves the managed inventory through the topics route",
            expected_topic_name="source.orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_topics_route_when_reading_then_serves_managed_inventory(
    test_case: TopicsRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            build_relation_stats_query(database="analytics"): AdapterQueryResult(
                rows=(("raw__orders", 1000, 4096),),
                column_names=("name", "total_rows", "total_bytes"),
            )
        },
    )

    payload: dict = client.get("/api/topics").json()

    topic_names: list[str] = [topic["name"] for topic in payload["topics"]]
    assert payload["available"] is True
    assert test_case.expected_topic_name in topic_names
