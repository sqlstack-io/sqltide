import pytest

import sqltide._native as _native
from sqltide.compiler.compile.models import DesiredState, ObjectKey
from tests.unit.src.sqltide.compiler.planner._test_types import (
    PlannerRebuildSubtreeTestCase,
    PlannerReplayAnchorSelectionTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_example_desired_state,
    build_key,
    build_single_transform_desired_state,
    key_parts,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerRebuildSubtreeTestCase(
            description="plans transform rebuild subtree from example desired state",
            root_key=(None, "table", "tbl__orders_enriched"),
            expected_descendant_keys=(
                (None, "table", "tbl__orders_enriched"),
                (None, "materialized_view", "mv__orders_enriched"),
            ),
            expected_upstream_boundary_key=(None, "table", "raw__orders"),
        ),
        PlannerRebuildSubtreeTestCase(
            description="plans landing rebuild subtree from example desired state",
            root_key=(None, "table", "raw__orders"),
            expected_descendant_keys=(
                (None, "table", "raw__orders"),
                (None, "materialized_view", "mv__orders"),
                (None, "table", "tbl__orders_enriched"),
                (None, "materialized_view", "mv__orders_enriched"),
            ),
            expected_upstream_boundary_key=(None, "table", "raw__orders"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_changed_desired_object_when_planning_rebuild_then_it_returns_expected_subtree(
    test_case: PlannerRebuildSubtreeTestCase,
) -> None:
    graph: _native.DesiredGraph = _native.DesiredGraph(build_example_desired_state())
    root_key: ObjectKey = build_key(*test_case.root_key)

    descendant_key_set: tuple[ObjectKey, ...] = graph.descendants(root_key)
    upstream_boundary_key: ObjectKey = graph.nearest_replay_anchor(root_key, False)

    assert tuple(key_parts(key) for key in descendant_key_set) == test_case.expected_descendant_keys
    assert key_parts(upstream_boundary_key) == test_case.expected_upstream_boundary_key


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerReplayAnchorSelectionTestCase(
            description="uses upstream source instead of replaying a transform table from itself",
            query=(
                "SELECT CAST(order_id AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                'CAST(_replay_offset AS UInt64) AS _replay_offset FROM __ref("orders")'
            ),
            expected_upstream_boundary_key=(None, "table", "raw__orders"),
        ),
        PlannerReplayAnchorSelectionTestCase(
            description="falls back upstream when transform table is not replay-anchor-eligible",
            query=(
                "SELECT CAST(customer_id AS UInt64) AS customer_id, "
                "CAST(count() AS UInt64) AS order_count, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders") GROUP BY customer_id, _replay_partition, _replay_offset'
            ),
            order_by=("customer_id",),
            expected_upstream_boundary_key=(None, "table", "raw__orders"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_transform_when_planning_rebuild_then_it_uses_expected_replay_anchor(
    test_case: PlannerReplayAnchorSelectionTestCase,
) -> None:
    desired_state: DesiredState = build_single_transform_desired_state(
        query=test_case.query,
        replay_lineage_mode=test_case.replay_lineage_mode,
        replay_anchor=test_case.replay_anchor,
        order_by=test_case.order_by,
    )
    root_key: ObjectKey = build_key(None, "table", "tbl__orders_enriched")

    upstream_boundary_key: ObjectKey = _native.DesiredGraph(desired_state).nearest_replay_anchor(
        root_key, False
    )

    assert key_parts(upstream_boundary_key) == test_case.expected_upstream_boundary_key
