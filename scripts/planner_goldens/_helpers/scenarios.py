"""Fixed planner scenarios run against one analyzed corpus project."""

from collections.abc import Callable
from dataclasses import replace

from scripts.planner_goldens._helpers.serialize import jsonable
from scripts.planner_goldens.constants import (
    DRIFTED_MATERIALIZED_VIEW_QUERY,
    DRIFTED_TABLE_SETTINGS,
    GOLDEN_DATABASE,
    GOLDEN_DEPLOYMENT_ID,
    GOLDEN_START_TIME,
)
from scripts.planner_goldens.types import ActualObject, DesiredObject
from sqltide.adapter.models import (
    AdapterIdentity,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.main.plan_deployment import plan_deployment
from sqltide.compiler.planner.main.plan_direct_build import plan_direct_build
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualState,
    ActualTable,
    ActualView,
    DirectWarehouseSnapshot,
)


def planner_scenarios(*, analysis: CompileAnalysis) -> dict[str, object]:
    """Run every fixed scenario, recording each plan or the error it raised."""

    desired_state: DesiredState = analysis.realized_project.desired_state
    settled: ActualState = ActualState(objects=tuple(map(_settled, desired_state.objects)))
    anchors: frozenset[ObjectKey] = desired_state.replay_anchor_keys
    empty: ActualState = ActualState(objects=())
    scenarios: dict[str, Callable[[], object]] = {
        "deploy_empty": lambda: _deploy(desired_state=desired_state, actual_state=empty),
        "deploy_settled": lambda: _deploy(desired_state=desired_state, actual_state=settled),
        "deploy_drifted": lambda: _deploy(
            desired_state=desired_state, actual_state=_drifted(desired_state.objects)
        ),
        "deploy_full_refresh_anchors": lambda: _deploy(
            desired_state=desired_state, actual_state=settled, full_refresh_keys=anchors
        ),
        "deploy_start_time_anchors": lambda: _deploy(
            desired_state=desired_state,
            actual_state=empty,
            start_time_keys=anchors,
        ),
        "direct_empty_all_models": lambda: _direct(analysis=analysis, relation_names=()),
        "direct_settled_all_models": lambda: _direct(
            analysis=analysis, relation_names=tuple(obj.name for obj in desired_state.objects)
        ),
    }
    return {name: _outcome(run) for name, run in scenarios.items()}


def _outcome(run: Callable[[], object]) -> object:
    try:
        return {"plan": jsonable(run())}
    except Exception as error:  # noqa: BLE001 - the golden records any planner rejection
        return {"error": type(error).__name__, "message": str(error)}


def _deploy(
    *,
    desired_state: DesiredState,
    actual_state: ActualState,
    full_refresh_keys: frozenset[ObjectKey] = frozenset(),
    start_time_keys: frozenset[ObjectKey] = frozenset(),
) -> object:
    return plan_deployment(
        desired_state=desired_state,
        actual_state=actual_state,
        default_database=GOLDEN_DATABASE,
        render_resource=ClickHouseAdapter().render_resource,
        deployment_id=GOLDEN_DEPLOYMENT_ID,
        full_refresh_keys=full_refresh_keys,
        start_time_keys=start_time_keys,
        start_time=GOLDEN_START_TIME if start_time_keys else None,
    )


def _direct(*, analysis: CompileAnalysis, relation_names: tuple[str, ...]) -> object:
    columns_by_name: dict[str, tuple[CatalogColumn, ...]] = {
        obj.name: _catalog_columns(obj)
        for obj in analysis.realized_project.desired_state.objects
        if isinstance(obj, (DesiredKafkaTable, DesiredTable))
    }
    return plan_direct_build(
        graph=analysis.graph,
        realized_project=analysis.realized_project,
        snapshot=DirectWarehouseSnapshot(
            catalog=CatalogSnapshot(
                identity=CatalogIdentity(
                    adapter=AdapterIdentity(name="clickhouse"), database=GOLDEN_DATABASE
                ),
                warehouse_timezone="UTC",
                relations=tuple(
                    CatalogRelation(
                        name=name, engine="MergeTree", columns=columns_by_name.get(name, ())
                    )
                    for name in relation_names
                ),
            )
        ),
        database=GOLDEN_DATABASE,
        selected_model_keys=frozenset(),
    )


def _catalog_columns(obj: DesiredKafkaTable | DesiredTable) -> tuple[CatalogColumn, ...]:
    return tuple(CatalogColumn(name=column.name, type=column.type) for column in obj.columns)


def _settled(obj: DesiredObject) -> ActualObject:
    if isinstance(obj, DesiredKafkaTable):
        return ActualKafkaTable(key=obj.key, spec=obj.spec)
    if isinstance(obj, DesiredTable):
        return ActualTable(key=obj.key, spec=obj.spec)
    if isinstance(obj, DesiredMaterializedView):
        return ActualMaterializedView(key=obj.key, spec=obj.spec)
    return ActualView(key=obj.key, spec=obj.spec)


def _drifted(objects: tuple[DesiredObject, ...]) -> ActualState:
    """Settled state with one shortened table, one re-tuned table, and one rewritten MV."""

    tables: list[DesiredTable] = [obj for obj in objects if isinstance(obj, DesiredTable)]
    views: list[DesiredMaterializedView] = [
        obj for obj in objects if isinstance(obj, DesiredMaterializedView)
    ]
    drifted: dict[ObjectKey, ActualObject] = {}
    if tables:
        first: DesiredTable = tables[0]
        drifted[first.key] = ActualTable(
            key=first.key, spec=replace(first.spec, columns=first.spec.columns[:-1])
        )
    if len(tables) > 1:
        second: DesiredTable = tables[1]
        drifted[second.key] = ActualTable(
            key=second.key,
            spec=replace(
                second.spec,
                storage=replace(second.spec.storage, settings=DRIFTED_TABLE_SETTINGS),
            ),
        )
    if views:
        view: DesiredMaterializedView = views[0]
        drifted[view.key] = ActualMaterializedView(
            key=view.key, spec=replace(view.spec, query=DRIFTED_MATERIALIZED_VIEW_QUERY)
        )
    return ActualState(objects=tuple(drifted.get(obj.key) or _settled(obj) for obj in objects))
