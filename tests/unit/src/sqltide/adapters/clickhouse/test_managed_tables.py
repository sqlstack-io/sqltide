from collections.abc import Callable, Mapping
from typing import cast

import pytest

from sqltide._native import (
    build_inspected_managed_table_state,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import InspectedManagedTableState
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    BuildInspectedManagedTableStateTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BuildInspectedManagedTableStateTestCase(
            description=(
                "ignores materialized view deployment candidates in managed table inspection"
            ),
            active_binding_rows=(),
            system_rows=(
                ("tbl__orders_enriched__20260726T180000Z_depa01", "MergeTree"),
                ("raw__orders__20260726T180000Z_depa01", "MergeTree"),
                ("mv__orders_enriched__20260726T180000Z_depa01", "MaterializedView"),
            ),
            expected_logical_names=("tbl__orders_enriched", "raw__orders"),
            expected_active_bindings=(),
        ),
        BuildInspectedManagedTableStateTestCase(
            description="parses quoted dotted stable bindings without textual splitting",
            active_binding_rows=(
                (
                    "tbl__orders_enriched",
                    "select * from `analytics-db`.`tbl__orders.with.dot`",
                ),
            ),
            system_rows=(),
            expected_logical_names=(),
            expected_active_bindings=(("tbl__orders_enriched", "tbl__orders.with.dot"),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_physical_candidates_when_building_inspected_state_then_it_ignores_mv_candidates(
    test_case: BuildInspectedManagedTableStateTestCase,
) -> None:
    class QueryingClient:
        def __init__(self) -> None:
            active_binding_rows: tuple[Mapping[str, object], ...] = tuple(
                {"name": name, "as_select": as_select}
                for name, as_select in test_case.active_binding_rows
            )
            physical_candidate_rows: tuple[Mapping[str, object], ...] = tuple(
                {"name": name} for name, _engine in test_case.system_rows
            )
            self.response_rows = iter((active_binding_rows, physical_candidate_rows))

        def query_many(
            self,
            *,
            statement: str,
            decode: Callable[[Mapping[str, object]], object],
        ) -> tuple[object, ...]:
            _ = statement
            return tuple(decode(row) for row in next(self.response_rows))

    inspected_state: InspectedManagedTableState = build_inspected_managed_table_state(
        client=cast(AdapterConnection, QueryingClient()),
        database="analytics",
    )

    assert tuple(candidate.logical_name for candidate in inspected_state.physical_candidates) == (
        test_case.expected_logical_names
    )
    assert (
        tuple(
            (binding.logical_name, binding.physical_name)
            for binding in inspected_state.active_bindings
        )
        == test_case.expected_active_bindings
    )
