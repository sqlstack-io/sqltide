from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from sqltide._native import add_empty_replay_source_warnings
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterIdentity,
    AdapterQueryResult,
    CatalogIdentity,
    CatalogRelation,
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
from sqltide.compiler.planner.constants import REBUILD_STRATEGY_SHADOW
from sqltide.compiler.planner.models import DeploymentPlan, RebuildSubtree
from tests.unit.src.sqltide.cli.plan.main._test_types import (
    CliReplaySourceWarningTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliReplaySourceWarningTestCase(
            description="adds empty replay source warning with active row context",
            replay_source_row_count=0,
            active_row_count=699534,
            expected_warning_message_fragment=(
                "replay source raw__flight_states is empty; staged outputs for this subtree may "
                "also be empty (active target tbl__flight_positions currently has 699534 rows)"
            ),
            expected_point_in_time_query_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_replay_source_when_augmenting_plan_then_it_adds_warning(
    test_case: CliReplaySourceWarningTestCase,
) -> None:
    raw_key: ObjectKey = ObjectKey(None, "table", "raw__flight_states")
    target_key: ObjectKey = ObjectKey(None, "table", "tbl__flight_positions")
    desired_state: DesiredState = DesiredState(
        objects=(
            DesiredTable(
                key=raw_key,
                deps=(),
                spec=TableSpec(
                    columns=(Column(name="kafka_key", type="String"),),
                    storage=TableStorage(engine="MergeTree()", order_by=("kafka_key",)),
                ),
            ),
            DesiredTable(
                key=target_key,
                deps=(raw_key,),
                spec=TableSpec(
                    columns=(Column(name="icao24", type="String"),),
                    storage=TableStorage(engine="MergeTree()", order_by=("icao24",)),
                ),
            ),
        ),
        replay_anchor_keys=frozenset({raw_key}),
        mutable_ref_warning_keys=frozenset(),
    )
    plan: DeploymentPlan = DeploymentPlan(
        deployment_id=None,
        object_changes=(),
        rebuild_subtrees=(
            RebuildSubtree(
                root_key=target_key,
                affected_keys=(target_key,),
                upstream_boundary_key=raw_key,
                strategy=REBUILD_STRATEGY_SHADOW,
            ),
        ),
        steps=(),
        prepared_shadow_objects=(),
        warnings=(),
    )
    warning_client: FakeReplaySourceWarningClient = FakeReplaySourceWarningClient(
        replay_source_row_count=test_case.replay_source_row_count,
        active_row_count=test_case.active_row_count,
    )
    client: AdapterConnection = cast(AdapterConnection, warning_client)
    catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(
            adapter=AdapterIdentity(name="clickhouse"),
            database="flights_demo",
        ),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(name=raw_key.name, engine="MergeTree", columns=()),
            CatalogRelation(name=target_key.name, engine="View", columns=()),
        ),
    )

    updated_plan: DeploymentPlan = add_empty_replay_source_warnings(
        client=client,
        catalog=catalog,
        database="flights_demo",
        desired_state=desired_state,
        plan=plan,
    )

    assert len(updated_plan.warnings) == 1
    assert updated_plan.warnings[0].warning_code == "empty_replay_source"
    assert test_case.expected_warning_message_fragment in updated_plan.warnings[0].message
    assert warning_client.query_count == test_case.expected_point_in_time_query_count


class FakeReplaySourceWarningClient:
    def __init__(self, *, replay_source_row_count: int, active_row_count: int | None) -> None:
        self.query_count: int = 0
        self._query_results: Iterator[AdapterQueryResult] = iter(
            (
                AdapterQueryResult(rows=((replay_source_row_count,),)),
                AdapterQueryResult(rows=((active_row_count,),)),
            )
        )

    def query(self, statement: str) -> AdapterQueryResult:
        _ = statement
        self.query_count += 1
        return next(self._query_results)
