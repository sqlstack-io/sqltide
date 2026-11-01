"""Assemble the authoritative virtual build workflow."""

from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import (
    METADATA_DEPLOYMENTS_TABLE_NAME,
    METADATA_PUBLISH_HISTORY_TABLE_NAME,
)
from sqltide.adapter.models import (
    AdapterDeploymentRecord,
    AdapterDeploymentReplayRequest,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterReplayRequest,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapter.types import AdapterReplayBoundaryMode
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    ExternalSourceReplayConfig,
    ObjectKey,
)
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.main.build_adapter_resource import build_adapter_resource
from sqltide.compiler.planner.models import DeploymentPlan
from sqltide.executor.backfill._helpers.metadata import build_deployment_metadata_state
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.backfill.models import BackfillBootstrapRequest
from sqltide.executor.backfill.types import NativeVirtualRoot
from sqltide.executor.population.main._build_population_replay_templates import (
    build_population_replay_templates,
)
from sqltide.executor.population.main._expand_population_plan import expand_population_plan
from sqltide.executor.population.main._plan_population_objects import plan_population_objects
from sqltide.executor.population.main.plan_population_sources import plan_population_sources
from sqltide.executor.population.models import (
    PopulationObject,
    PopulationPlan,
    PopulationRealization,
    PopulationRoot,
    PopulationSourcePreparation,
)
from sqltide.executor.workflow.models import BuildWorkflow, WarehouseStatement
from sqltide.executor.workflow.types import (
    NativeStatement,
    StatementIntent,
    WorkflowMode,
    WorkflowPhase,
)


def assemble_virtual_build_workflow(
    *, request: BackfillBootstrapRequest, client: AdapterConnection, plan_json: str
) -> BuildWorkflow:
    """Resolve preconditions and assemble every virtual build statement once."""

    deployment_plan: DeploymentPlan = _confirmed_plan(request)
    deployment_id: str = _confirmed_deployment_id(request)
    target_catalog: CatalogSnapshot = _confirmed_catalog(
        catalog=request.confirmed_target_catalog,
        catalog_name="target",
    )
    metadata_catalog: CatalogSnapshot = _confirmed_catalog(
        catalog=request.confirmed_metadata_catalog,
        catalog_name="metadata",
    )
    source_preparation: PopulationSourcePreparation
    source_realizations: tuple[PopulationRealization, ...]
    source_preparation, source_realizations = plan_population_sources(
        desired_state=request.desired_state,
        default_database=request.default_database,
        existing_relation_names=target_catalog.relation_names(),
    )
    population_plan: PopulationPlan = expand_population_plan(
        plan=_population_plan(
            deployment_plan=deployment_plan,
            execution_id=deployment_id,
            replay_lineage_mode=str(request.replay_lineage_mode),
        ),
        desired_state=request.desired_state,
    )
    replay_templates: tuple[tuple[ObjectKey, AdapterReplayRequest], ...] = (
        build_population_replay_templates(
            plan=population_plan,
            desired_state=request.desired_state,
            default_database=request.default_database,
        )
    )
    metadata_state: AdapterMetadataState = build_deployment_metadata_state(
        deployment_plan=deployment_plan,
        desired_objects=request.desired_state.objects,
        deployment_id=deployment_id,
        created_at=_created_at(request),
        replay_lineage_mode=ReplayLineageMode(request.replay_lineage_mode),
        workflow_fingerprint=sha256(plan_json.encode()).hexdigest(),
        tool_version=version("sqltide"),
    )
    statements: tuple[WarehouseStatement, ...] = _assemble_statements(
        request=request,
        client=client,
        target_catalog=target_catalog,
        metadata_catalog=metadata_catalog,
        metadata_state=metadata_state,
        source_preparation=source_preparation,
        source_realizations=source_realizations,
        population_plan=population_plan,
        replay_templates=replay_templates,
    )
    return BuildWorkflow(
        mode=WorkflowMode.VIRTUAL_ENVIRONMENT,
        plan_json=plan_json,
        statements=statements,
    )


def build_virtual_bootstrap_workflow(*, workflow: BuildWorkflow) -> BuildWorkflow:
    """Return the preflight and preparation prefix used by bootstrap tests."""

    statements: tuple[WarehouseStatement, ...] = tuple(
        statement
        for statement in workflow.statements
        if statement.phase in {WorkflowPhase.PREFLIGHT, WorkflowPhase.PREPARATION}
    )
    return BuildWorkflow(mode=workflow.mode, plan_json=workflow.plan_json, statements=statements)


