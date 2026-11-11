"""Behavior tests for the authored model step specifications."""

import copy
import operator
import re
from dataclasses import fields

import pytest

from sqltide.compiler.discovery.exceptions import ProjectSpecError
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    KafkaSettings,
    Pipeline,
    RawConnectionConfig,
    TransformStep,
    ViewStep,
)
from tests.unit.src.sqltide.compiler.discovery._test_types import (
    KafkaSettingsReprTestCase,
    ModelStepErrorTestCase,
    ReplaceTestCase,
    ShallowCopyTestCase,
)

QUERY_OR_FILE_MESSAGE: str = "Exactly one of 'query' or 'sql_file' must be provided"
OBJECT_REPR_PATTERN: str = (
    r"<sqltide\.compiler\.discovery\.models\.KafkaSettings object at 0x[0-9a-f]+>"
)
TRANSFORM_ARGUMENTS: dict[str, object] = {
    "name": "orders",
    "source": "raw_orders",
    "engine": "MergeTree",
}


@pytest.mark.parametrize(
    "test_case",
    [
        ModelStepErrorTestCase(
            description="a transform needs a query or a SQL file",
            step_class=TransformStep,
            arguments={**TRANSFORM_ARGUMENTS, "order_by": ("order_id",)},
            expected_message=QUERY_OR_FILE_MESSAGE,
        ),
        ModelStepErrorTestCase(
            description="a transform rejects both a query and a SQL file",
            step_class=TransformStep,
            arguments={
                **TRANSFORM_ARGUMENTS,
                "order_by": ("order_id",),
                "query": "SELECT 1",
                "sql_file": "orders.sql",
            },
            expected_message=QUERY_OR_FILE_MESSAGE,
        ),
        ModelStepErrorTestCase(
            description="a transform needs at least one ORDER BY expression",
            step_class=TransformStep,
            arguments={**TRANSFORM_ARGUMENTS, "order_by": [], "query": "SELECT 1"},
            expected_message="TransformStep must declare at least one ORDER BY expression",
        ),
        ModelStepErrorTestCase(
            description="the query check runs before the ORDER BY check",
            step_class=TransformStep,
            arguments={**TRANSFORM_ARGUMENTS, "order_by": (), "sql_file": ""},
            expected_message=QUERY_OR_FILE_MESSAGE,
        ),
        ModelStepErrorTestCase(
            description="a view needs a query or a SQL file",
            step_class=ViewStep,
            arguments={"name": "orders_view", "query": ""},
            expected_message=QUERY_OR_FILE_MESSAGE,
        ),
        ModelStepErrorTestCase(
            description="a view rejects both a query and a SQL file",
            step_class=ViewStep,
            arguments={"name": "orders_view", "query": "SELECT 1", "sql_file": "view.sql"},
            expected_message=QUERY_OR_FILE_MESSAGE,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_model_step_when_constructing_then_project_spec_error_is_raised(
    test_case: ModelStepErrorTestCase,
) -> None:
    with pytest.raises(ProjectSpecError) as error_info:
        _ = test_case.step_class(**test_case.arguments)

    assert error_info.value.args == (test_case.expected_message,)


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaSettingsReprTestCase(
            description="landing Kafka settings keep the object repr and hide every field",
            kafka_settings=KafkaSettings(
                broker_list="broker-1.orders.example.com:9092",
                topic="orders_topic",
                consumer_group="orders_consumer_group",
                format="JSONEachRow",
                ttl="orders_ttl_expression",
                settings={"kafka_sasl_password": "orders-secret-value"},
                retention_origin="project",
            ),
            expected_repr_pattern=OBJECT_REPR_PATTERN,
            expected_hidden_values=(
                "broker-1.orders.example.com",
                "orders_topic",
                "orders_consumer_group",
                "JSONEachRow",
                "orders_ttl_expression",
                "kafka_sasl_password",
                "orders-secret-value",
                "KafkaRetentionOrigin",
                "broker_list=",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_kafka_settings_when_rendering_repr_then_no_field_value_is_shown(
    test_case: KafkaSettingsReprTestCase,
) -> None:
    rendered: str = repr(test_case.kafka_settings)

    assert re.fullmatch(test_case.expected_repr_pattern, rendered) is not None
    assert not any(value in rendered for value in test_case.expected_hidden_values)


@pytest.mark.parametrize(
    "test_case",
    [
        ShallowCopyTestCase(
            description="connection values keep their frozen configuration pairs",
            instance=RawConnectionConfig(
                values=(
                    ("host", "warehouse.example.com"),
                    ("settings", {"max_threads": [1, 2]}),
                )
            ),
            expected_type=RawConnectionConfig,
        ),
        ShallowCopyTestCase(
            description="a transform keeps its settings mapping and ORDER BY tuple",
            instance=TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=["order_id"],
                query="SELECT 1 AS order_id",
                settings={"max_threads": "4"},
            ),
            expected_type=TransformStep,
        ),
        ShallowCopyTestCase(
            description="a pipeline keeps its transforms tuple",
            instance=Pipeline(
                name="orders",
                source=KafkaLandingStep(
                    name="orders",
                    kafka=KafkaSettings(broker_list="kafka:9092", topic="source.orders"),
                ),
                transforms=[
                    TransformStep(
                        name="orders_enriched",
                        source="orders",
                        engine="MergeTree()",
                        order_by=("order_id",),
                        query="SELECT 1 AS order_id",
                    )
                ],
            ),
            expected_type=Pipeline,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_authored_model_when_shallow_copying_then_the_copy_shares_every_field(
    test_case: ShallowCopyTestCase,
) -> None:
    copied: object = copy.copy(test_case.instance)

    assert type(copied) is test_case.expected_type
    assert copied == test_case.instance
    assert all(
        getattr(copied, field.name) is getattr(test_case.instance, field.name)
        for field in fields(test_case.instance)
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ReplaceTestCase(
            description="a transform coerces its replaced ORDER BY and settings again",
            instance=TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=("order_id",),
                query="SELECT 1 AS order_id",
            ),
            changes={"order_by": ["customer_id"], "settings": {"max_threads": "8"}},
            expected=TransformStep(
                name="orders_enriched",
                source="orders",
                engine="MergeTree()",
                order_by=("customer_id",),
                query="SELECT 1 AS order_id",
                settings={"max_threads": "8"},
            ),
        ),
        ReplaceTestCase(
            description="connection values are replaced as frozen configuration pairs",
            instance=RawConnectionConfig(values=(("host", "warehouse.example.com"),)),
            changes={"values": (("host", "replica.example.com"), ("port", 9000))},
            expected=RawConnectionConfig(values=(("host", "replica.example.com"), ("port", 9000))),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_authored_model_when_replacing_through_copy_protocol_then_fields_are_rebuilt(
    test_case: ReplaceTestCase,
) -> None:
    replaced: object = operator.methodcaller("__replace__", **test_case.changes)(test_case.instance)

    assert type(replaced) is type(test_case.expected)
    assert replaced == test_case.expected
