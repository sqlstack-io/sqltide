"""Assemble the complete authoritative direct build workflow."""

from __future__ import annotations

from dataclasses import replace

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterCapturedReplayRequest,
    AdapterMaterializedView,
    AdapterReplayBoundary,
    AdapterReplayCoverageRequest,
    AdapterReplayLowerBound,
    AdapterReplayOffsetProgressRequest,
    AdapterReplayOffsetRange,
    AdapterReplayRequest,
    CatalogColumn,
)
from sqltide.compiler.compile.models import (
    Column,
    DesiredMaterializedView,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.planner.models import (
    DirectWarehouseSnapshot,
)
from sqltide.compiler.planner.types import DirectResourceKind
from sqltide.executor.auditing.constants import AUDIT_SAMPLE_LIMIT
from sqltide.executor.direct._helpers.population_plan import build_direct_population_plan
from sqltide.executor.direct._helpers.preflight import reject_incapable_adapter
from sqltide.executor.direct._helpers.sources import plan_preserved_managed_sources
from sqltide.executor.direct.constants import (
    DIRECT_REPLAY_BUILD_ERROR,
    MISSING_REPLAY_PARTITION_ERROR,
)
from sqltide.executor.direct.exceptions import DirectBuildError
from sqltide.executor.direct.models import (
    DirectBuildRequest,
    DirectBuildWorkflow,
    DirectReplayCapture,
    DirectRuntimeReplay,
)
from sqltide.executor.population.main._build_population_replay_templates import (
    build_population_replay_templates,
)
from sqltide.executor.population.main._expand_population_plan import expand_population_plan
from sqltide.executor.population.main._plan_population_objects import plan_population_objects
from sqltide.executor.population.models import (
    PopulationManagedSource,
    PopulationPlan,
    PopulationRealization,
    PopulationSourcePreparation,
    PopulationSourceRecreation,
)
from sqltide.executor.workflow.models import BuildWorkflow, WarehouseStatement
from sqltide.executor.workflow.types import (
    NativeStatement,
    StatementIntent,
    WorkflowMode,
    WorkflowPhase,
)


def assemble_direct_build_workflow(
    *,
    request: DirectBuildRequest,
    client: AdapterConnection,
    snapshot: DirectWarehouseSnapshot,
    plan_json: str,
) -> DirectBuildWorkflow:
    """Resolve preconditions and assemble every direct lifecycle statement once."""

    reject_incapable_adapter(client=client)
    source_preparation: PopulationSourcePreparation
    source_realizations: tuple[PopulationRealization, ...]
    source_preparation, source_realizations = plan_preserved_managed_sources(
        plan=request.plan,
        realized_project=request.realized_project,
        catalog=snapshot.catalog,
        database=request.database,
    )
    preserved_landing_views: tuple[DesiredMaterializedView, ...] = tuple(
        desired
        for desired in request.realized_project.desired_state.objects
        if isinstance(desired, DesiredMaterializedView)
        and desired.name in source_preparation.preserved_relation_names
    )
    population_plan: PopulationPlan = expand_population_plan(
        plan=build_direct_population_plan(
            plan=request.plan,
            realized_project=request.realized_project,
        ),
        desired_state=request.realized_project.desired_state,
    )
    realizations: tuple[PopulationRealization, ...] = plan_population_objects(
        plan=population_plan,
        desired_state=request.realized_project.desired_state,
        default_database=request.database,
    )
    replay_template_pairs: tuple[tuple[ObjectKey, AdapterReplayRequest], ...] = (
        build_population_replay_templates(
            plan=population_plan,
            desired_state=request.realized_project.desired_state,
            default_database=request.database,
        )
    )
    replay_templates: tuple[AdapterReplayRequest, ...] = tuple(
        template for _key, template in replay_template_pairs
    )
    runtime_replays: tuple[DirectRuntimeReplay, ...] = _runtime_replays(
        request=request,
        snapshot=snapshot,
        replay_templates=replay_templates,
    )
    statements: tuple[WarehouseStatement, ...]
    source_recovery_statements: tuple[WarehouseStatement, ...]
    statements, source_recovery_statements = _assemble_statements(
        request=request,
        client=client,
        source_preparation=source_preparation,
        source_realizations=source_realizations,
        preserved_landing_views=preserved_landing_views,
        realizations=realizations,
        runtime_replays=runtime_replays,
    )
    step_ids: frozenset[str] = frozenset(statement.step_id for statement in statements)
    recreated_sources: tuple[PopulationSourceRecreation, ...] = tuple(
        recreation
        for recreation in source_preparation.recreated_sources
        if f"swap_source_{_native.workflow_step_segment(recreation.managed_source.resource.name)}"
        in step_ids
    )
    return DirectBuildWorkflow(
        template=BuildWorkflow(
            mode=WorkflowMode.DIRECT,
            plan_json=plan_json,
            statements=statements,
        ),
        runtime_replays=runtime_replays,
        workflow_id=request.workflow_id,
        source_recovery_statements=source_recovery_statements,
        recreated_sources=recreated_sources,
    )


def _assemble_statements(
    *,
    request: DirectBuildRequest,
    client: AdapterConnection,
    source_preparation: PopulationSourcePreparation,
    source_realizations: tuple[PopulationRealization, ...],
    preserved_landing_views: tuple[DesiredMaterializedView, ...],
    realizations: tuple[PopulationRealization, ...],
    runtime_replays: tuple[DirectRuntimeReplay, ...],
) -> tuple[tuple[WarehouseStatement, ...], tuple[WarehouseStatement, ...]]:
    """Return the workflow statements and the statements that recover paused sources."""

    if not request.plan.execution_scope and not request.audits:
        return (), ()
    rendered_realizations: list[tuple[str, str]] = [
        (
            realization.resource.name,
            client.render_resource(resource=realization.resource, database=realization.database),
        )
        for realization in realizations
    ]
    ensure_database_sql: str = client.render_ensure_database(request.database)
    rendered_sources: list[tuple[str, str]] = [
        (
            realization.resource.name,
            client.render_resource(
                resource=realization.resource,
                database=realization.database,
                if_not_exists=True,
            ),
        )
        for realization in source_realizations
    ]
    recreations: list[tuple[str, str, str, str, str]] = [
        _native_recreation(client=client, recreation=recreation)
        for recreation in source_preparation.recreated_sources
    ]
    bootstrapped: list[tuple[str, str]] = _rendered_landing_views(
        request=request, client=client, landing_views=source_preparation.landing_views
    )
    coverage: list[tuple[str, str, str, str]] = [
        (
            runtime_replay.model_name,
            runtime_replay.capture_step_id,
            runtime_replay.replay_step_id,
            client.render_replay_coverage_query(
                AdapterReplayCoverageRequest(
                    replay=runtime_replay.replay,
                    boundary_column_type=runtime_replay.boundary_column_type,
                )
            ),
        )
        for runtime_replay in runtime_replays
    ]
    activations: list[tuple[str, str]] = _rendered_landing_views(
        request=request,
        client=client,
        landing_views=(*preserved_landing_views, *source_preparation.landing_views),
    )
    statements: list[NativeStatement]
    recovery: list[NativeStatement]
    statements, recovery = _native.direct_workflow_statements(
        (
            request.database,
            ensure_database_sql,
            rendered_sources,
            recreations,
            [landing_view.name for landing_view in preserved_landing_views],
            bootstrapped,
            activations,
        ),
        (
            float(request.stabilization_seconds),
            [
                (operation.relation_name, operation.resource_kind == DirectResourceKind.VIEW)
                for operation in request.plan.teardown_operations
            ],
            rendered_realizations,
            coverage,
            [(audit.name, audit.query) for audit in request.audits],
            AUDIT_SAMPLE_LIMIT,
        ),
    )
    return _warehouse_statements(statements), _warehouse_statements(recovery)


def _warehouse_statements(statements: list[NativeStatement]) -> tuple[WarehouseStatement, ...]:
    return tuple(
        WarehouseStatement(
            sequence=sequence,
            step_id=step_id,
            phase=WorkflowPhase(phase),
            intent=StatementIntent(intent),
            sql=sql,
            continue_on_error=continue_on_error,
        )
        for sequence, (step_id, phase, intent, sql, continue_on_error) in enumerate(
            statements, start=1
        )
    )


def _native_recreation(
    *, client: AdapterConnection, recreation: PopulationSourceRecreation
) -> tuple[str, str, str, str, str]:
    source: PopulationManagedSource = recreation.managed_source
    return (
        source.resource.name,
        source.database,
        recreation.staged_relation_name,
        recreation.landing_view_name,
        client.render_resource(
            resource=replace(source.resource, name=recreation.staged_relation_name),
            database=source.database,
        ),
    )


def _rendered_landing_views(
    *,
    request: DirectBuildRequest,
    client: AdapterConnection,
    landing_views: tuple[DesiredMaterializedView, ...],
) -> list[tuple[str, str]]:
    return [
        (
            landing_view.name,
            _render_landing_view_sql(request=request, client=client, landing_view=landing_view),
        )
        for landing_view in landing_views
    ]


def _render_landing_view_sql(
    *,
    request: DirectBuildRequest,
    client: AdapterConnection,
    landing_view: DesiredMaterializedView,
) -> str:
    built_resource: object = build_adapter_resource(landing_view)
    if not isinstance(built_resource, AdapterMaterializedView):
        raise DirectBuildError(
            f"Landing view '{landing_view.name}' did not realize as a materialized view"
        )
    return client.render_resource(
        resource=built_resource,
        database=landing_view.key.database or request.database,
        if_not_exists=True,
    )


def realize_direct_replay_statement(
    *,
    template_statement: WarehouseStatement,
    runtime_replay: DirectRuntimeReplay,
    capture: DirectReplayCapture,
    client: AdapterConnection,
) -> WarehouseStatement:
    """Replace one direct replay template with SQL from its in-memory capture."""

    boundaries: list[tuple[str, str, bool, str | None]]
    lower_bounds: list[tuple[str, str | None]]
    offset_ranges: list[tuple[int, int, int]]
    try:
        boundaries, lower_bounds, offset_ranges = _native.realize_direct_replay_capture(
            str(capture.boundary_mode),
            [
                (
                    replay_range.partition_value,
                    replay_range.lower_value,
                    replay_range.replay_cutoff_value,
                    replay_range.cutoff_inclusive,
                )
                for replay_range in capture.ranges
            ],
        )
    except ValueError as error:
        reason: tuple[str, ...] = error.args
        if reason[0] == MISSING_REPLAY_PARTITION_ERROR:
            raise DirectBuildError("Captured offset replay range has no partition") from None
        int(reason[1])
        raise
    replay: AdapterReplayRequest = replace(
        runtime_replay.replay,
        boundaries=tuple(
            AdapterReplayBoundary(
                boundary_key=boundary_key,
                cutoff_value=cutoff_value,
                cutoff_inclusive=cutoff_inclusive,
                partition_value=partition_value,
            )
            for boundary_key, cutoff_value, cutoff_inclusive, partition_value in boundaries
        ),
    )
    rendered_sql: str = client.render_replay_from_capture(
        AdapterCapturedReplayRequest(
            replay=replay,
            boundary_column_type=runtime_replay.boundary_column_type,
            lower_bounds=tuple(
                AdapterReplayLowerBound(value=value, partition_value=partition_value)
                for value, partition_value in lower_bounds
            ),
        )
    )
    return replace(
        template_statement,
        sql=_native.workflow_terminate_sql(rendered_sql),
        replay_offset_progress=(
            AdapterReplayOffsetProgressRequest(
                database=replay.database,
                relation=replay.relations.target,
                partition_column=replay.columns.partition,
                offset_column=replay.columns.offset,
                ranges=tuple(
                    AdapterReplayOffsetRange(
                        partition=partition, lower_offset=lower_offset, upper_offset=upper_offset
                    )
                    for partition, lower_offset, upper_offset in offset_ranges
                ),
            )
            if offset_ranges
            else None
        ),
    )


def _runtime_replays(
    *,
    request: DirectBuildRequest,
    snapshot: DirectWarehouseSnapshot,
    replay_templates: tuple[AdapterReplayRequest, ...],
) -> tuple[DirectRuntimeReplay, ...]:
    resolved: list[tuple[str, str, str, str | None]]
    try:
        resolved = _native.resolve_direct_replays(
            [(entry.model_key.name, list(entry.relation_names)) for entry in request.plan.entries],
            [
                (
                    str(replay.mode),
                    replay.relations.anchor,
                    replay.relations.target,
                    replay.columns.timestamp,
                    replay.columns.landed_at,
                    replay.columns.cursor,
                )
                for replay in replay_templates
            ],
            request.effective_start_time is not None,
            [
                (desired.name, _native_columns(desired.columns))
                for desired in request.realized_project.desired_state.objects
                if isinstance(desired, DesiredTable)
            ],
            [
                (relation.name, _native_columns(relation.columns))
                for relation in snapshot.catalog.relations
            ],
        )
    except ValueError as error:
        reason: tuple[str, ...] = error.args
        if reason[0] == DIRECT_REPLAY_BUILD_ERROR:
            raise DirectBuildError(reason[1]) from None
        raise StopIteration from None
    return tuple(
        DirectRuntimeReplay(
            model_name=model_name,
            capture_step_id=capture_step_id,
            replay_step_id=replay_step_id,
            replay=replay,
            boundary_column_type=boundary_column_type,
        )
        for replay, (model_name, capture_step_id, replay_step_id, boundary_column_type) in zip(
            replay_templates, resolved, strict=True
        )
    )


def _native_columns(
    columns: tuple[Column, ...] | tuple[CatalogColumn, ...],
) -> list[tuple[str, str]]:
    return [(column.name, column.type) for column in columns]


def assemble_direct_fingerprint_statements(
    *, rendered: tuple[str, ...]
) -> tuple[WarehouseStatement, ...]:
    """Wrap optional logical fingerprint SQL for the mutation gateway."""

    return tuple(
        WarehouseStatement(
            sequence=index,
            step_id=f"record_direct_fingerprint_{index}",
            phase=WorkflowPhase.FINALIZATION,
            intent=StatementIntent.MUTATION,
            sql=_native.workflow_terminate_sql(sql),
        )
        for index, sql in enumerate(rendered, start=1)
    )
