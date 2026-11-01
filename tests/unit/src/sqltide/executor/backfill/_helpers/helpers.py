from sqltide.adapter.models import AdapterReplayRequest, CatalogColumn, CatalogRelation
from sqltide.compiler.compile.models import DesiredState, ObjectKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.models import DeploymentPlan, DeploymentWatermarkRecord
from sqltide.executor.population._helpers.replay import build_population_replay_requests
from sqltide.executor.population.models import (
    PopulationObject,
    PopulationPlan,
    PopulationRoot,
    PopulationWatermark,
)


def capture_replay_requests(
    *,
    mode: ReplayLineageMode,
    deployment_plan: DeploymentPlan,
    desired_state: DesiredState,
    watermarks: tuple[DeploymentWatermarkRecord, ...],
) -> tuple[AdapterReplayRequest, ...]:
    plan: PopulationPlan = PopulationPlan(
        execution_id=deployment_plan.deployment_id or "test",
        roots=tuple(
            PopulationRoot(
                root_key=subtree.root_key,
                affected_keys=subtree.affected_keys,
                upstream_boundary_key=subtree.upstream_boundary_key,
                replay_lineage_mode=mode,
                execution_mode=subtree.execution_mode,
                forced_start_time=subtree.forced_start_time,
                execution_lookback_seconds=subtree.execution_lookback_seconds,
            )
            for subtree in deployment_plan.rebuild_subtrees
        ),
        objects=tuple(
            PopulationObject(logical_key=prepared.logical_key, physical_name=prepared.physical_name)
            for prepared in deployment_plan.prepared_shadow_objects
        ),
    )
    requests: tuple[tuple[ObjectKey, AdapterReplayRequest], ...] = build_population_replay_requests(
        plan=plan,
        desired_state=desired_state,
        default_database="analytics",
        watermarks=tuple(
            PopulationWatermark(
                root_key=watermark.root_key,
                anchor_key=watermark.anchor_key,
                boundary_key=watermark.boundary_key,
                cutoff_value=watermark.cutoff_value,
            )
            for watermark in watermarks
        ),
        boundary_time="2026-04-08 13:00:00.000",
    )
    return tuple(request for _root_key, request in requests)


def bound_live_table(*column_names: str) -> tuple[CatalogRelation, ...]:
    """Catalog relations for a stable view bound to a live table with `column_names`."""

    return (
        CatalogRelation(
            name="tbl__hourly_order_volume",
            engine="View",
            columns=(),
            stable_binding_name="tbl__hourly_order_volume__20260409T150000Z_ab12",
        ),
        CatalogRelation(
            name="tbl__hourly_order_volume__20260409T150000Z_ab12",
            engine="MergeTree",
            columns=tuple(CatalogColumn(name=name, type="String") for name in column_names),
        ),
    )
