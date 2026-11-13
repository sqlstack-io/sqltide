import pytest

from sqltide._native import plan_deployment
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import (
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.planner.constants import REBUILD_EXECUTION_MODE_FULL
from sqltide.compiler.planner.models import ActualState, DeploymentPlan
from sqltide.compiler.planner.types import DeploymentAction
from tests.unit.src.sqltide.compiler.planner._test_types import (
    PlannerDeploymentPlanTestCase,
    PlannerFullRefreshPlanTestCase,
    PlannerInvalidTableSettingTestCase,
    PlannerMutableWarningTestCase,
    PlannerQueryOnlyChangeTestCase,
    PlannerSettledPlanTestCase,
    PlannerShadowIdentityTestCase,
    PlannerTableSettingSqlTestCase,
    PlannerViewDeploymentTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_actual_state_matching_desired,
    build_example_actual_state,
    build_example_desired_state,
    build_mutable_ref_desired_state,
    build_single_transform_desired_state,
    build_view_only_desired_state,
    key_parts,
    optional_key_parts,
    orders_enriched_table,
    with_raw_orders_ttl,
)

_SETTINGS_QUERY: str = 'SELECT CAST(kafka_key AS String) AS order_id FROM __ref("orders")'


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerDeploymentPlanTestCase(
            description="builds staged deployment plan from conservative object changes",
            expected_change_count=5,
            expected_rebuild_root_keys=((None, "table", "tbl__orders_enriched"),),
            expected_steps=(
                ("plan", "plan_shadow_table", (None, "table", "tbl__orders_enriched")),
                (
                    "plan",
                    "plan_shadow_materialized_view",
                    (None, "materialized_view", "mv__orders_enriched"),
                ),
                ("backfill", "backfill_subtree", None),
                ("audit", "audit_subtree", None),
                ("publish", "publish_subtree", None),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_desired_and_actual_state_when_planning_deployment_then_it_returns_expected_steps(
    test_case: PlannerDeploymentPlanTestCase,
) -> None:
    desired_state: DesiredState = build_example_desired_state()
    actual_state: ActualState = build_example_actual_state()

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    )

    assert len(deployment_plan.object_changes) == test_case.expected_change_count
    assert (
        tuple(key_parts(subtree.root_key) for subtree in deployment_plan.rebuild_subtrees)
        == test_case.expected_rebuild_root_keys
    )
    assert (
        tuple(
            (
                step.phase,
                step.action,
                optional_key_parts(step.target_key),
            )
            for step in deployment_plan.steps
        )
        == test_case.expected_steps
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerViewDeploymentTestCase(
            description="view-only deployment prepares one view without replay work",
            deployment_id="20260731T200000Z_ab12cd",
            expected_root_key=(None, "view", "customer_orders"),
            expected_physical_name="customer_orders__20260731T200000Z_ab12cd",
            expected_action=DeploymentAction.PLAN_SHADOW_VIEW,
            expected_replay_required=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_view_only_change_when_planning_deployment_then_it_has_no_replay_root(
    test_case: PlannerViewDeploymentTestCase,
) -> None:
    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=build_view_only_desired_state(),
        actual_state=ActualState(objects=()),
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
        deployment_id=test_case.deployment_id,
    )

    assert key_parts(deployment_plan.rebuild_subtrees[0].root_key) == test_case.expected_root_key
    assert deployment_plan.rebuild_subtrees[0].replay_required is test_case.expected_replay_required
    assert deployment_plan.prepared_shadow_objects[0].physical_name == (
        test_case.expected_physical_name
    )
    assert deployment_plan.steps[0].action == test_case.expected_action


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerQueryOnlyChangeTestCase(
            description="a filter-only change rebuilds from the table its materialized view fills",
            published_query=_SETTINGS_QUERY,
            changed_query=f"{_SETTINGS_QUERY} WHERE kafka_key != ''",
            expected_root_key=(None, "table", "tbl__orders_enriched"),
            expected_affected_keys=(
                (None, "table", "tbl__orders_enriched"),
                (None, "materialized_view", "mv__orders_enriched"),
            ),
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_query_only_change_when_planning_deployment_then_it_roots_at_the_target_table(
    test_case: PlannerQueryOnlyChangeTestCase,
) -> None:
    published: DesiredState = build_single_transform_desired_state(
        query=test_case.published_query
    )

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=build_single_transform_desired_state(query=test_case.changed_query),
        actual_state=build_actual_state_matching_desired(published),
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    )

    assert len(deployment_plan.rebuild_subtrees) == 1
    subtree = deployment_plan.rebuild_subtrees[0]
    assert key_parts(subtree.root_key) == test_case.expected_root_key
    assert tuple(key_parts(key) for key in subtree.affected_keys) == (
        test_case.expected_affected_keys
    )
    assert subtree.replay_required is True
    assert subtree.execution_mode == test_case.expected_execution_mode


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerMutableWarningTestCase(
            description="emits mutable-ref warning for affected transform target",
            expected_warning_code="mutable_ref_replay_not_guaranteed",
            expected_target_key=(None, "table", "tbl__orders_enriched"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_desired_state_with_mutable_refs_when_planning_deployment_then_it_returns_warning(
    test_case: PlannerMutableWarningTestCase,
) -> None:
    desired_state: DesiredState = build_mutable_ref_desired_state()
    actual_state: ActualState = build_example_actual_state()

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    )

    assert tuple(warning.warning_code for warning in deployment_plan.warnings) == (
        test_case.expected_warning_code,
    )
    assert tuple(
        optional_key_parts(warning.target_key) for warning in deployment_plan.warnings
    ) == (test_case.expected_target_key,)


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerShadowIdentityTestCase(
            description="adds deterministic shadow physical names when deployment id is supplied",
            deployment_id="20260408T153012Z_ab12cd",
            expected_prepared_shadow_objects=(
                (
                    (None, "table", "tbl__orders_enriched"),
                    "tbl__orders_enriched__20260408T153012Z_ab12cd",
                    "orders_enriched",
                ),
                (
                    (None, "materialized_view", "mv__orders_enriched"),
                    "mv__orders_enriched__20260408T153012Z_ab12cd",
                    "orders_enriched",
                ),
            ),
            expected_plan_step_physical_names=(
                "tbl__orders_enriched__20260408T153012Z_ab12cd",
                "mv__orders_enriched__20260408T153012Z_ab12cd",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_id_when_planning_deployment_then_it_returns_prepared_shadow_objects(
    test_case: PlannerShadowIdentityTestCase,
) -> None:
    desired_state: DesiredState = build_example_desired_state()
    actual_state: ActualState = build_example_actual_state()

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
        deployment_id=test_case.deployment_id,
    )

    assert deployment_plan.deployment_id == test_case.deployment_id
    assert (
        tuple(
            (
                key_parts(prepared_shadow_object.logical_key),
                prepared_shadow_object.physical_name,
                prepared_shadow_object.logical_model_name,
            )
            for prepared_shadow_object in deployment_plan.prepared_shadow_objects
        )
        == test_case.expected_prepared_shadow_objects
    )
    assert (
        tuple(step.physical_name for step in deployment_plan.steps[:2])
        == test_case.expected_plan_step_physical_names
    )
    assert deployment_plan.sql_diffs != ()


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerFullRefreshPlanTestCase(
            description="forces full rebuild planning for a selected no-op root",
            full_refresh_key=(None, "table", "tbl__orders_enriched"),
            expected_rebuild_root_keys=((None, "table", "tbl__orders_enriched"),),
            expected_execution_mode=REBUILD_EXECUTION_MODE_FULL,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_full_refresh_key_when_planning_deployment_then_it_forces_full_rebuild(
    test_case: PlannerFullRefreshPlanTestCase,
) -> None:
    desired_state: DesiredState = build_example_desired_state()
    actual_state: ActualState = build_actual_state_matching_desired(desired_state)
    desired_keys_by_parts: dict[tuple[str | None, str, str], ObjectKey] = {
        key_parts(object_.key): object_.key for object_ in desired_state.objects
    }
    full_refresh_key: ObjectKey = desired_keys_by_parts[test_case.full_refresh_key]

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
        full_refresh_keys=frozenset({full_refresh_key}),
    )

    assert (
        tuple(key_parts(subtree.root_key) for subtree in deployment_plan.rebuild_subtrees)
        == test_case.expected_rebuild_root_keys
    )
    assert tuple(subtree.execution_mode for subtree in deployment_plan.rebuild_subtrees) == (
        test_case.expected_execution_mode,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerSettledPlanTestCase(
            description="a published virtual-environment deployment still plans no rebuild work",
            raw_ttl=None,
            expected_rebuild_subtree_count=0,
            expected_step_count=0,
            expected_change_types=frozenset({"no_op"}),
        ),
        PlannerSettledPlanTestCase(
            description="a landing table ttl without a canonical form is still unchanged",
            raw_ttl="created_at + INTERVAL 7 DAY WHERE order_id != ''",
            expected_rebuild_subtree_count=0,
            expected_step_count=0,
            expected_change_types=frozenset({"no_op"}),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_matching_actual_state_when_planning_deployment_then_it_remains_a_no_op(
    test_case: PlannerSettledPlanTestCase,
) -> None:
    desired_state: DesiredState = with_raw_orders_ttl(
        build_example_desired_state(), test_case.raw_ttl
    )
    actual_state: ActualState = build_actual_state_matching_desired(desired_state)

    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
    )

    assert len(deployment_plan.rebuild_subtrees) == test_case.expected_rebuild_subtree_count
    assert len(deployment_plan.steps) == test_case.expected_step_count
    assert (
        frozenset(change.change_type for change in deployment_plan.object_changes)
        == test_case.expected_change_types
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerTableSettingSqlTestCase(
            description="quotes a bare word and keeps keywords, numbers and booleans as sql values",
            settings={
                "storage_policy": "hot",
                "merge_with_ttl_timeout": "3600",
                "allow_nullable_key": "True",
                "min_bytes_for_wide_part": "'10485760'",
                "merge_selector_algorithm": "default",
                "max_suspicious_broken_parts": "infinity",
                "ttl_only_drop_parts": '"default"',
            },
            expected_settings={
                "allow_nullable_key": "TRUE",
                "max_suspicious_broken_parts": "infinity",
                "merge_selector_algorithm": "DEFAULT",
                "merge_with_ttl_timeout": "3600",
                "min_bytes_for_wide_part": "'10485760'",
                "storage_policy": "'hot'",
                "ttl_only_drop_parts": "'default'",
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_model_table_settings_when_realizing_then_values_render_as_one_sql_value(
    test_case: PlannerTableSettingSqlTestCase,
) -> None:
    desired_state: DesiredState = build_single_transform_desired_state(
        query=_SETTINGS_QUERY, settings=test_case.settings
    )
    table: DesiredTable = orders_enriched_table(desired_state)

    assert dict(table.spec.storage.settings or {}) == test_case.expected_settings


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerInvalidTableSettingTestCase(
            description="two values",
            value="1 2",
            expected_error_fragment="is not exactly one SQL value",
        ),
        PlannerInvalidTableSettingTestCase(
            description="a value list",
            value="1, 2",
            expected_error_fragment="is not exactly one SQL value",
        ),
        PlannerInvalidTableSettingTestCase(
            description="an aliased value",
            value="'x' AS y",
            expected_error_fragment="is not exactly one SQL value",
        ),
        PlannerInvalidTableSettingTestCase(
            description="two bare words",
            value="hot cold",
            expected_error_fragment="is not exactly one SQL value",
        ),
        PlannerInvalidTableSettingTestCase(
            description="a trailing clause",
            value="1) FROM t WHERE (1",
            expected_error_fragment="is not exactly one SQL value",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_malformed_table_setting_when_realizing_then_compile_fails(
    test_case: PlannerInvalidTableSettingTestCase,
) -> None:
    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        build_single_transform_desired_state(
            query=_SETTINGS_QUERY, settings={"merge_with_ttl_timeout": test_case.value}
        )