def _assemble_statements(
    *,
    request: BackfillBootstrapRequest,
    client: AdapterConnection,
    target_catalog: CatalogSnapshot,
    metadata_catalog: CatalogSnapshot,
    metadata_state: AdapterMetadataState,
    source_preparation: PopulationSourcePreparation,
    source_realizations: tuple[PopulationRealization, ...],
    population_plan: PopulationPlan,
    replay_templates: tuple[tuple[ObjectKey, AdapterReplayRequest], ...],
) -> tuple[WarehouseStatement, ...]:
    metadata_names: frozenset[str] = metadata_catalog.relation_names()
    deployment: AdapterDeploymentRecord = metadata_state.deployments[0]
    prepared: list[tuple[str, str | None]] = [
        _prepared_relation(target_catalog=target_catalog, relation_name=name)
        for name in _prepared_relation_names(request)
    ]
    ensure_database_sql: str = client.render_ensure_database(request.default_database)
    migrate_metadata_sql: list[str] = list(
        client.render_migrate_metadata_state(request.metadata_database)
    )
    rendered_sources: list[tuple[str, str]] = _rendered_realizations(
        client=client, realizations=source_realizations
    )
    persist_metadata_sql: list[str] = list(
        client.render_persist_metadata_state(
            database=request.metadata_database,
            state=metadata_state,
        )
    )
    rendered_objects: list[tuple[str, str]] = _rendered_realizations(
        client=client,
        realizations=plan_population_objects(
            plan=population_plan,
            desired_state=request.desired_state,
            default_database=request.default_database,
        ),
    )
    rendered_landing_views: list[tuple[str, str]] = [
        (
            landing_view.name,
            _render_landing_view(request=request, client=client, landing_view=landing_view),
        )
        for landing_view in source_preparation.landing_views
    ]
    root_by_key: dict[ObjectKey, PopulationRoot] = {
        root.root_key: root for root in population_plan.roots
    }
    roots: list[NativeVirtualRoot] = [
        _native_root(
            request=request,
            client=client,
            target_catalog=target_catalog,
            root=root_by_key[root_key],
            replay=replay,
        )
        for root_key, replay in replay_templates
    ]
    native_statements: list[NativeStatement] = _native.virtual_workflow_statements(
        (
            request.metadata_database,
            request.default_database,
            _confirmed_deployment_id(request),
            (
                deployment.deployment_id,
                deployment.created_at,
                str(deployment.replay_lineage_mode),
                deployment.workflow_fingerprint,
                deployment.tool_version,
            ),
            request.boundary_time,
            METADATA_DEPLOYMENTS_TABLE_NAME in metadata_names,
            METADATA_PUBLISH_HISTORY_TABLE_NAME in metadata_names,
        ),
        (
            prepared,
            ensure_database_sql,
            migrate_metadata_sql,
            rendered_sources,
            persist_metadata_sql,
            rendered_objects,
            rendered_landing_views,
            float(request.stabilization_seconds),
        ),
        roots,
    )
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
            native_statements, start=1
        )
    )


def _prepared_relation(
    *, target_catalog: CatalogSnapshot, relation_name: str
) -> tuple[str, str | None]:
    relation: CatalogRelation | None = target_catalog.relation(relation_name)
    return (relation_name, None if relation is None else relation.definition_sql or "")


def _rendered_realizations(
    *, client: AdapterConnection, realizations: tuple[PopulationRealization, ...]
) -> list[tuple[str, str]]:
    return [
        (
            realization.resource.name,
            client.render_resource(
                resource=realization.resource,
                database=realization.database,
                if_not_exists=True,
            ),
        )
        for realization in realizations
    ]


def _render_landing_view(
    *,
    request: BackfillBootstrapRequest,
    client: AdapterConnection,
    landing_view: DesiredMaterializedView,
) -> str:
    built_resource: object = build_adapter_resource(landing_view)
    if not isinstance(built_resource, AdapterMaterializedView):
        raise BackfillExecutionError(
            f"Landing view '{landing_view.name}' did not realize as a materialized view"
        )
    return client.render_resource(
        resource=built_resource,
        database=landing_view.key.database or request.default_database,
        if_not_exists=True,
    )


def _native_root(
    *,
    request: BackfillBootstrapRequest,
    client: AdapterConnection,
    target_catalog: CatalogSnapshot,
    root: PopulationRoot,
    replay: AdapterReplayRequest,
) -> NativeVirtualRoot:
    rendered: tuple[str, ...] = client.render_replay_from_deployment(
        AdapterDeploymentReplayRequest(
            replay=replay,
            metadata_database=request.metadata_database,
            deployment_id=_confirmed_deployment_id(request),
            boundary_column_type=_boundary_column_type(
                desired_state=request.desired_state,
                target_catalog=target_catalog,
                replay=replay,
                anchor_key=root.upstream_boundary_key,
            ),
            active_relation_name=_active_relation_name(
                target_catalog=target_catalog,
                replay=replay,
            ),
            active_column_names=_active_column_names(
                target_catalog=target_catalog,
                replay=replay,
            ),
            anchor_column_names=_anchor_column_names(
                desired_state=request.desired_state,
                target_catalog=target_catalog,
                replay=replay,
                anchor_key=root.upstream_boundary_key,
            ),
        )
    )
    boundary_key: ObjectKey = root.upstream_boundary_key
    return (
        root.root_key.name,
        replay,
        root.persist_watermarks,
        (boundary_key.database, str(boundary_key.object_type), boundary_key.name),
        target_catalog.relation(replay.relations.anchor) is not None,
        list(rendered),
    )


