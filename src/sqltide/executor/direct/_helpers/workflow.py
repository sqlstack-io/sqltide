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
    CatalogRelation,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    DesiredView,
    ObjectKey,
)
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.planner.models import (
    DirectPlanEntry,
    DirectWarehouseSnapshot,
)
from sqltide.compiler.planner.types import DirectResourceKind
from sqltide.executor.auditing.constants import AUDIT_SAMPLE_LIMIT
from sqltide.executor.direct._helpers.population_plan import build_direct_population_plan
from sqltide.executor.direct._helpers.preflight import reject_incapable_adapter
from sqltide.executor.direct._helpers.sources import plan_preserved_managed_sources
from sqltide.executor.direct.exceptions import DirectBuildError
from sqltide.executor.direct.models import (
    DirectBuildRequest,
    DirectBuildWorkflow,
    DirectReplayCapture,
    DirectReplayRange,
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
    replay_by_model_name: dict[str, AdapterReplayRequest] = {
        _model_name_for_target(request=request, target=replay.relations.target): replay
        for replay in replay_templates
    }
    _assert_bounded_replay_inputs(
        request=request,
        snapshot=snapshot,
        replay_by_model_name=replay_by_model_name,
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
        if f"swap_source_{_step_segment(recreation.managed_source.resource.name)}" in step_ids
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

    replay: AdapterReplayRequest = replace(
        runtime_replay.replay,
        boundaries=_captured_boundaries(capture=capture),
    )
    rendered_sql: str = client.render_replay_from_capture(
        AdapterCapturedReplayRequest(
            replay=replay,
            boundary_column_type=runtime_replay.boundary_column_type,
            lower_bounds=_captured_lower_bounds(capture=capture),
        )
    )
    return replace(
        template_statement,
        sql=_terminate_sql(rendered_sql),
        replay_offset_progress=_replay_offset_progress_request(replay=replay, capture=capture),
    )


def _replay_offset_progress_request(
    *, replay: AdapterReplayRequest, capture: DirectReplayCapture
) -> AdapterReplayOffsetProgressRequest | None:
    if capture.boundary_mode != AdapterReplayBoundaryMode.OFFSETS:
        return None
    bounds_by_partition: dict[int, tuple[int, int]] = {}
    for replay_range in capture.ranges:
        partition: int = int(_required_partition(replay_range=replay_range))
        lower_offset: int = int(replay_range.lower_value)
        upper_offset: int = int(replay_range.replay_cutoff_value)
        previous: tuple[int, int] | None = bounds_by_partition.get(partition)
        bounds_by_partition[partition] = (
            lower_offset if previous is None else min(previous[0], lower_offset),
            upper_offset if previous is None else max(previous[1], upper_offset),
        )
    ranges: tuple[AdapterReplayOffsetRange, ...] = tuple(
        AdapterReplayOffsetRange(
            partition=partition,
            lower_offset=bounds_by_partition[partition][0],
            upper_offset=bounds_by_partition[partition][1],
        )
        for partition in sorted(bounds_by_partition)
    )
    if not ranges:
        return None
    return AdapterReplayOffsetProgressRequest(
        database=replay.database,
        relation=replay.relations.target,
        partition_column=replay.columns.partition,
        offset_column=replay.columns.offset,
        ranges=ranges,
    )


def _required_partition(*, replay_range: DirectReplayRange) -> str:
    if replay_range.partition_value is None:
        raise DirectBuildError("Captured offset replay range has no partition")
    return replay_range.partition_value


def _runtime_replays(
    *,
    request: DirectBuildRequest,
    snapshot: DirectWarehouseSnapshot,
    replay_templates: tuple[AdapterReplayRequest, ...],
) -> tuple[DirectRuntimeReplay, ...]:
    runtime_replays: list[DirectRuntimeReplay] = []
    replay: AdapterReplayRequest
    for replay in replay_templates:
        model_name: str = _model_name_for_target(request=request, target=replay.relations.target)
        segment: str = _step_segment(model_name)
        runtime_replays.append(
            DirectRuntimeReplay(
                model_name=model_name,
                capture_step_id=f"capture_replay_{segment}",
                replay_step_id=f"replay_{segment}",
                replay=replay,
                boundary_column_type=_boundary_column_type(
                    request=request,
                    replay=replay,
                    snapshot=snapshot,
                ),
            )
        )
    return tuple(runtime_replays)


def _captured_boundaries(*, capture: DirectReplayCapture) -> tuple[AdapterReplayBoundary, ...]:
    if capture.boundary_mode != AdapterReplayBoundaryMode.OFFSETS:
        return tuple(
            AdapterReplayBoundary(
                boundary_key=f"_replay_{capture.boundary_mode}",
                cutoff_value=replay_range.replay_cutoff_value,
                cutoff_inclusive=replay_range.cutoff_inclusive,
            )
            for replay_range in capture.ranges[:1]
        )
    cutoff_by_partition: dict[str, int] = {}
    for replay_range in capture.ranges:
        if replay_range.partition_value is None:
            raise DirectBuildError("Captured offset replay range has no partition")
        cutoff_value: int = int(replay_range.replay_cutoff_value)
        cutoff_by_partition[replay_range.partition_value] = max(
            cutoff_value,
            cutoff_by_partition.get(replay_range.partition_value, cutoff_value),
        )
    return tuple(
        AdapterReplayBoundary(
            boundary_key=f"_replay_partition={partition_value}",
            cutoff_value=str(cutoff_by_partition[partition_value]),
            cutoff_inclusive=True,
            partition_value=partition_value,
        )
        for partition_value in sorted(cutoff_by_partition, key=_partition_order)
    )


def _captured_lower_bounds(*, capture: DirectReplayCapture) -> tuple[AdapterReplayLowerBound, ...]:
    if not capture.ranges:
        return ()
    if capture.boundary_mode != AdapterReplayBoundaryMode.OFFSETS:
        return (AdapterReplayLowerBound(value=capture.ranges[0].lower_value),)
    lower_by_partition: dict[str, int] = {}
    for replay_range in capture.ranges:
        if replay_range.partition_value is None:
            raise DirectBuildError("Captured offset replay range has no partition")
        lower_value: int = int(replay_range.lower_value)
        lower_by_partition[replay_range.partition_value] = min(
            lower_value,
            lower_by_partition.get(replay_range.partition_value, lower_value),
        )
    return tuple(
        AdapterReplayLowerBound(
            value=str(lower_by_partition[partition_value]),
            partition_value=partition_value,
        )
        for partition_value in sorted(lower_by_partition, key=_partition_order)
    )


def _partition_order(value: str) -> int:
    return int(value)


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
            sql=_terminate_sql(sql),
        )
        for index, sql in enumerate(rendered, start=1)
    )


