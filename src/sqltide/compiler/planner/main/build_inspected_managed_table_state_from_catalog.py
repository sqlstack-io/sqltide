"""Build managed-table inspection from an immutable catalog snapshot."""

import sqltide._native as _native
from sqltide.adapter.models import (
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)


def build_inspected_managed_table_state_from_catalog(
    *, catalog: CatalogSnapshot
) -> InspectedManagedTableState:
    """Build managed-table inspection without additional warehouse reads."""

    database: str = catalog.identity.database
    bindings, candidates = _native.managed_catalog_relations(
        [
            (relation.name, relation.query_sql is not None, relation.stable_binding_name)
            for relation in catalog.relations
        ]
    )
    return InspectedManagedTableState(
        active_bindings=tuple(
            InspectedActiveTableBinding(
                database=database,
                logical_name=logical_name,
                physical_name=physical_name,
            )
            for logical_name, physical_name in bindings
        ),
        physical_candidates=tuple(
            InspectedPhysicalTableCandidate(
                database=database,
                logical_name=logical_name,
                physical_name=physical_name,
                object_type="view" if is_view else "table",
            )
            for logical_name, physical_name, is_view in candidates
        ),
    )