def _population_plan(
    *, deployment_plan: DeploymentPlan, execution_id: str, replay_lineage_mode: str
) -> PopulationPlan:
    return PopulationPlan(
        execution_id=execution_id,
        roots=tuple(
            PopulationRoot(
                root_key=subtree.root_key,
                affected_keys=subtree.affected_keys,
                upstream_boundary_key=subtree.upstream_boundary_key,
                replay_lineage_mode=replay_lineage_mode,
                execution_mode=subtree.execution_mode,
                forced_start_time=subtree.forced_start_time,
                execution_lookback_seconds=subtree.execution_lookback_seconds,
            )
            for subtree in deployment_plan.rebuild_subtrees
            if subtree.replay_required
        ),
        objects=tuple(
            PopulationObject(
                logical_key=prepared.logical_key,
                physical_name=prepared.physical_name,
            )
            for prepared in deployment_plan.prepared_shadow_objects
        ),
    )


def _boundary_column_type(
    *,
    desired_state: DesiredState,
    target_catalog: CatalogSnapshot,
    replay: AdapterReplayRequest,
    anchor_key: ObjectKey,
) -> str | None:
    if replay.mode == AdapterReplayBoundaryMode.OFFSETS:
        return None
    position_column: str = {
        AdapterReplayBoundaryMode.CURSOR: replay.columns.cursor,
        AdapterReplayBoundaryMode.TIMESTAMP: replay.columns.timestamp,
        AdapterReplayBoundaryMode.LANDED_AT: replay.columns.landed_at,
    }[replay.mode]
    desired: DesiredTable
    for desired in (item for item in desired_state.objects if isinstance(item, DesiredTable)):
        if desired.key == anchor_key:
            return next(column.type for column in desired.columns if column.name == position_column)
    relation: CatalogRelation | None = target_catalog.relation(replay.relations.anchor)
    if relation is not None:
        return next(column.type for column in relation.columns if column.name == position_column)
    external: ExternalSourceReplayConfig
    for external in desired_state.external_source_replay_configs:
        if external.table_name == replay.relations.anchor:
            source_relation: CatalogRelation | None = target_catalog.relation(external.table_name)
            if source_relation is not None:
                return next(
                    column.type
                    for column in source_relation.columns
                    if column.name == position_column
                )
    return _boundary_type_expression(replay.mode)


def _active_column_names(
    *, target_catalog: CatalogSnapshot, replay: AdapterReplayRequest
) -> tuple[str, ...]:
    relation: CatalogRelation | None = target_catalog.relation(
        _active_relation_name(target_catalog=target_catalog, replay=replay)
    )
    return () if relation is None else tuple(column.name for column in relation.columns)


def _active_relation_name(*, target_catalog: CatalogSnapshot, replay: AdapterReplayRequest) -> str:
    relation: CatalogRelation | None = target_catalog.relation(replay.relations.root)
    if relation is None or relation.stable_binding_name is None:
        return replay.relations.root
    return relation.stable_binding_name


def _anchor_column_names(
    *,
    desired_state: DesiredState,
    target_catalog: CatalogSnapshot,
    replay: AdapterReplayRequest,
    anchor_key: ObjectKey,
) -> tuple[str, ...]:
    desired: DesiredTable
    for desired in (item for item in desired_state.objects if isinstance(item, DesiredTable)):
        if desired.key == anchor_key:
            return tuple(column.name for column in desired.columns)
    relation: CatalogRelation | None = target_catalog.relation(replay.relations.anchor)
    return () if relation is None else tuple(column.name for column in relation.columns)


def _confirmed_plan(request: BackfillBootstrapRequest) -> DeploymentPlan:
    if request.confirmed_plan is None:
        raise BackfillExecutionError(
            "Virtual workflow assembly requires the confirmed deployment plan"
        )
    return request.confirmed_plan


def _confirmed_deployment_id(request: BackfillBootstrapRequest) -> str:
    plan: DeploymentPlan = _confirmed_plan(request)
    if request.deployment_id is None or plan.deployment_id != request.deployment_id:
        raise BackfillExecutionError(
            "Virtual workflow deployment identity does not match the confirmed plan"
        )
    return request.deployment_id


def _created_at(request: BackfillBootstrapRequest) -> str:
    if request.created_at is None:
        raise BackfillExecutionError(
            "Virtual workflow assembly requires a fixed creation timestamp"
        )
    return request.created_at


def _confirmed_catalog(*, catalog: CatalogSnapshot | None, catalog_name: str) -> CatalogSnapshot:
    if catalog is None:
        raise BackfillExecutionError(
            f"Virtual workflow assembly requires the pre-confirmation {catalog_name} catalog"
        )
    return catalog


def _prepared_relation_names(request: BackfillBootstrapRequest) -> tuple[str, ...]:
    return tuple(
        prepared.physical_name for prepared in _confirmed_plan(request).prepared_shadow_objects
    )


def _boundary_type_expression(mode: AdapterReplayBoundaryMode) -> str:
    return "UInt64" if mode == AdapterReplayBoundaryMode.CURSOR else "DateTime64(3, 'UTC')"