def _assert_bounded_replay_inputs(
    *,
    request: DirectBuildRequest,
    snapshot: DirectWarehouseSnapshot,
    replay_by_model_name: dict[str, AdapterReplayRequest],
) -> None:
    if request.effective_start_time is None:
        return
    model_name: str
    replay: AdapterReplayRequest
    for model_name, replay in replay_by_model_name.items():
        relation: CatalogRelation | None = snapshot.catalog.relation(replay.relations.anchor)
        if relation is None:
            raise DirectBuildError(
                f"Direct --start-time requires existing replay input "
                f"'{replay.relations.anchor}' for model '{model_name}'; run an ordinary direct "
                "build first to create managed sources and retained lineage."
            )
        time_column: str = _forced_time_column(replay=replay)
        if time_column not in {column.name for column in relation.columns}:
            raise DirectBuildError(
                f"Direct --start-time cannot bound model '{model_name}' because replay input "
                f"'{replay.relations.anchor}' does not expose time-lineage column "
                f"'{time_column}'. Project replay timestamp or landed-at lineage through the "
                "intermediate model before selecting this closure."
            )


def _forced_time_column(*, replay: AdapterReplayRequest) -> str:
    return {
        AdapterReplayBoundaryMode.OFFSETS: replay.columns.landed_at or replay.columns.timestamp,
        AdapterReplayBoundaryMode.CURSOR: replay.columns.timestamp,
        AdapterReplayBoundaryMode.TIMESTAMP: replay.columns.timestamp,
        AdapterReplayBoundaryMode.LANDED_AT: replay.columns.landed_at,
    }[replay.mode]


def _boundary_column_type(
    *,
    request: DirectBuildRequest,
    replay: AdapterReplayRequest,
    snapshot: DirectWarehouseSnapshot,
) -> str | None:
    if replay.mode == AdapterReplayBoundaryMode.OFFSETS:
        return None
    position_column: str = {
        AdapterReplayBoundaryMode.CURSOR: replay.columns.cursor,
        AdapterReplayBoundaryMode.TIMESTAMP: replay.columns.timestamp,
        AdapterReplayBoundaryMode.LANDED_AT: replay.columns.landed_at,
    }[replay.mode]
    desired: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    for desired in request.realized_project.desired_state.objects:
        if isinstance(desired, DesiredTable) and desired.name == replay.relations.anchor:
            return next(column.type for column in desired.columns if column.name == position_column)
    relation: CatalogRelation | None = snapshot.catalog.relation(replay.relations.anchor)
    if relation is not None:
        return next(column.type for column in relation.columns if column.name == position_column)
    raise DirectBuildError(
        f"Cannot resolve boundary type for {replay.relations.anchor}.{position_column}"
    )


def _model_name_for_target(*, request: DirectBuildRequest, target: str) -> str:
    return next(
        entry.model_key.name for entry in request.plan.entries if target in entry.relation_names
    )


def _retired_relation_names(*, request: DirectBuildRequest) -> tuple[str, ...]:
    current_names: frozenset[str] = frozenset(_entry_relation_names(request=request))
    return tuple(
        operation.relation_name
        for operation in request.plan.teardown_operations
        if operation.relation_name not in current_names
    )


def _terminate_sql(sql: str) -> str:
    return f"{sql.rstrip().rstrip(';')};"


def _escape_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _step_segment(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


def _entry_relation_names(*, request: DirectBuildRequest) -> tuple[str, ...]:
    names: list[str] = []
    entry: DirectPlanEntry
    for entry in request.plan.entries:
        names.extend(entry.relation_names)
    return tuple(names)
