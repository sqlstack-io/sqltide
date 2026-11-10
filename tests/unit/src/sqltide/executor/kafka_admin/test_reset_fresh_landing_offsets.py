from typing import cast

import pytest

from sqltide._native import reset_fresh_landing_offsets
from sqltide.executor.kafka_admin.models import ConsumerGroupOffsetReset
from sqltide.executor.population.models import PopulationSourcePreparation
from tests.unit.src.sqltide.executor.kafka_admin._test_types import (
    AdminClientConfigTestCase,
    FreshLandingOffsetResetTestCase,
    OffsetResetFailureTestCase,
)
from tests.unit.src.sqltide.executor.kafka_admin.helpers import (
    DeleteFailingClientFactory,
    FailingClientFactory,
    RecordingClientFactory,
    build_failure_reset,
    build_source_preparation,
)

_EFFECTIVE_CONSUMER_GROUP: str = "sqltide_orders_orders_analytics"
_SUCCESS_NOTICE: str = (
    "Reset committed offsets for consumer group "
    f"'{_EFFECTIVE_CONSUMER_GROUP}' ahead of creating raw__orders; ingestion starts from the "
    "earliest retained messages."
)


@pytest.mark.parametrize(
    "test_case",
    [
        FreshLandingOffsetResetTestCase(
            description="fresh landing deletes the database-scoped consumer group",
            created_relation_names=("kafka__orders", "raw__orders", "mv__orders"),
            delete_results=((_EFFECTIVE_CONSUMER_GROUP, "no_error"),),
            expected_resets=(
                ConsumerGroupOffsetReset(
                    consumer_group=_EFFECTIVE_CONSUMER_GROUP,
                    landing_relation_name="raw__orders",
                    deleted=True,
                    error=None,
                    notice=_SUCCESS_NOTICE,
                ),
            ),
            expected_deleted_group_calls=((_EFFECTIVE_CONSUMER_GROUP,),),
            expected_closed_states=(True,),
        ),
        FreshLandingOffsetResetTestCase(
            description="preserved landing leaves committed offsets unchanged",
            created_relation_names=("kafka__orders", "mv__orders"),
            delete_results=((_EFFECTIVE_CONSUMER_GROUP, "no_error"),),
            expected_resets=(),
            expected_deleted_group_calls=(),
            expected_closed_states=(),
        ),
        FreshLandingOffsetResetTestCase(
            description="fresh landing with preserved Kafka table checks the existing group",
            created_relation_names=("raw__orders",),
            delete_results=((_EFFECTIVE_CONSUMER_GROUP, "group_id_not_found"),),
            expected_resets=(
                ConsumerGroupOffsetReset(
                    consumer_group=_EFFECTIVE_CONSUMER_GROUP,
                    landing_relation_name="raw__orders",
                    deleted=False,
                    error=None,
                    notice=None,
                ),
            ),
            expected_deleted_group_calls=((_EFFECTIVE_CONSUMER_GROUP,),),
            expected_closed_states=(True,),
        ),
        FreshLandingOffsetResetTestCase(
            description="active consumer group reports broker rejection",
            created_relation_names=("raw__orders",),
            delete_results=((_EFFECTIVE_CONSUMER_GROUP, "group_not_empty"),),
            expected_resets=(
                build_failure_reset(
                    consumer_group=_EFFECTIVE_CONSUMER_GROUP,
                    error="NON_EMPTY_GROUP",
                ),
            ),
            expected_deleted_group_calls=((_EFFECTIVE_CONSUMER_GROUP,),),
            expected_closed_states=(True,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_source_plan_when_resetting_fresh_landings_then_only_orphaned_offsets_are_deleted(
    test_case: FreshLandingOffsetResetTestCase,
) -> None:
    source_preparation: PopulationSourcePreparation = build_source_preparation(
        created_relation_names=test_case.created_relation_names
    )
    client_factory: RecordingClientFactory = RecordingClientFactory(
        delete_results=test_case.delete_results
    )

    resets: tuple[ConsumerGroupOffsetReset, ...] = reset_fresh_landing_offsets(
        source_preparation=source_preparation,
        client_factory=client_factory,
    )

    assert resets == test_case.expected_resets
    assert tuple(tuple(client.deleted_group_ids) for client in client_factory.clients) == (
        test_case.expected_deleted_group_calls
    )
    assert tuple(client.closed for client in client_factory.clients) == (
        test_case.expected_closed_states
    )


@pytest.mark.parametrize(
    "test_case",
    [
        OffsetResetFailureTestCase(
            description="unreachable broker reports an actionable best-effort warning",
            error="broker unavailable",
            expected_reset=build_failure_reset(
                consumer_group=_EFFECTIVE_CONSUMER_GROUP,
                error="broker unavailable",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unreachable_broker_when_resetting_then_failure_is_returned_without_raising(
    test_case: OffsetResetFailureTestCase,
) -> None:
    source_preparation: PopulationSourcePreparation = build_source_preparation(
        created_relation_names=("raw__orders",)
    )
    client_factory: FailingClientFactory = FailingClientFactory(error=test_case.error)

    resets: tuple[ConsumerGroupOffsetReset, ...] = reset_fresh_landing_offsets(
        source_preparation=source_preparation,
        client_factory=client_factory,
    )

    assert resets == (test_case.expected_reset,)


@pytest.mark.parametrize(
    "test_case",
    [
        OffsetResetFailureTestCase(
            description="delete timeout reports an actionable best-effort warning",
            error="request timed out",
            expected_reset=build_failure_reset(
                consumer_group=_EFFECTIVE_CONSUMER_GROUP,
                error="request timed out",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_delete_failure_when_resetting_then_client_closes_and_failure_is_returned(
    test_case: OffsetResetFailureTestCase,
) -> None:
    source_preparation: PopulationSourcePreparation = build_source_preparation(
        created_relation_names=("raw__orders",)
    )
    client_factory: DeleteFailingClientFactory = DeleteFailingClientFactory(error=test_case.error)

    resets: tuple[ConsumerGroupOffsetReset, ...] = reset_fresh_landing_offsets(
        source_preparation=source_preparation,
        client_factory=client_factory,
    )

    assert resets == (test_case.expected_reset,)
    assert client_factory.client.closed is True


@pytest.mark.parametrize(
    "test_case",
    [
        AdminClientConfigTestCase(
            description="multiple plaintext brokers are normalized",
            broker_list="kafka-1:9092, kafka-2:9092",
            settings=(),
            expected_bootstrap_servers=("kafka-1:9092", "kafka-2:9092"),
            expected_config_items=(),
        ),
        AdminClientConfigTestCase(
            description="ClickHouse SASL settings are translated for kafka-python",
            broker_list="secure-kafka:9093",
            settings=(
                ("kafka_security_protocol", "sasl_ssl"),
                ("kafka_sasl_mechanism", "SCRAM-SHA-512"),
                ("kafka_sasl_username", "sqltide"),
                ("kafka_sasl_password", "secret"),
                ("kafka_num_consumers", "2"),
            ),
            expected_bootstrap_servers=("secure-kafka:9093",),
            expected_config_items=(
                ("security_protocol", "SASL_SSL"),
                ("sasl_mechanism", "SCRAM-SHA-512"),
                ("sasl_plain_username", "sqltide"),
                ("sasl_plain_password", "secret"),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_source_kafka_settings_when_resetting_then_admin_client_uses_matching_credentials(
    test_case: AdminClientConfigTestCase,
) -> None:
    source_preparation: PopulationSourcePreparation = build_source_preparation(
        created_relation_names=("raw__orders",),
        broker_list=test_case.broker_list,
        settings=test_case.settings,
    )
    client_factory: RecordingClientFactory = RecordingClientFactory(
        delete_results=((_EFFECTIVE_CONSUMER_GROUP, "no_error"),)
    )

    _ = reset_fresh_landing_offsets(
        source_preparation=source_preparation,
        client_factory=client_factory,
    )

    config: dict[str, object] = client_factory.clients[0].config
    assert tuple(cast(list[str], config["bootstrap_servers"])) == (
        test_case.expected_bootstrap_servers
    )
    for name, expected_value in test_case.expected_config_items:
        assert config[name] == expected_value
    assert "kafka_num_consumers" not in config


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
