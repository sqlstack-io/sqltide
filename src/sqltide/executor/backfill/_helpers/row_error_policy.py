"""Apply row-error policy changes to a live virtual deployment's views."""

from itertools import chain

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import CatalogRelation, CatalogSnapshot
from sqltide.compiler.compile.models import DesiredMaterializedView, DesiredTable, ObjectKey
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.planner.main.build_shadow_adapter_resource import (
    build_shadow_adapter_resource,
)
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange
from sqltide.executor.backfill.models import BackfillBootstrapRequest
from sqltide.executor.population.main._row_error_policy_restore_statements import (
    row_error_policy_restore_statements,
)
from sqltide.executor.population.main._row_error_policy_view_steps import (
    row_error_policy_view_steps,
)
from sqltide.executor.population.main._with_metadata_dead_letter_target import (
    with_metadata_dead_letter_target,
)
from sqltide.executor.workflow.types import NativeStatement

_AUDIT_SAMPLE_LIMIT: int = 0


def virtual_row_error_policy_statements(
    *,
    request: BackfillBootstrapRequest,
    client: AdapterConnection,
    target_catalog: CatalogSnapshot,
) -> tuple[list[NativeStatement], list[NativeStatement]]:
    """Return pause, view swap and resume statements for policy changes, and their recovery."""

    changes: tuple[DirectRowErrorPolicyChange, ...] = request.row_error_policy_changes
    views: dict[str, DesiredMaterializedView] = {
        desired.name: desired
        for desired in request.desired_state.objects
        if isinstance(desired, DesiredMaterializedView)
    }
    physical_table_by_key: dict[ObjectKey, str] = _active_physical_tables(
        request=request, target_catalog=target_catalog
    )
    physical_by_view: dict[str, str] = {}
    for change in changes:
        physical_by_view[change.logical_view_name] = change.view_name
        physical_by_view[change.logical_dead_letter_view_name] = change.dead_letter_view_name

    def render_view(logical_name: str) -> str | None:
        view: DesiredMaterializedView | None = views.get(logical_name)
        if view is None:
            return None
        return client.render_resource(
            resource=with_metadata_dead_letter_target(
                resource=build_shadow_adapter_resource(
                    desired_object=view,
                    physical_name=physical_by_view[logical_name],
                    physical_name_by_key=physical_table_by_key,
                ),
                database=request.default_database,
                metadata_database=request.metadata_database,
            ),
            database=view.key.database or request.default_database,
        )

    teardown, realizations = row_error_policy_view_steps(
        changes=changes,
        database=request.default_database,
        render_view=render_view,
    )
    landing_names: list[str] = sorted(
        set(chain.from_iterable(change.landing_view_names for change in changes))
    )
    activations: list[tuple[str, str]] = [
        (
            name,
            client.render_resource(
                resource=build_adapter_resource(views[name]),
                database=views[name].key.database or request.default_database,
                if_not_exists=True,
            ),
        )
        for name in landing_names
    ]
    migrations: list[tuple[str, str]] = [
        (f"metadata_{position}", statement)
        for position, statement in enumerate(
            client.render_migrate_metadata_state(request.metadata_database), start=1
        )
    ]
    statements, recovery = _native.direct_workflow_statements(
        (
            request.default_database,
            client.render_ensure_database(request.default_database),
            migrations,
            [],
            landing_names,
            [],
            activations,
        ),
        (
            float(request.stabilization_seconds),
            teardown,
            realizations,
            [],
            [],
            _AUDIT_SAMPLE_LIMIT,
        ),
    )
    restores: list[NativeStatement] = row_error_policy_restore_statements(
        changes=changes, render_restore=client.render_restore_materialized_view
    )
    return statements, [*restores, *recovery]


def _active_physical_tables(
    *, request: BackfillBootstrapRequest, target_catalog: CatalogSnapshot
) -> dict[ObjectKey, str]:
    physical: dict[ObjectKey, str] = {}
    for desired in request.desired_state.objects:
        if isinstance(desired, DesiredTable):
            relation: CatalogRelation | None = target_catalog.relation(desired.name)
            if relation is not None and relation.stable_binding_name is not None:
                physical[desired.key] = relation.stable_binding_name
    return physical
