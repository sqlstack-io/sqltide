import re

import pytest

from sqltide._native import plan_deployment, plan_population_objects
from sqltide.adapter.models import (
    AdapterIdentity,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.models import ActualState, DeploymentPlan
from sqltide.executor.population.models import (
    PopulationObject,
    PopulationPlan,
    PopulationRealization,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    build_single_transform_desired_state,
)
from tests.unit.src.sqltide.executor.backfill._helpers._test_types import (
    CreateShadowObjectsOrderingTestCase,
    StagedViewActiveSourceTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CreateShadowObjectsOrderingTestCase(
            description=(
                "creates referenced shadow tables before dependent shadow materialized views"
            ),
            expected_preceding_fragment="CREATE TABLE analytics.tbl__region_lookup__dep_ref",
            expected_following_fragment=(
                "CREATE MATERIALIZED VIEW analytics.mv__orders_enriched__dep_ref"
            ),
            expected_rewritten_query_fragment=(
                "LEFT JOIN analytics.tbl__region_lookup__dep_ref AS r"
            ),
            expected_absent_query_fragment=("LEFT JOIN analytics.tbl__region_lookup AS r"),
            expected_canonical_query_fragment=("LEFT JOIN tbl__region_lookup__dep_ref AS r"),
            expected_database_template_fragment=(
                "LEFT JOIN __sqltide_target_database__.tbl__region_lookup__dep_ref AS r"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reference_ref_when_creating_shadow_objects_then_it_creates_dependency_tables_first(
    test_case: CreateShadowObjectsOrderingTestCase,
) -> None:
    desired_state: DesiredState = build_single_transform_desired_state(
        query=(
            "SELECT CAST(o.order_id AS UInt64) AS order_id, "
            "CAST(o._replay_partition AS UInt64) AS _replay_partition, "
            "CAST(o._replay_offset AS UInt64) AS _replay_offset "
            'FROM __ref("orders") AS o LEFT JOIN '
            '__ref("region_lookup", ref_type="reference") AS r ON o.order_id = r.region_id'
        ),
        order_by=("order_id", "_replay_partition", "_replay_offset"),
        supporting_transforms=(
            (
                "region_lookup",
                "SELECT CAST(order_id AS UInt64) AS region_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders")',
                ("region_id",),
            ),
        ),
    )
    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=ActualState(objects=()),
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
        deployment_id="dep_ref",
    )
    population_plan: PopulationPlan = PopulationPlan(
        execution_id="dep_ref",
        roots=(),
        objects=tuple(
            PopulationObject(logical_key=prepared.logical_key, physical_name=prepared.physical_name)
            for prepared in deployment_plan.prepared_shadow_objects
        ),
    )
    realizations: tuple[PopulationRealization, ...] = plan_population_objects(
        plan=population_plan,
        desired_state=desired_state,
        default_database="analytics",
    )

    adapter: ClickHouseAdapter = ClickHouseAdapter()
    rendered_commands: str = "\n".join(
        adapter.render_resource(resource=item.resource, database=item.database)
        for item in realizations
    )
    rewritten_resources: str = "\n".join(str(item.resource) for item in realizations)
    preceding_index: int = rendered_commands.index(test_case.expected_preceding_fragment)
    following_index: int = rendered_commands.index(test_case.expected_following_fragment)

    assert preceding_index < following_index
    assert test_case.expected_rewritten_query_fragment in rendered_commands
    assert test_case.expected_absent_query_fragment not in rendered_commands
    assert test_case.expected_canonical_query_fragment in rewritten_resources
    assert test_case.expected_database_template_fragment in rewritten_resources


@pytest.mark.parametrize(
    "test_case",
    [
        StagedViewActiveSourceTestCase(
            description="a published upstream model feeds the staged view from its physical table",
            active_source_binding="tbl__orders_base__dep_a",
            expected_source_pattern=r"FROM analytics\.tbl__orders_base__dep_a\b",
        ),
        StagedViewActiveSourceTestCase(
            description="an upstream table with no binding keeps its own name",
            active_source_binding=None,
            expected_source_pattern=r"FROM analytics\.tbl__orders_base\b",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unstaged_upstream_model_when_creating_shadow_objects_then_view_reads_active_table(
    test_case: StagedViewActiveSourceTestCase,
) -> None:
    desired_state: DesiredState = build_single_transform_desired_state(
        query=(
            "SELECT CAST(order_id AS UInt64) AS order_id, "
            "CAST(_replay_partition AS UInt64) AS _replay_partition, "
            "CAST(_replay_offset AS UInt64) AS _replay_offset "
            'FROM __ref("orders_base")'
        ),
        order_by=("order_id", "_replay_partition", "_replay_offset"),
        source="orders_base",
        supporting_transforms=(
            (
                "orders_base",
                "SELECT CAST(kafka_key AS UInt64) AS order_id, "
                "CAST(_replay_partition AS UInt64) AS _replay_partition, "
                "CAST(_replay_offset AS UInt64) AS _replay_offset "
                'FROM __ref("orders")',
                ("order_id",),
            ),
        ),
    )
    deployment_plan: DeploymentPlan = plan_deployment(
        desired_state=desired_state,
        actual_state=ActualState(objects=()),
        default_database="analytics",
        render_resource=ClickHouseAdapter().render_resource,
        deployment_id="dep_b",
    )
    objects_by_name: dict[str, PopulationObject] = {
        prepared.logical_key.name: PopulationObject(
            logical_key=prepared.logical_key, physical_name=prepared.physical_name
        )
        for prepared in deployment_plan.prepared_shadow_objects
    }
    # Only the downstream model is staged; the upstream model stays published.
    population_plan: PopulationPlan = PopulationPlan(
        execution_id="dep_b",
        roots=(),
        objects=(objects_by_name["tbl__orders_enriched"], objects_by_name["mv__orders_enriched"]),
    )
    catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="tbl__orders_base",
                engine="View",
                columns=(),
                stable_binding_name=test_case.active_source_binding,
            ),
        ),
    )

    realizations: tuple[PopulationRealization, ...] = plan_population_objects(
        plan=population_plan,
        desired_state=desired_state,
        default_database="analytics",
        target_catalog=catalog,
    )

    adapter: ClickHouseAdapter = ClickHouseAdapter()
    rendered_commands: str = "\n".join(
        adapter.render_resource(resource=item.resource, database=item.database)
        for item in realizations
    )
    assert re.search(test_case.expected_source_pattern, rendered_commands) is not None
