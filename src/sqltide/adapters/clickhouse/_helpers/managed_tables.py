"""Managed-table inspection helpers for ClickHouse live state."""

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.adapters.clickhouse._helpers.catalog_parsing import extract_stable_binding
from sqltide.adapters.clickhouse.constants import CLICKHOUSE_VIEW_ENGINE
from sqltide.adapters.clickhouse.models import (
    ActiveBindingSystemRow,
    PhysicalCandidateSystemRow,
)


def build_inspected_managed_table_state(
    *,
    client: AdapterConnection,
    database: str,
) -> InspectedManagedTableState:
    """Build inspected managed-table state from ClickHouse system metadata."""

    active_binding_rows: tuple[ActiveBindingSystemRow, ...] = client.query_many(
        statement="SELECT name, as_select FROM system.tables "
        f"WHERE database = '{database}' AND engine = 'View'",
        decode=_decode_active_binding_system_row,
    )
    physical_candidate_rows: tuple[PhysicalCandidateSystemRow, ...] = client.query_many(
        statement=(
            f"SELECT name FROM system.tables WHERE database = '{database}' AND engine != 'View'"
        ),
        decode=_decode_physical_candidate_system_row,
    )

    active_bindings: list[InspectedActiveTableBinding] = []
    physical_candidates: list[InspectedPhysicalTableCandidate] = []
    row: ActiveBindingSystemRow
    for row in active_binding_rows:
        logical_name: str | None = _native.deployment_candidate_logical_name(row.name)
        if logical_name is not None:
            physical_candidates.append(
                InspectedPhysicalTableCandidate(
                    database=database,
                    logical_name=logical_name,
                    physical_name=row.name,
                    object_type="view",
                )
            )
            continue
        physical_name: str | None = extract_stable_binding(
            engine=CLICKHOUSE_VIEW_ENGINE,
            as_select=row.as_select,
        )
        if physical_name is None:
            continue
        active_bindings.append(
            InspectedActiveTableBinding(
                database=database,
                logical_name=row.name,
                physical_name=physical_name,
            )
        )

    physical_candidate_row: PhysicalCandidateSystemRow
    for physical_candidate_row in physical_candidate_rows:
        candidate_logical_name: str | None = _native.deployment_candidate_logical_name(
            physical_candidate_row.name
        )
        if candidate_logical_name is None:
            continue
        physical_candidates.append(
            InspectedPhysicalTableCandidate(
                database=database,
                logical_name=candidate_logical_name,
                physical_name=physical_candidate_row.name,
                object_type="table",
            )
        )

    return InspectedManagedTableState(
        active_bindings=tuple(active_bindings),
        physical_candidates=tuple(physical_candidates),
    )


def _decode_active_binding_system_row(row: Mapping[str, object]) -> ActiveBindingSystemRow:
    return ActiveBindingSystemRow(
        name=str(row["name"]),
        as_select=str(row["as_select"]),
    )


def _decode_physical_candidate_system_row(
    row: Mapping[str, object],
) -> PhysicalCandidateSystemRow:
    return PhysicalCandidateSystemRow(name=str(row["name"]))
