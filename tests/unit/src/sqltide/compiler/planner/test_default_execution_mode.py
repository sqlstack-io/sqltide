import pytest

import sqltide._native as _native
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.discovery.models import (
    ReplayOnChangePolicy,
    ReplayOnChangeRule,
)
from sqltide.compiler.discovery.types import ReplayOnChangeMode
from sqltide.compiler.planner.constants import (
    PLANNED_CHANGE_TYPE_REBUILD,
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
)
from sqltide.compiler.planner.models import PlannedObjectChange, RebuildSubtree
from tests.unit.src.sqltide.compiler.planner._test_types import PlannerExecutionModeTestCase
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_example_desired_state_with_replay_on_change,
    build_key,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerExecutionModeTestCase(
            description="defaults non-breaking seedable table changes to seeded bounded rebuild",
            schema_change_kind="non_breaking",
            seed_compatibility="seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
        ),
        PlannerExecutionModeTestCase(
            description="defaults breaking seedable table changes to full rebuild",
            schema_change_kind="breaking",
            seed_compatibility="seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        PlannerExecutionModeTestCase(
            description="defaults non-seedable table changes to full rebuild",
            schema_change_kind="breaking",
            seed_compatibility="non_seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        PlannerExecutionModeTestCase(
            description="defaults unclassified changes to full rebuild",
            schema_change_kind=None,
            seed_compatibility=None,
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        ),
        PlannerExecutionModeTestCase(
            description="uses configured full policy for non-breaking changes",
            schema_change_kind="non_breaking",
            seed_compatibility="seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
            configured_backfill_mode="full",
            replay_on_change=ReplayOnChangePolicy(
                non_breaking=ReplayOnChangeRule(
                    mode=ReplayOnChangeMode.FULL, lookback_seconds=None
                ),
                breaking=None,
            ),
        ),
        PlannerExecutionModeTestCase(
            description=(
                "uses configured bounded policy with seeded execution for seedable breaking changes"
            ),
            schema_change_kind="breaking",
            seed_compatibility="seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
            configured_backfill_mode="bounded",
            configured_lookback_seconds=1800,
            replay_on_change=ReplayOnChangePolicy(
                non_breaking=None,
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.BOUNDED, lookback_seconds=1800),
            ),
        ),
        PlannerExecutionModeTestCase(
            description=(
                "uses configured bounded policy with unseeded execution for non-seedable "
                "breaking changes"
            ),
            schema_change_kind="breaking",
            seed_compatibility="non_seedable",
            expected_execution_mode=REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED,
            configured_backfill_mode="bounded",
            configured_lookback_seconds=1800,
            replay_on_change=ReplayOnChangePolicy(
                non_breaking=None,
                breaking=ReplayOnChangeRule(mode=ReplayOnChangeMode.BOUNDED, lookback_seconds=1800),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_planned_change_metadata_when_building_subtrees_then_it_sets_expected_execution_mode(
    test_case: PlannerExecutionModeTestCase,
) -> None:
    desired_state: DesiredState = build_example_desired_state_with_replay_on_change(
        replay_on_change=test_case.replay_on_change
    )
    rebuild_subtrees: tuple[RebuildSubtree, ...] = _native.DesiredGraph(
        desired_state
    ).rebuild_subtrees(
        (
            PlannedObjectChange(
                key=build_key(None, "table", "tbl__orders_enriched"),
                change_type=PLANNED_CHANGE_TYPE_REBUILD,
                schema_change_kind=test_case.schema_change_kind,
                seed_compatibility=test_case.seed_compatibility,
            ),
        )
    )

    assert rebuild_subtrees[0].execution_mode == test_case.expected_execution_mode
    assert rebuild_subtrees[0].configured_backfill_mode == test_case.configured_backfill_mode
    assert rebuild_subtrees[0].execution_lookback_seconds == test_case.configured_lookback_seconds
