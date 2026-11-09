"""Load actual warehouse state for the managed objects of a pipeline."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.main.load_planning_warehouse_snapshot import (
    load_planning_warehouse_snapshot,
)
from sqltide.compiler.planner.models import ActualState, PlanningWarehouseSnapshot


def load_actual_state(
    *,
    client: AdapterConnection,
    desired_state: DesiredState,
    database: str,
) -> ActualState:
    """Load the current active actual state from ClickHouse inspection."""

    snapshot: PlanningWarehouseSnapshot = load_planning_warehouse_snapshot(
        client=client,
        database=database,
    )
    return _native.load_actual_state_from_snapshot(
        snapshot=snapshot,
        desired_state=desired_state,
        database=database,
    )
