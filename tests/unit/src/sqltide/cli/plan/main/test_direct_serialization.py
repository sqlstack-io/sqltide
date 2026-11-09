import json

import pytest

from sqltide._native import render_direct_plan_json, render_direct_plan_text
from sqltide.adapter.models import AdapterReplayColumns
from sqltide.compiler.compile.models import LogicalResourceKey, ObjectKey
from sqltide.compiler.planner.models import (
    DirectPlan,
    DirectPlanEntry,
    DirectPrerequisite,
    DirectRelationOperation,
    DirectReplayRoot,
    PlannerWarning,
)
from tests.unit.src.sqltide.cli.plan.main._test_types import (
    CliDirectPlanRenderingTestCase,
    CliDirectPlanSerializationTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliDirectPlanSerializationTestCase(
            description="serializes every direct plan and nested identity field",
            expected_payload={
                "mode": "direct",
                "adapter": "clickhouse",
                "database": "analytics",
                "start_time": None,
                "selection_mode": "explicit",
                "user_scope": [{"resource_type": "model", "name": "orders_enriched"}],
                "execution_scope": [{"resource_type": "model", "name": "orders_enriched"}],
                "prerequisite_execution_scope": [],
                "prerequisite_scope": [
                    {
                        "key": {"resource_type": "source", "name": "orders"},
                        "relation_names": ["raw__orders"],
                        "present": True,
                        "framework_managed": True,
                    }
                ],
                "entries": [
                    {
                        "model_key": {
                            "resource_type": "model",
                            "name": "orders_enriched",
                        },
                        "reason": "selected",
                        "relation_names": ["tbl__orders_enriched", "mv__orders_enriched"],
                        "resource_kinds": ["table", "materialized_view"],
                        "driving_input_key": {"resource_type": "source", "name": "orders"},
                        "is_replay_root": True,
                        "sql_change": None,
                    }
                ],
                "replay_roots": [
                    {
                        "model_key": {
                            "resource_type": "model",
                            "name": "orders_enriched",
                        },
                        "driving_input_key": {
                            "resource_type": "source",
                            "name": "orders",
                        },
                        "driving_input_relation_name": "raw__orders",
                        "driving_input_replay_columns": {
                            "partition": "event_partition",
                            "offset": "event_offset",
                            "timestamp": "event_timestamp",
                            "landed_at": "landed_at",
                            "cursor": "event_cursor",
                        },
                        "replay_boundary_mode": "offsets",
                        "propagated_model_keys": [
                            {"resource_type": "model", "name": "orders_enriched"}
                        ],
                        "has_aggregate_semantics": False,
                        "settings": {"max_block_size": "32"},
                    }
                ],
                "teardown": [
                    {
                        "relation_name": "mv__orders_enriched",
                        "action": "drop",
                        "model_key": {
                            "resource_type": "model",
                            "name": "orders_enriched",
                        },
                        "resource_kind": "materialized_view",
                    }
                ],
                "creation": [
                    {
                        "relation_name": "tbl__orders_enriched",
                        "action": "create",
                        "model_key": {
                            "resource_type": "model",
                            "name": "orders_enriched",
                        },
                        "resource_kind": "table",
                    }
                ],
                "source_drift_selections": [],
                "source_recreations": [],
                "warnings": [
                    {
                        "warning_code": "replay_overlap",
                        "message": "Replay intentionally overlaps live propagation",
                        "root_key": {
                            "database": "analytics",
                            "object_type": "table",
                            "name": "raw__orders",
                        },
                        "target_key": {
                            "database": "analytics",
                            "object_type": "table",
                            "name": "tbl__orders_enriched",
                        },
                    }
                ],
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_plan_when_serializing_then_preserves_complete_nested_identity(
    test_case: CliDirectPlanSerializationTestCase,
) -> None:
    source_key: LogicalResourceKey = LogicalResourceKey("source", "orders")
    model_key: LogicalResourceKey = LogicalResourceKey("model", "orders_enriched")
    plan: DirectPlan = DirectPlan(
        database="analytics",
        user_scope=(model_key,),
        execution_scope=(model_key,),
        prerequisite_scope=(
            DirectPrerequisite(
                key=source_key,
                relation_names=("raw__orders",),
                present=True,
                framework_managed=True,
            ),
        ),
        entries=(
            DirectPlanEntry(
                model_key=model_key,
                reason="selected",
                relation_names=("tbl__orders_enriched", "mv__orders_enriched"),
                resource_kinds=("table", "materialized_view"),
                driving_input_key=source_key,
                is_replay_root=True,
            ),
        ),
        replay_roots=(
            DirectReplayRoot(
                model_key=model_key,
                driving_input_key=source_key,
                driving_input_relation_name="raw__orders",
                driving_input_replay_columns=AdapterReplayColumns(
                    partition="event_partition",
                    offset="event_offset",
                    timestamp="event_timestamp",
                    landed_at="landed_at",
                    cursor="event_cursor",
                ),
                replay_boundary_mode="offsets",
                propagated_model_keys=(model_key,),
                replay_settings=(("max_block_size", "32"),),
            ),
        ),
        teardown_operations=(
            DirectRelationOperation(
                relation_name="mv__orders_enriched",
                action="drop",
                model_key=model_key,
                resource_kind="materialized_view",
            ),
        ),
        creation_operations=(
            DirectRelationOperation(
                relation_name="tbl__orders_enriched",
                action="create",
                model_key=model_key,
                resource_kind="table",
            ),
        ),
        warnings=(
            PlannerWarning(
                warning_code="replay_overlap",
                message="Replay intentionally overlaps live propagation",
                root_key=ObjectKey("analytics", "table", "raw__orders"),
                target_key=ObjectKey("analytics", "table", "tbl__orders_enriched"),
            ),
        ),
    )

    assert json.loads(render_direct_plan_json(plan=plan, adapter_name="clickhouse")) == (
        test_case.expected_payload
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CliDirectPlanRenderingTestCase(
            description="renders replay propagation and standalone rebuilds as a compact forest",
            expected_fragments=(
                "Rebuild paths:",
                "[replay source] raw__orders [offsets]",
                "├── [replay root] order_cancellations",
                "└── [replay root] orders",
                "    └── [rebuild] enriched_orders",
                "[rebuild] region_lookup",
                "Model decisions:",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_replay_closure_when_rendering_then_rebuild_paths_form_a_forest(
    test_case: CliDirectPlanRenderingTestCase,
) -> None:
    source_key: LogicalResourceKey = LogicalResourceKey("source", "orders")
    cancellations_key: LogicalResourceKey = LogicalResourceKey("model", "order_cancellations")
    orders_key: LogicalResourceKey = LogicalResourceKey("model", "orders")
    enriched_key: LogicalResourceKey = LogicalResourceKey("model", "enriched_orders")
    region_key: LogicalResourceKey = LogicalResourceKey("model", "region_lookup")
    entries: tuple[DirectPlanEntry, ...] = (
        DirectPlanEntry(
            model_key=cancellations_key,
            reason="selected",
            relation_names=("tbl__order_cancellations",),
            resource_kinds=("table",),
            driving_input_key=source_key,
            is_replay_root=True,
        ),
        DirectPlanEntry(
            model_key=orders_key,
            reason="selected",
            relation_names=("tbl__orders",),
            resource_kinds=("table",),
            driving_input_key=source_key,
            is_replay_root=True,
        ),
        DirectPlanEntry(
            model_key=enriched_key,
            reason="selected",
            relation_names=("tbl__enriched_orders",),
            resource_kinds=("table",),
            driving_input_key=None,
            is_replay_root=False,
        ),
        DirectPlanEntry(
            model_key=region_key,
            reason="selected",
            relation_names=("tbl__region_lookup",),
            resource_kinds=("table",),
            driving_input_key=None,
            is_replay_root=False,
        ),
    )
    plan: DirectPlan = DirectPlan(
        database="analytics",
        user_scope=(orders_key,),
        execution_scope=(cancellations_key, orders_key, enriched_key, region_key),
        prerequisite_scope=(),
        entries=entries,
        replay_roots=(
            DirectReplayRoot(
                model_key=cancellations_key,
                driving_input_key=source_key,
                driving_input_relation_name="raw__orders",
                driving_input_replay_columns=AdapterReplayColumns(
                    partition="_replay_partition",
                    offset="_replay_offset",
                    timestamp="_replay_timestamp",
                    landed_at="_replay_landed_at",
                    cursor="_replay_cursor",
                ),
                replay_boundary_mode="offsets",
                propagated_model_keys=(cancellations_key,),
            ),
            DirectReplayRoot(
                model_key=orders_key,
                driving_input_key=source_key,
                driving_input_relation_name="raw__orders",
                driving_input_replay_columns=AdapterReplayColumns(
                    partition="_replay_partition",
                    offset="_replay_offset",
                    timestamp="_replay_timestamp",
                    landed_at="_replay_landed_at",
                    cursor="_replay_cursor",
                ),
                replay_boundary_mode="offsets",
                propagated_model_keys=(orders_key, enriched_key),
            ),
        ),
        teardown_operations=(),
        creation_operations=(),
    )

    rendered: str = render_direct_plan_text(plan=plan, adapter_name="clickhouse", use_color=False)

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
