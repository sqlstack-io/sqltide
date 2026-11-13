from typing import cast

import pytest

import sqltide._native as _native
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualState,
    ActualTable,
    ActualView,
    PlanningWarehouseSnapshot,
)
from tests.unit.src.sqltide.compiler.planner._test_types import ActualStateProjectionTestCase
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_projection_characterization_inputs,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ActualStateProjectionTestCase(
            description="preserves synthesized landing specs and unloaded transform options",
            expected_ordered_names=(
                "kafka__orders",
                "mv__orders_landing",
                "raw__orders",
                "tbl__orders_enriched",
            ),
            expected_kafka_columns=(("message", "String", None),),
            expected_kafka_broker_list="kafka:9092",
            expected_kafka_topic="source.orders.created",
            expected_kafka_consumer_group="sqltide_orders_orders",
            expected_kafka_format="JSONAsString",
            expected_kafka_settings={"kafka_num_consumers": "4"},
            expected_raw_columns=(("order_id", "String", None),),
            expected_raw_engine="MergeTree()",
            expected_raw_order_by=("order_id",),
            expected_raw_partition_by="toYYYYMM(created_at)",
            expected_raw_ttl="created_at + INTERVAL 30 DAY",
            expected_raw_settings={"index_granularity": "8192"},
            expected_landing_mv_source="kafka__orders",
            expected_landing_mv_target="raw__orders",
            expected_landing_mv_query="SELECT message AS order_id FROM kafka__orders",
            expected_transform_columns=(("order_id", "String", None),),
            expected_transform_engine="MergeTree()",
            expected_transform_order_by=("order_id",),
            expected_transform_partition_by=None,
            expected_transform_ttl="created_at + INTERVAL 30 DAY",
            expected_transform_settings=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_names_and_transform_spec_when_projecting_then_preserves_current_shortcuts(
    test_case: ActualStateProjectionTestCase,
) -> None:
    desired_state: DesiredState
    snapshot: PlanningWarehouseSnapshot
    desired_state, snapshot = build_projection_characterization_inputs()
    actual_state: ActualState = _native.load_actual_state_from_snapshot(
        snapshot=snapshot, desired_state=desired_state, database="analytics"
    )
    actual_objects: tuple[
        ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView, ...
    ] = actual_state.objects
    actual_by_name: dict[
        str, ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView
    ] = {actual_object.name: actual_object for actual_object in actual_objects}
    assert (
        tuple(actual_object.name for actual_object in actual_objects)
        == test_case.expected_ordered_names
    )
    kafka_table: ActualKafkaTable = cast(ActualKafkaTable, actual_by_name["kafka__orders"])
    raw_table: ActualTable = cast(ActualTable, actual_by_name["raw__orders"])
    landing_mv: ActualMaterializedView = cast(
        ActualMaterializedView,
        actual_by_name["mv__orders_landing"],
    )
    transform_table: ActualTable = cast(ActualTable, actual_by_name["tbl__orders_enriched"])

    assert (
        tuple((column.name, column.type, column.default) for column in kafka_table.columns)
        == test_case.expected_kafka_columns
    )
    assert kafka_table.kafka.broker_list == test_case.expected_kafka_broker_list
    assert kafka_table.kafka.topic == test_case.expected_kafka_topic
    assert kafka_table.kafka.consumer_group == test_case.expected_kafka_consumer_group
    assert kafka_table.kafka.format == test_case.expected_kafka_format
    assert kafka_table.kafka.settings == test_case.expected_kafka_settings
    assert (
        tuple((column.name, column.type, column.default) for column in raw_table.columns)
        == test_case.expected_raw_columns
    )
    assert raw_table.engine == test_case.expected_raw_engine
    assert raw_table.order_by == test_case.expected_raw_order_by
    assert raw_table.partition_by == test_case.expected_raw_partition_by
    assert raw_table.ttl == test_case.expected_raw_ttl
    assert raw_table.settings == test_case.expected_raw_settings
    assert landing_mv.source_table_name == test_case.expected_landing_mv_source
    assert landing_mv.target_table_name == test_case.expected_landing_mv_target
    assert landing_mv.query == test_case.expected_landing_mv_query
    assert (
        tuple((column.name, column.type, column.default) for column in transform_table.columns)
        == test_case.expected_transform_columns
    )
    assert transform_table.engine == test_case.expected_transform_engine
    assert transform_table.order_by == test_case.expected_transform_order_by
    assert transform_table.partition_by == test_case.expected_transform_partition_by
    assert transform_table.ttl == test_case.expected_transform_ttl
    assert transform_table.settings == test_case.expected_transform_settings
