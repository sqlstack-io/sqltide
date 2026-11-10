import json

import pytest

from sqltide._native import (
    render_direct_build_json,
    render_direct_build_text,
)
from sqltide.cli.build.main.render_virtual_build_result import render_virtual_build_result
from sqltide.cli.plan.main.render_plan_result import render_plan_result
from sqltide.cli.promotion.main.render_promotion_result import render_promotion_result
from sqltide.cli.readiness.main.render_ambiguous_deployment_message import (
    render_ambiguous_deployment_message,
)
from sqltide.cli.readiness.main.render_deployment_audit_result import (
    render_deployment_audit_result,
)
from sqltide.compiler.compile.models import (
    Column,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
)
from sqltide.compiler.discovery.models import (
    ReplayOnChangePolicy,
    ReplayOnChangeRule,
)
from sqltide.compiler.planner.constants import (
    DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE,
    DEPLOYMENT_PHASE_PLAN,
    REBUILD_EXECUTION_MODE_FULL,
    REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
    REBUILD_STRATEGY_SHADOW,
)
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    DeploymentStep,
    PlannedObjectChange,
    PlannedSqlDiff,
    PlannerWarning,
    PreparedShadowObject,
    RebuildSubtree,
)
from sqltide.executor.auditing.models import SqlAuditRunResult
from sqltide.executor.backfill.models import (
    BackfillBootstrapResult,
    BackfillExecutionResult,
    BackfillRootReplayResult,
    RootBackfillReport,
)
from sqltide.executor.direct.models import DirectBuildResult, DirectRootReplayResult
from sqltide.executor.promotion.models import PublishedView, PublishResult
from sqltide.executor.readiness.models import (
    AuditDeploymentCandidate,
    DeploymentAuditResult,
    OffsetCatchupSummary,
    RootAuditResult,
    ScalarCatchupSummary,
)
from tests.integration.src.sqltide.executor.backfill.helpers import (
    build_scalar_replay_compiled_pipeline,
)
from tests.unit.src.sqltide.cli._test_types import (
    CliPlanRenderingBaselineTestCase,
    CliPublishAtomicityRenderingTestCase,
    CliRenderingTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import realize_compiled_pipelines


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanRenderingBaselineTestCase(
            description="renders the complete virtual-environment plan output contracts",
            expected_payload={
                "mode": "virtual environments",
                "adapter": "clickhouse",
                "deployment_id": None,
                "object_changes": [],
                "steps": [
                    {
                        "step_id": "step-1",
                        "phase": "plan",
                        "action": "plan_shadow_table",
                        "root_key": {
                            "database": None,
                            "object_type": "table",
                            "name": "raw__orders",
                        },
                        "target_key": {
                            "database": None,
                            "object_type": "table",
                            "name": "tbl__orders_enriched",
                        },
                        "physical_name": None,
                    }
                ],
                "rebuild_subtrees": [
                    {
                        "root_key": {
                            "database": None,
                            "object_type": "table",
                            "name": "raw__orders",
                        },
                        "upstream_boundary_key": {
                            "database": None,
                            "object_type": "table",
                            "name": "raw__orders",
                        },
                        "affected_keys": [
                            {
                                "database": None,
                                "object_type": "table",
                                "name": "tbl__orders_enriched",
                            }
                        ],
                        "strategy": "shadow_rebuild",
                        "execution_mode": "seeded_bounded_rebuild",
                        "forced_full_refresh": False,
                        "forced_start_time": None,
                        "requested_start_time": None,
                        "configured_backfill_mode": "bounded",
                        "execution_lookback_seconds": 3600,
                        "history_preserving_bounded_supported": True,
                        "resolved_bounded_replay_fallback": "full",
                        "replay_required": True,
                    }
                ],
                "prepared_shadow_objects": [],
                "warnings": [
                    {
                        "warning_code": "mutable_reference",
                        "root_key": {
                            "database": None,
                            "object_type": "table",
                            "name": "raw__orders",
                        },
                        "target_key": None,
                        "message": "mutable side reference is read at execution time",
                    }
                ],
                "sql_diffs": [
                    {
                        "key": {
                            "database": None,
                            "object_type": "materialized_view",
                            "name": "mv__orders_enriched",
                        },
                        "object_type": "materialized_view",
                        "name": "mv__orders_enriched",
                        "diff_lines": ["- old query", "+ new query"],
                    }
                ],
            },
            expected_compact_text=(
                "Plan Ready\n"
                "Database: analytics\n"
                "Subtrees to rebuild: 1\n"
                "Planned steps: 1\n"
                "\n"
                "Subtrees:\n"
                "Subtree 1\n"
                "[replay start] raw__orders\n"
                "└── [live target] tbl__orders_enriched\n"
                "\n"
                "Diffs:\n"
                "- mv__orders_enriched\n"
                "Run `sqt plan --verbose` to show full diffs\n"
                "\n"
                "Warnings:\n"
                "- raw__orders: mutable side reference is read at execution time"
            ),
            expected_verbose_text=(
                "Plan Ready\n"
                "Database: analytics\n"
                "Subtrees to rebuild: 1\n"
                "Planned steps: 1\n"
                "\n"
                "Subtrees:\n"
                "Subtree 1\n"
                "[replay start] raw__orders\n"
                "└── [live target] tbl__orders_enriched\n"
                "\n"
                "SQL diffs:\n"
                "\n"
                "Materialized_View: mv__orders_enriched\n"
                "- old query\n"
                "+ new query\n"
                "\n"
                "Staged rollout objects:\n"
                "- replay source: raw__orders\n"
                "- live target: tbl__orders_enriched\n"
                "\n"
                "Workflow:\n"
                "- prepare staged objects for subtree rooted at tbl__orders_enriched\n"
                "- backfill from raw__orders\n"
                "- audit staged tbl__orders_enriched\n"
                "- publish tbl__orders_enriched\n"
                "\n"
                "Warnings:\n"
                "- raw__orders: mutable side reference is read at execution time"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_environment_plan_when_rendering_then_preserves_complete_outputs(
    test_case: CliPlanRenderingBaselineTestCase,
) -> None:
    raw_key: ObjectKey = ObjectKey(None, "table", "raw__orders")
    target_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    plan: DeploymentPlan = DeploymentPlan(
        deployment_id=None,
        object_changes=(),
        rebuild_subtrees=(
            RebuildSubtree(
                root_key=raw_key,
                affected_keys=(target_key,),
                upstream_boundary_key=raw_key,
                strategy="shadow_rebuild",
                execution_mode="seeded_bounded_rebuild",
                configured_backfill_mode="bounded",
                execution_lookback_seconds=3600,
                resolved_bounded_replay_fallback="full",
            ),
        ),
        steps=(
            DeploymentStep(
                step_id="step-1",
                phase="plan",
                action="plan_shadow_table",
                root_key=raw_key,
                target_key=target_key,
            ),
        ),
        prepared_shadow_objects=(),
        warnings=(
            PlannerWarning(
                warning_code="mutable_reference",
                message="mutable side reference is read at execution time",
                root_key=raw_key,
            ),
        ),
        sql_diffs=(
            PlannedSqlDiff(
                key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                object_type="materialized_view",
                name="mv__orders_enriched",
                diff_lines=("- old query", "+ new query"),
            ),
        ),
    )
    desired_state: DesiredState = DesiredState(
        objects=(),
        replay_anchor_keys=frozenset(),
        mutable_ref_warning_keys=frozenset(),
    )
    rendered_json: str = render_plan_result(
        plan=plan,
        desired_state=desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=True,
    )
    rendered_compact: str = render_plan_result(
        plan=plan,
        desired_state=desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )
    rendered_verbose: str = render_plan_result(
        plan=plan,
        desired_state=desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
        verbose=True,
    )

    assert json.loads(rendered_json) == test_case.expected_payload
    assert rendered_compact == test_case.expected_compact_text
    assert rendered_verbose == test_case.expected_verbose_text


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders human-readable plan summary",
            expected_fragments=(
                "Plan Ready",
                "Database: analytics",
                "Subtrees to rebuild: 1",
                "Subtree 1",
                "[replay start] raw__orders",
                "└── [live target] tbl__orders_enriched",
                "Warnings",
                "- none",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_plan_result_when_rendering_text_then_it_returns_operator_summary(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=ObjectKey(None, "table", "raw__orders"),
                    affected_keys=(ObjectKey(None, "table", "tbl__orders_enriched"),),
                    upstream_boundary_key=ObjectKey(None, "table", "raw__orders"),
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                ),
            ),
            steps=(
                DeploymentStep(
                    step_id="step-1",
                    phase=DEPLOYMENT_PHASE_PLAN,
                    action=DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE,
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    target_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                ),
            ),
            prepared_shadow_objects=(
                PreparedShadowObject(
                    logical_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    physical_name="tbl__orders_enriched__20260410T000000Z_ab12cd",
                    logical_model_name="orders_enriched",
                ),
            ),
            warnings=(),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="deduplicates changed targets across overlapping subtrees",
            expected_fragments=(
                "Subtrees to rebuild: 2",
                "Changes detected:",
                "tbl__orders_enriched",
                "- transform query changed",
                "- table schema changed",
                "- replay on change: bounded-7d",
                "- plan: bounded replay with history will keep older active rows "
                "and replay the last 7d",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_overlapping_subtrees_when_rendering_compact_plan_then_it_deduplicates_targets(
    test_case: CliRenderingTestCase,
) -> None:
    raw_orders_a_key: ObjectKey = ObjectKey(None, "table", "raw__orders_a")
    raw_orders_b_key: ObjectKey = ObjectKey(None, "table", "raw__orders_b")
    orders_enriched_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    mv_from_a_key: ObjectKey = ObjectKey(None, "materialized_view", "mv__from_a")
    mv_from_b_key: ObjectKey = ObjectKey(None, "materialized_view", "mv__from_b")

    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(key=mv_from_a_key, change_type="replace"),
                PlannedObjectChange(key=mv_from_b_key, change_type="replace"),
                PlannedObjectChange(
                    key=orders_enriched_key,
                    change_type="replace",
                    schema_change_kind="non_breaking",
                    seed_compatibility="seedable",
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=raw_orders_a_key,
                    affected_keys=(raw_orders_a_key, mv_from_a_key, orders_enriched_key),
                    upstream_boundary_key=raw_orders_a_key,
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                    configured_backfill_mode="bounded",
                    execution_lookback_seconds=7 * 24 * 60 * 60,
                ),
                RebuildSubtree(
                    root_key=raw_orders_b_key,
                    affected_keys=(raw_orders_b_key, mv_from_b_key, orders_enriched_key),
                    upstream_boundary_key=raw_orders_b_key,
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                    configured_backfill_mode="bounded",
                    execution_lookback_seconds=7 * 24 * 60 * 60,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
        ),
        desired_state=DesiredState(
            objects=(
                DesiredTable(
                    key=raw_orders_a_key,
                    deps=(),
                    spec=TableSpec(
                        columns=(Column("id", "UInt64"),),
                        storage=TableStorage(engine="MergeTree", order_by=("id",)),
                    ),
                ),
                DesiredTable(
                    key=raw_orders_b_key,
                    deps=(),
                    spec=TableSpec(
                        columns=(Column("id", "UInt64"),),
                        storage=TableStorage(engine="MergeTree", order_by=("id",)),
                    ),
                ),
                DesiredTable(
                    key=orders_enriched_key,
                    deps=(raw_orders_a_key, raw_orders_b_key),
                    spec=TableSpec(
                        columns=(Column("id", "UInt64"), Column("amount", "UInt64")),
                        storage=TableStorage(engine="MergeTree", order_by=("id",)),
                    ),
                    replay_on_change=ReplayOnChangePolicy(
                        breaking=ReplayOnChangeRule(mode="full"),
                        non_breaking=ReplayOnChangeRule(
                            mode="bounded",
                            lookback_seconds=7 * 24 * 60 * 60,
                        ),
                    ),
                ),
                DesiredMaterializedView(
                    key=mv_from_a_key,
                    deps=(raw_orders_a_key,),
                    spec=MaterializedViewSpec(
                        source_table_name="raw__orders_a",
                        target_table_name="tbl__orders_enriched",
                        query="SELECT id, amount FROM raw__orders_a",
                    ),
                ),
                DesiredMaterializedView(
                    key=mv_from_b_key,
                    deps=(raw_orders_b_key,),
                    spec=MaterializedViewSpec(
                        source_table_name="raw__orders_b",
                        target_table_name="tbl__orders_enriched",
                        query="SELECT id, amount FROM raw__orders_b",
                    ),
                ),
            ),
            replay_anchor_keys=frozenset({raw_orders_a_key, raw_orders_b_key}),
            mutable_ref_warning_keys=frozenset(),
        ),
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
    assert rendered.count("tbl__orders_enriched\n- transform query changed") == 1


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders audit caution summary without publish recommendation",
            expected_fragments=(
                "Audit: caution",
                "state: greenfield",
                "assessment: caution",
                "replay source: raw__orders",
                "replay source rows: 0",
                "staged rows: 0",
                "active rows: n/a",
                "investigate audit findings before publish, especially for tbl__orders_enriched",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_non_ready_audit_result_when_rendering_text_then_it_avoids_publish_recommendation(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_deployment_audit_result(
        result=DeploymentAuditResult(
            deployment_id="20260410T000000Z_cd34ef",
            deployment_status="backfilling",
            assessment="caution",
            replay_lineage_mode="offsets",
            warning_codes=(),
            root_results=(
                RootAuditResult(
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    staged_physical_name="tbl__orders_enriched__20260410T000000Z_cd34ef",
                    state="greenfield",
                    replay_source_name="raw__orders",
                    replay_source_row_count=0,
                    staged_exists=True,
                    active_exists=False,
                    active_row_count=None,
                    staged_row_count=0,
                    row_delta=None,
                    row_ratio=None,
                    assessment="caution",
                    replay_lineage_mode="offsets",
                    offset_catchup_summary=None,
                    scalar_catchup_summary=None,
                    warnings=("replay source raw__orders is empty",),
                ),
            ),
        ),
        database="analytics",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
    assert "sqt deployment promote" not in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders audit row ratio and time ranges for active caution root",
            expected_fragments=(
                "state: active_view_present",
                "row delta: -699534",
                "row ratio: 7.6%",
                "staged range: 2026-04-14 16:52:39.592 .. 2026-04-15 03:07:27.587",
                "active range: 2026-04-10 16:01:42.514 .. 2026-04-15 03:07:27.587",
                "lag seconds: 0",
                "warning: staged row count is far below active row count for tbl__flight_positions",
                "investigate audit findings before publish, especially for tbl__flight_positions",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_caution_audit_result_when_rendering_text_then_it_shows_relevant_context(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_deployment_audit_result(
        result=DeploymentAuditResult(
            deployment_id="20260414T190649Z_36b35f",
            deployment_status="backfilling",
            assessment="caution",
            replay_lineage_mode="offsets",
            warning_codes=(),
            root_results=(
                RootAuditResult(
                    root_key=ObjectKey(None, "table", "tbl__flight_positions"),
                    staged_physical_name="tbl__flight_positions__20260414T190649Z_36b35f",
                    staged_exists=True,
                    active_exists=True,
                    active_row_count=757203,
                    staged_row_count=57669,
                    row_delta=-699534,
                    row_ratio=57669 / 757203,
                    assessment="caution",
                    replay_lineage_mode="offsets",
                    offset_catchup_summary=OffsetCatchupSummary(
                        active_partition_count=8,
                        staged_partition_count=8,
                        partitions_compared=8,
                        missing_staged_partition_count=0,
                        missing_freshness_partition_count=0,
                        lagging_partition_count=0,
                        max_offset_gap=0,
                        average_offset_gap=0.0,
                        lag_boundary_column="_replay_landed_at",
                        max_lag_seconds=0.0,
                        average_lag_seconds=0.0,
                    ),
                    scalar_catchup_summary=ScalarCatchupSummary(
                        active_min_value="2026-04-10 16:01:42.514",
                        active_max_value="2026-04-15 03:07:27.587",
                        staged_min_value="2026-04-14 16:52:39.592",
                        staged_max_value="2026-04-15 03:07:27.587",
                        lag_seconds=0.0,
                    ),
                    state="active_view_present",
                    replay_source_name="raw__flight_states",
                    replay_source_row_count=57669,
                    warnings=(
                        "staged row count is far below active row count for tbl__flight_positions",
                    ),
                ),
            ),
        ),
        database="flights_demo",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
    assert "sqt deployment promote" not in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders multiple live targets under one replay start",
            expected_fragments=(
                "[replay start] raw__orders",
                "├── [live target] tbl__orders_enriched",
                "└── [live target] tbl__orders_rollup",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_plan_with_multiple_live_targets_when_rendering_text_then_it_renders_branches(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=ObjectKey(None, "table", "raw__orders"),
                    affected_keys=(
                        ObjectKey(None, "table", "tbl__orders_enriched"),
                        ObjectKey(None, "table", "tbl__orders_rollup"),
                    ),
                    upstream_boundary_key=ObjectKey(None, "table", "raw__orders"),
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders changed transform query against live target table",
            expected_fragments=(
                "[replay start] raw__orders",
                "└── [live target] tbl__orders_enriched",
                "Changes detected",
                "tbl__orders_enriched",
                "- transform query changed",
                "Diffs",
                "- tbl__orders_enriched",
                "Run `sqt plan --verbose` to show full diffs",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mv_root_change_when_rendering_plan_then_it_maps_to_live_target_table(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(
                    key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    change_type="rebuild",
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    affected_keys=(ObjectKey(None, "materialized_view", "mv__orders_enriched"),),
                    upstream_boundary_key=ObjectKey(None, "table", "raw__orders"),
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
            sql_diffs=(
                PlannedSqlDiff(
                    key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    object_type="transform",
                    name="mv__orders_enriched",
                    diff_lines=(
                        "--- current",
                        "+++ desired",
                        "@@ -1,3 +1,3 @@",
                        "-SELECT order_id FROM analytics.raw__orders",
                        "+SELECT order_id, customer_id FROM analytics.raw__orders",
                    ),
                ),
            ),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders schema change details grouped under changed target",
            expected_fragments=(
                "Changes detected",
                "tbl__orders_enriched",
                "- table schema changed",
                "- replay on change: bounded-7d",
                "- plan: bounded replay with history will keep older active rows "
                "and replay the last 7d",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_schema_change_plan_when_rendering_text_then_it_explains_policy_per_diff(
    test_case: CliRenderingTestCase,
) -> None:
    raw_orders_key: ObjectKey = ObjectKey(None, "table", "raw__orders")
    tbl_orders_enriched_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    mv_orders_enriched_key: ObjectKey = ObjectKey(None, "materialized_view", "mv__orders_enriched")
    desired_state: DesiredState = DesiredState(
        objects=(
            DesiredTable(
                key=raw_orders_key,
                deps=(),
                spec=TableSpec(
                    columns=(
                        Column("order_id", "UInt64"),
                        Column("_replay_landed_at", "DateTime64(3)"),
                    ),
                    storage=TableStorage(engine="MergeTree", order_by=("order_id",)),
                ),
            ),
            DesiredTable(
                key=tbl_orders_enriched_key,
                deps=(raw_orders_key,),
                spec=TableSpec(
                    columns=(
                        Column("order_id", "UInt64"),
                        Column("order_total", "Decimal(18, 2)"),
                        Column("region", "LowCardinality(String)"),
                    ),
                    storage=TableStorage(
                        engine="ReplacingMergeTree",
                        order_by=("order_id",),
                    ),
                ),
                replay_on_change=ReplayOnChangePolicy(
                    breaking=ReplayOnChangeRule(mode="full"),
                    non_breaking=ReplayOnChangeRule(
                        mode="bounded",
                        lookback_seconds=7 * 24 * 60 * 60,
                    ),
                ),
            ),
            DesiredMaterializedView(
                key=mv_orders_enriched_key,
                deps=(raw_orders_key,),
                spec=MaterializedViewSpec(
                    source_table_name="raw__orders",
                    target_table_name="tbl__orders_enriched",
                    query="SELECT order_id, region FROM raw__orders",
                ),
            ),
        ),
        replay_anchor_keys=frozenset({raw_orders_key}),
        mutable_ref_warning_keys=frozenset(),
    )

    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(
                    key=mv_orders_enriched_key,
                    change_type="replace",
                ),
                PlannedObjectChange(
                    key=tbl_orders_enriched_key,
                    change_type="replace",
                    schema_change_kind="non_breaking",
                    seed_compatibility="seedable",
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=raw_orders_key,
                    affected_keys=(mv_orders_enriched_key, tbl_orders_enriched_key),
                    upstream_boundary_key=raw_orders_key,
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                    configured_backfill_mode="bounded",
                    execution_lookback_seconds=7 * 24 * 60 * 60,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
            sql_diffs=(
                PlannedSqlDiff(
                    key=mv_orders_enriched_key,
                    object_type="transform",
                    name="mv__orders_enriched",
                    diff_lines=("--- current", "+++ desired"),
                ),
                PlannedSqlDiff(
                    key=tbl_orders_enriched_key,
                    object_type="table",
                    name="tbl__orders_enriched",
                    diff_lines=("--- current", "+++ desired"),
                ),
            ),
        ),
        desired_state=desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders full refresh request as an operator action",
            expected_fragments=(
                "Changes detected",
                "tbl__orders_enriched",
                "- full refresh requested",
                "- plan: full refresh requested; replay all history for this subtree",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_forced_full_refresh_when_rendering_plan_then_it_explains_operator_intent(
    test_case: CliRenderingTestCase,
) -> None:
    raw_orders_key: ObjectKey = ObjectKey(None, "table", "raw__orders")
    tbl_orders_enriched_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    desired_state: DesiredState = DesiredState(
        objects=(
            DesiredTable(
                key=raw_orders_key,
                deps=(),
                spec=TableSpec(
                    columns=(Column(name="order_id", type="UInt64"),),
                    storage=TableStorage(engine="MergeTree()", order_by=("order_id",)),
                ),
            ),
            DesiredTable(
                key=tbl_orders_enriched_key,
                deps=(raw_orders_key,),
                spec=TableSpec(
                    columns=(Column(name="order_id", type="UInt64"),),
                    storage=TableStorage(engine="MergeTree()", order_by=("order_id",)),
                ),
            ),
        ),
        replay_anchor_keys=frozenset({raw_orders_key}),
        mutable_ref_warning_keys=frozenset(),
    )
    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(
                    key=tbl_orders_enriched_key,
                    change_type="rebuild",
                    force_full_refresh=True,
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=tbl_orders_enriched_key,
                    affected_keys=(tbl_orders_enriched_key,),
                    upstream_boundary_key=raw_orders_key,
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_FULL,
                    forced_full_refresh=True,
                    configured_backfill_mode="full",
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
        ),
        desired_state=desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
    assert "Schema change: non_breaking" not in rendered
    assert "Seed compatibility: seedable" not in rendered
    assert "Planned rebuild: seeded_bounded_rebuild" not in rendered
    assert "Transform: mv__orders_enriched" not in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description=(
                "renders new targets separately from schema-changed targets in compact mode"
            ),
            expected_fragments=(
                "New targets",
                "- tbl__orders_rollup",
                "Changes detected",
                "tbl__orders_enriched",
                "- table schema changed",
                "- replay on change: bounded-7d",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_subtree_when_rendering_compact_plan_then_it_separates_new_targets(
    test_case: CliRenderingTestCase,
) -> None:
    raw_orders_key: ObjectKey = ObjectKey(None, "table", "raw__orders")
    tbl_orders_enriched_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_enriched")
    tbl_orders_rollup_key: ObjectKey = ObjectKey(None, "table", "tbl__orders_rollup")

    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(
                    key=tbl_orders_enriched_key,
                    change_type="replace",
                    schema_change_kind="non_breaking",
                    seed_compatibility="seedable",
                ),
                PlannedObjectChange(
                    key=tbl_orders_rollup_key,
                    change_type="create",
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=raw_orders_key,
                    affected_keys=(tbl_orders_enriched_key, tbl_orders_rollup_key),
                    upstream_boundary_key=raw_orders_key,
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                    configured_backfill_mode="bounded",
                    execution_lookback_seconds=7 * 24 * 60 * 60,
                ),
            ),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
            sql_diffs=(),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders verbose plan details when requested",
            expected_fragments=(
                "Changed objects",
                "SQL diffs",
                "Transform: mv__orders_enriched",
                "--- current",
                "Staged rollout objects",
                "Workflow",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_verbose_plan_when_rendering_text_then_it_shows_expanded_details(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(
                PlannedObjectChange(
                    key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    change_type="rebuild",
                ),
            ),
            rebuild_subtrees=(
                RebuildSubtree(
                    root_key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    affected_keys=(ObjectKey(None, "materialized_view", "mv__orders_enriched"),),
                    upstream_boundary_key=ObjectKey(None, "table", "raw__orders"),
                    strategy=REBUILD_STRATEGY_SHADOW,
                    execution_mode=REBUILD_EXECUTION_MODE_SEEDED_BOUNDED,
                ),
            ),
            steps=(
                DeploymentStep(
                    step_id="step-1",
                    phase=DEPLOYMENT_PHASE_PLAN,
                    action=DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE,
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    target_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                ),
            ),
            prepared_shadow_objects=(),
            warnings=(),
            sql_diffs=(
                PlannedSqlDiff(
                    key=ObjectKey(None, "materialized_view", "mv__orders_enriched"),
                    object_type="transform",
                    name="mv__orders_enriched",
                    diff_lines=(
                        "--- current",
                        "+++ desired",
                        "@@ -1,3 +1,3 @@",
                        "-SELECT order_id FROM analytics.raw__orders",
                        "+SELECT order_id, customer_id FROM analytics.raw__orders",
                    ),
                ),
            ),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
        verbose=True,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders ansi color when forced",
            expected_fragments=(
                "\u001b[1m\u001b[34mPlan Ready\u001b[0m",
                "\u001b[2m--- current\u001b[0m",
                "\u001b[2m+++ desired\u001b[0m",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_forced_color_when_rendering_plan_then_it_includes_ansi_styles(
    test_case: CliRenderingTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rendered: str = render_plan_result(
        plan=DeploymentPlan(
            deployment_id=None,
            object_changes=(),
            rebuild_subtrees=(),
            steps=(),
            prepared_shadow_objects=(),
            warnings=(),
            sql_diffs=(
                PlannedSqlDiff(
                    key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    object_type="table",
                    name="tbl__orders_enriched",
                    diff_lines=("--- current", "+++ desired", "+ added"),
                ),
            ),
        ),
        desired_state=realize_compiled_pipelines(
            (build_scalar_replay_compiled_pipeline("timestamp"),)
        ).desired_state,
        database="analytics",
        adapter_name="clickhouse",
        json_output=False,
        verbose=True,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders human-readable virtual build summary",
            expected_fragments=(
                "Virtual Build Ready",
                "Database: analytics",
                "Deployment: 20260410T000000Z_ab12cd",
                "Roots",
                "- tbl__orders_enriched",
                "strategy: create_from_scratch",
                "warehouse-written rows: 12",
                '"warehouse_written_rows": 12',
                "sqt deployment audit 20260410T000000Z_ab12cd",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_build_result_when_rendering_text_then_it_returns_operator_summary(
    test_case: CliRenderingTestCase,
) -> None:
    result: BackfillExecutionResult = BackfillExecutionResult(
        bootstrap=BackfillBootstrapResult(
            deployment_id="20260410T000000Z_ab12cd",
            created_at="2026-04-10 00:00:00.000",
            deployment_plan=DeploymentPlan(
                deployment_id=None,
                object_changes=(),
                rebuild_subtrees=(),
                steps=(),
                prepared_shadow_objects=(),
                warnings=(),
            ),
            root_reports=(
                RootBackfillReport(
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    state_kind="greenfield",
                    replay_strategy="create_from_scratch",
                    active_deployment_id=None,
                ),
            ),
            existing_relation_names=frozenset(),
        ),
        boundary_time="2026-04-10 00:00:00.000",
        replay_results=(
            BackfillRootReplayResult(
                root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                written_rows=12,
            ),
        ),
    )
    rendered: str = render_virtual_build_result(
        result=result,
        database="analytics",
        json_output=False,
    )
    json_rendered: str = render_virtual_build_result(
        result=result,
        database="analytics",
        json_output=True,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in f"{rendered}\n{json_rendered}"


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders actual direct replay roots and warehouse-written rows",
            expected_fragments=(
                "Replay roots executed: 2",
                "orders_enriched  warehouse-written rows: 7",
                "order_totals  warehouse-written rows: unavailable",
                '"warehouse_written_rows": 7',
                '"warehouse_written_rows": null',
                "Effective start time: 2026-08-02 09:00:00.000",
                '"start_time": "2026-08-02 09:00:00.000"',
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_replay_results_when_rendering_then_text_and_json_report_warehouse_rows(
    test_case: CliRenderingTestCase,
) -> None:
    result: DirectBuildResult = DirectBuildResult(
        database="analytics",
        preserved_source_relation_names=(),
        created_source_relation_names=(),
        dropped_relation_names=(),
        created_relation_names=(),
        boundary_time="2026-07-31 12:00:00.000",
        boundaries=(),
        replay_results=(
            DirectRootReplayResult(model_name="orders_enriched", written_rows=7),
            DirectRootReplayResult(model_name="order_totals", written_rows=None),
        ),
        effective_start_time="2026-08-02 09:00:00.000",
    )
    audit_result: SqlAuditRunResult = SqlAuditRunResult(audit_results=())
    rendered_text: str = render_direct_build_text(
        result=result,
        adapter_name="clickhouse",
        audit_result=audit_result,
    )
    rendered_json: str = render_direct_build_json(
        result=result,
        adapter_name="clickhouse",
        audit_result=audit_result,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in f"{rendered_text}\n{rendered_json}"


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders audit root and abnormal metric colors when forced",
            expected_fragments=(
                "- \u001b[1m\u001b[32mtbl__orders_enriched\u001b[0m",
                "- \u001b[1m\u001b[33mtbl__flight_positions\u001b[0m",
                "\u001b[2mrow delta\u001b[0m: +0",
                "\u001b[2mrow delta\u001b[0m: \u001b[33m-699534\u001b[0m",
                "\u001b[2mrow ratio\u001b[0m: \u001b[33m13.4%\u001b[0m",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_forced_color_when_rendering_audit_then_it_colors_root_names(
    test_case: CliRenderingTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rendered: str = render_deployment_audit_result(
        result=DeploymentAuditResult(
            deployment_id="20260414T190649Z_36b35f",
            deployment_status="backfilling",
            assessment="caution",
            replay_lineage_mode="offsets",
            warning_codes=(),
            root_results=(
                RootAuditResult(
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    staged_physical_name="tbl__orders_enriched__20260414T190649Z_36b35f",
                    state="active_view_present",
                    replay_source_name="raw__orders",
                    replay_source_row_count=42,
                    staged_exists=True,
                    active_exists=True,
                    active_row_count=42,
                    staged_row_count=42,
                    row_delta=0,
                    row_ratio=1.0,
                    assessment="ready",
                    replay_lineage_mode="offsets",
                    offset_catchup_summary=None,
                    scalar_catchup_summary=None,
                    warnings=(),
                ),
                RootAuditResult(
                    root_key=ObjectKey(None, "table", "tbl__flight_positions"),
                    staged_physical_name="tbl__flight_positions__20260414T190649Z_36b35f",
                    state="active_view_present",
                    replay_source_name="raw__flight_states",
                    replay_source_row_count=108242,
                    staged_exists=True,
                    active_exists=True,
                    active_row_count=807776,
                    staged_row_count=108242,
                    row_delta=-699534,
                    row_ratio=108242 / 807776,
                    assessment="caution",
                    replay_lineage_mode="offsets",
                    offset_catchup_summary=None,
                    scalar_catchup_summary=None,
                    warnings=(
                        "staged row count is far below active row count for tbl__flight_positions",
                    ),
                ),
            ),
        ),
        database="analytics",
        json_output=False,
    )

    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders human-readable audit summary",
            expected_fragments=(
                "Audit: ready",
                "Deployment status: backfilling",
                "Replay lineage: offsets",
                "- tbl__orders_enriched",
                "staged exists: yes",
                "staged rows: 42",
                "active rows: n/a",
                "Next",
                "sqt deployment promote 20260410T000000Z_ab12cd",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_audit_result_when_rendering_text_then_it_returns_operator_summary(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_deployment_audit_result(
        result=DeploymentAuditResult(
            deployment_id="20260410T000000Z_ab12cd",
            deployment_status="backfilling",
            assessment="ready",
            replay_lineage_mode="offsets",
            warning_codes=(),
            root_results=(
                RootAuditResult(
                    root_key=ObjectKey(None, "table", "tbl__orders_enriched"),
                    staged_physical_name="tbl__orders_enriched__20260410T000000Z_ab12cd",
                    staged_exists=True,
                    active_exists=False,
                    active_row_count=None,
                    staged_row_count=42,
                    row_delta=None,
                    row_ratio=None,
                    assessment="ready",
                    replay_lineage_mode="offsets",
                    offset_catchup_summary=None,
                    scalar_catchup_summary=None,
                    state="greenfield",
                    replay_source_name="raw__orders",
                    replay_source_row_count=42,
                ),
            ),
        ),
        database="analytics",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders human-readable promotion summary",
            expected_fragments=(
                "Promotion Complete",
                "Database: analytics",
                "Deployment: 20260410T000000Z_ab12cd",
                "- tbl__orders_enriched -> tbl__orders_enriched__20260410T000000Z_ab12cd",
                "Atomicity",
                "- Each logical binding replacement: atomic",
                "- Entire deployment promotion: not atomic",
                "- Bindings are replaced one relation at a time",
                "Next",
                "post-promotion check",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_promotion_result_when_rendering_text_then_it_returns_operator_summary(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_promotion_result(
        result=PublishResult(
            deployment_id="20260410T000000Z_ab12cd",
            published_views=(
                PublishedView(
                    view_name="tbl__orders_enriched",
                    target_table_name="tbl__orders_enriched__20260410T000000Z_ab12cd",
                ),
            ),
            per_relation_atomic_replace=True,
            graph_atomic_publish=False,
        ),
        database="analytics",
        json_output=False,
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliPublishAtomicityRenderingTestCase(
            description="renders exact publish atomicity in JSON",
            per_relation_atomic_replace=True,
            graph_atomic_publish=False,
            expected_atomicity={
                "per_relation_atomic_replace": True,
                "graph_atomic_publish": False,
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_publish_result_when_rendering_json_then_atomicity_is_machine_readable(
    test_case: CliPublishAtomicityRenderingTestCase,
) -> None:
    rendered: str = render_promotion_result(
        result=PublishResult(
            deployment_id="20260410T000000Z_ab12cd",
            published_views=(
                PublishedView(
                    view_name="tbl__orders_enriched",
                    target_table_name="tbl__orders_enriched__20260410T000000Z_ab12cd",
                ),
            ),
            per_relation_atomic_replace=test_case.per_relation_atomic_replace,
            graph_atomic_publish=test_case.graph_atomic_publish,
        ),
        database="analytics",
        json_output=True,
    )
    payload: dict[str, object] = json.loads(rendered)

    assert payload["atomicity"] == test_case.expected_atomicity


@pytest.mark.parametrize(
    "test_case",
    [
        CliRenderingTestCase(
            description="renders ambiguity guidance with roots and next command",
            expected_fragments=(
                "Deployment Audit selection is ambiguous",
                "Affected roots",
                "- tbl__orders_enriched",
                "Candidate deployments",
                "- 20260410T000000Z_cd34ef",
                "created at: 2026-04-10T00:05:00.000Z",
                "status: backfilling",
                "roots: tbl__orders_enriched",
                "Recommended",
                "- sqt deployment audit 20260410T000000Z_cd34ef",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_ambiguous_candidates_when_rendering_message_then_it_returns_guidance(
    test_case: CliRenderingTestCase,
) -> None:
    rendered: str = render_ambiguous_deployment_message(
        command_name="deployment audit",
        database="analytics",
        root_names=("tbl__orders_enriched",),
        candidates=(
            AuditDeploymentCandidate(
                deployment_id="20260410T000000Z_ab12cd",
                created_at="2026-04-10 00:00:00.000",
                deployment_status="backfilling",
                root_names=("tbl__orders_enriched",),
            ),
            AuditDeploymentCandidate(
                deployment_id="20260410T000000Z_cd34ef",
                created_at="2026-04-10 00:05:00.000",
                deployment_status="backfilling",
                root_names=("tbl__orders_enriched",),
            ),
        ),
    )

    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered
