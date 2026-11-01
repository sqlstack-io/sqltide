import pytest

from sqltide.adapter.models import (
    AdapterIdentity,
    CatalogIdentity,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import (
    Column,
    DesiredState,
    DesiredTable,
    ObjectKey,
    TableSpec,
    TableStorage,
)
from sqltide.compiler.discovery.types import BoundedReplayFallback, ReplayLineageMode
from sqltide.compiler.planner.constants import (
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
    REBUILD_STRATEGY_SHADOW,
)
from sqltide.compiler.planner.models import DeploymentPlan, RebuildSubtree
from sqltide.executor.backfill.main.resolve_unsupported_bounded_replay_behavior import (
    resolve_unsupported_bounded_replay_behavior,
)
from tests.unit.src.sqltide.executor.backfill._helpers._test_types import (
    ResolveUnsupportedBoundedReplayBehaviorTestCase,
)
from tests.unit.src.sqltide.executor.backfill._helpers.helpers import bound_live_table

_FORCED_START_TIME: str = "2026-04-09 15:00:00.000"
_ROOT_NAME: str = "tbl__hourly_order_volume"


@pytest.mark.parametrize(
    "test_case",
    [
        ResolveUnsupportedBoundedReplayBehaviorTestCase(
            description="live offset lineage keeps the seeded bounded rebuild",
            bounded_replay_fallback=BoundedReplayFallback.FULL,
            catalog_relations=bound_live_table("_replay_partition", "_replay_offset", "order_id"),
            expected_execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
            expected_requested_start_time=None,
            expected_history_supported=True,
            expected_resolved_fallback=None,
        ),
        ResolveUnsupportedBoundedReplayBehaviorTestCase(
            description="policy full resolves missing offset lineage to a full rebuild",
            bounded_replay_fallback=BoundedReplayFallback.FULL,
            catalog_relations=bound_live_table("_replay_partition", "order_id"),
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
            expected_requested_start_time=_FORCED_START_TIME,
            expected_history_supported=False,
            expected_resolved_fallback=BoundedReplayFallback.FULL,
        ),
        ResolveUnsupportedBoundedReplayBehaviorTestCase(
            description="policy window only resolves missing lineage to unseeded bounded",
            bounded_replay_fallback=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
            catalog_relations=bound_live_table("order_id"),
            expected_execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
            expected_requested_start_time=None,
            expected_history_supported=False,
            expected_resolved_fallback=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
        ),
        ResolveUnsupportedBoundedReplayBehaviorTestCase(
            description="a forced start without a live table replays the tail even under full",
            bounded_replay_fallback=BoundedReplayFallback.FULL,
            catalog_relations=(),
            expected_execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
            expected_requested_start_time=None,
            expected_history_supported=False,
            expected_resolved_fallback=BoundedReplayFallback.FULL,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_live_lineage_when_resolving_bounded_replay_then_it_applies_policy(
    test_case: ResolveUnsupportedBoundedReplayBehaviorTestCase,
) -> None:
    root_key: ObjectKey = ObjectKey(None, "table", _ROOT_NAME)
    plan: DeploymentPlan = resolve_unsupported_bounded_replay_behavior(
        catalog=CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=AdapterIdentity(name="clickhouse"), database="analytics"
            ),
            warehouse_timezone="UTC",
            relations=test_case.catalog_relations,
        ),
        deployment_plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=root_key,
                    affected_keys=(root_key,),
                    upstream_boundary_key=ObjectKey(None, "table", "tbl__orders"),
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                    forced_start_time=_FORCED_START_TIME,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
        ),
        desired_state=DesiredState(
            objects=(
                DesiredTable(
                    key=root_key,
                    deps=(),
                    spec=TableSpec(
                        columns=(Column(name="order_id", type="String"),),
                        storage=TableStorage(engine="MergeTree", order_by=("order_id",)),
                    ),
                    bounded_replay_fallback=test_case.bounded_replay_fallback,
                ),
            ),
            replay_anchor_keys=frozenset(),
            mutable_ref_warning_keys=frozenset(),
        ),
        default_database="analytics",
        replay_lineage_mode=ReplayLineageMode.OFFSETS,
    )

    subtree: RebuildSubtree = plan.rebuild_subtrees[0]
    assert subtree.execution_mode == test_case.expected_execution_mode
    assert subtree.requested_start_time == test_case.expected_requested_start_time
    assert subtree.history_preserving_bounded_supported is test_case.expected_history_supported
    assert subtree.resolved_bounded_replay_fallback == test_case.expected_resolved_fallback
