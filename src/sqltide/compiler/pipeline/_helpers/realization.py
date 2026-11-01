"""Assemble adapter realizations and planner desired state from a logical project."""

from dataclasses import replace

import sqltide._native as _native
from sqltide.adapter.constants import (
    MANAGED_SOURCE_KIND_KAFKA,
)
from sqltide.adapter.models import (
    AdapterAdoptedSourceRealizationRequest,
    AdapterColumn,
    AdapterManagedSource,
    AdapterManagedSourceRealizationRequest,
    AdapterMaterializedView,
    AdapterModelRealization,
    AdapterModelRealizationRequest,
    AdapterSourceRealization,
    AdapterTable,
    AdapterView,
    AdapterViewRealizationRequest,
)
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_KAFKA_TABLE,
    DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.main._render_kafka_retention import render_kafka_retention
from sqltide.compiler.compile.models import (
    Column,
    CompiledModel,
    CompiledProject,
    CompiledSource,
    CompiledTableModel,
    CompiledViewModel,
    CompilerAdapterProfile,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    ExternalSourceReplayConfig,
    KafkaSettings,
    KafkaTableSpec,
    LogicalResourceKey,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
    ViewSpec,
)
from sqltide.compiler.discovery.models import (
    ExternalTableSourceStep,
    KafkaLandingStep,
    PostgresRefreshSourceStep,
    ReplayBoundary,
    ReplayBoundaryColumns,
)
from sqltide.compiler.discovery.types import BoundedReplayFallback
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.pipeline.types import AdapterResource, DesiredObject
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.models import SqlResolvedQuery


def build_realized_project(
    *,
    project: CompiledProject,
    adapter_profile: CompilerAdapterProfile,
    sql_analyzer: SqlModelAnalyzer,
) -> RealizedProject:
    """Map one complete logical project to adapter resources and planner state."""

    source_realizations: dict[LogicalResourceKey, AdapterSourceRealization] = {
        source.key: _canonical_source_realization(
            realization=adapter_profile.realize_source(
                request=_source_request(source=source, project=project)
            ),
            sql_analyzer=sql_analyzer,
        )
        for source in project.sources
    }
    relation_names: dict[LogicalResourceKey, str] = {
        key: realization.relation_name for key, realization in source_realizations.items()
    }
    model: CompiledModel
    for model in project.models:
        relation_names[model.key] = model.relation_name
    relation_name_by_logical_name: dict[str, str] = {
        key.name: relation_name for key, relation_name in relation_names.items()
    }
    relation_sql_by_name: dict[str, str] = _relation_sql_by_logical_name(
        project=project,
        relation_names=relation_names,
    )
    postgres_by_source_name: dict[str, PostgresRefreshSourceStep] = (
        _postgres_refresh_by_source_name(project=project)
    )
    model_realizations: dict[LogicalResourceKey, AdapterModelRealization] = {}
    resolved_queries: dict[LogicalResourceKey, str] = {}
    for model in project.models:
        resolved_query: SqlResolvedQuery = sql_analyzer.resolve(
            analysis=model.sql_analysis,
            resolver=relation_sql_by_name,
        )
        realization: AdapterModelRealization = adapter_profile.realize_model(
            request=_model_request(
                model=model,
                resolved_query=resolved_query,
                relation_names=relation_names,
                relation_name_by_logical_name=relation_name_by_logical_name,
                postgres_by_source_name=postgres_by_source_name,
            )
        )
        _validate_model_relation_name(
            model=model,
            realization=realization,
            expected_relation_name=relation_names[model.key],
        )
        model_realizations[model.key] = realization
        resolved_queries[model.key] = resolved_query.canonical_sql
    resources_by_key: dict[LogicalResourceKey, tuple[AdapterResource, ...]] = {
        key: realization.resources for key, realization in source_realizations.items()
    }
    resources_by_key.update(
        {key: realization.resources for key, realization in model_realizations.items()}
    )
    return RealizedProject(
        project=project,
        resources_by_logical_key=resources_by_key,
        relation_name_by_logical_key=relation_names,
        resolved_query_by_model_key=resolved_queries,
        desired_state=_build_desired_state(
            project=project,
            source_realizations=source_realizations,
            model_realizations=model_realizations,
        ),
    )


def _canonical_source_realization(
    *, realization: AdapterSourceRealization, sql_analyzer: SqlModelAnalyzer
) -> AdapterSourceRealization:
    resources: list[AdapterResource] = []
    resource: AdapterResource
    for resource in realization.resources:
        if isinstance(resource, AdapterMaterializedView):
            query: SqlResolvedQuery = sql_analyzer.canonicalize_query(sql=resource.query)
            resources.append(
                replace(
                    resource,
                    query=query.canonical_sql,
                    database_template=query.database_template,
                )
            )
        else:
            resources.append(resource)
    return replace(realization, resources=tuple(resources))


def _source_request(
    *, source: CompiledSource, project: CompiledProject
) -> AdapterManagedSourceRealizationRequest | AdapterAdoptedSourceRealizationRequest:
    authored_source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep = (
        source.source
    )
    if isinstance(authored_source, ExternalTableSourceStep):
        return AdapterAdoptedSourceRealizationRequest(
            logical_name=source.key.name,
            relation_name=authored_source.table_name,
        )
    if isinstance(authored_source, PostgresRefreshSourceStep):
        return AdapterAdoptedSourceRealizationRequest(
            logical_name=source.key.name,
            relation_name=_postgres_source_relation_sql(authored_source),
        )
    settings: tuple[tuple[str, str], ...] = (
        ()
        if authored_source.kafka.settings is None
        else tuple(sorted(authored_source.kafka.settings.items()))
    )
    return AdapterManagedSourceRealizationRequest(
        logical_name=source.key.name,
        source_kind=MANAGED_SOURCE_KIND_KAFKA,
        broker_list=authored_source.kafka.broker_list,
        topic=authored_source.kafka.topic,
        consumer_group=authored_source.kafka.consumer_group,
        format=authored_source.kafka.format,
        project_name=project.project_name,
        target_name=project.target_name,
        ttl=(
            render_kafka_retention(policy=authored_source.kafka.retention)
            if authored_source.kafka.retention not in (None, False)
            else authored_source.kafka.ttl
        ),
        settings=settings,
        naming_macro_fingerprint=authored_source.naming_macro_fingerprint,
    )


def _relation_sql_by_logical_name(
    *,
    project: CompiledProject,
    relation_names: dict[LogicalResourceKey, str],
) -> dict[str, str]:
    relation_sqls: dict[str, str] = {
        key.name: relation_name for key, relation_name in relation_names.items()
    }
    source: CompiledSource
    for source in project.sources:
        if isinstance(source.source, ExternalTableSourceStep):
            relation_sqls[source.key.name] = _external_source_relation_sql(source.source)
        if isinstance(source.source, PostgresRefreshSourceStep):
            relation_sqls[source.key.name] = _postgres_source_relation_sql(source.source)
    return relation_sqls


def _postgres_source_relation_sql(source: PostgresRefreshSourceStep) -> str:
    """Render the postgres table function, keeping any credential out of the artifact."""

    return _native.clickhouse_postgres_source_relation_sql(
        source.host,
        str(source.port),
        source.database,
        source.table,
        source.user,
        source.password_env,
    )


def _postgres_refresh_by_source_name(
    *, project: CompiledProject
) -> dict[str, PostgresRefreshSourceStep]:
    return {
        source.key.name: source.source
        for source in project.sources
        if isinstance(source.source, PostgresRefreshSourceStep)
    }


def _external_source_relation_sql(source: ExternalTableSourceStep) -> str:
    columns: ReplayBoundaryColumns = source.replay_boundary.columns
    return _native.clickhouse_external_source_relation_sql(
        source.table_name,
        [columns.partition, columns.offset, columns.timestamp, columns.landed_at, columns.cursor],
    )


def _model_request(
    *,
    model: CompiledModel,
    resolved_query: SqlResolvedQuery,
    relation_names: dict[LogicalResourceKey, str],
    relation_name_by_logical_name: dict[str, str],
    postgres_by_source_name: dict[str, PostgresRefreshSourceStep],
) -> AdapterModelRealizationRequest | AdapterViewRealizationRequest:
    if isinstance(model, CompiledViewModel):
        return AdapterViewRealizationRequest(
            logical_name=model.key.name,
            target_relation_name=relation_names[model.key],
            resolved_query=resolved_query.canonical_sql,
            resolved_database_template=resolved_query.database_template,
        )
    if not isinstance(model, CompiledTableModel):
        raise PipelineCompileError(f"Unsupported compiled model type for '{model.key.name}'")
    settings: tuple[tuple[str, str], ...] = (
        () if model.transform.settings is None else tuple(sorted(model.transform.settings.items()))
    )
    source_relation_name: str = relation_name_by_logical_name[model.transform.source]
    postgres_source: PostgresRefreshSourceStep | None = postgres_by_source_name.get(
        model.transform.source
    )
    return AdapterModelRealizationRequest(
        logical_name=model.key.name,
        target_relation_name=relation_names[model.key],
        source_relation_name=source_relation_name,
        resolved_query=resolved_query.canonical_sql,
        resolved_database_template=resolved_query.database_template,
        columns=tuple(_adapter_column(column) for column in model.output_columns),
        engine=model.transform.engine,
        order_by=tuple(model.transform.order_by),
        partition_by=model.transform.partition_by,
        ttl=model.transform.ttl,
        settings=settings,
        refresh=None if postgres_source is None else postgres_source.refresh,
        append=True if postgres_source is None else postgres_source.append,
    )


def _adapter_column(column: Column) -> AdapterColumn:
    return AdapterColumn(name=column.name, type=column.type, default_expression=column.default)


def _validate_model_relation_name(
    *,
    model: CompiledModel,
    realization: AdapterModelRealization,
    expected_relation_name: str,
) -> None:
    if realization.relation_name != expected_relation_name:
        raise PipelineCompileError(
            f"Adapter realized model '{model.key.name}' with relation "
            f"'{realization.relation_name}' after naming it '{expected_relation_name}'"
        )


def _build_desired_state(
    *,
    project: CompiledProject,
    source_realizations: dict[LogicalResourceKey, AdapterSourceRealization],
    model_realizations: dict[LogicalResourceKey, AdapterModelRealization],
) -> DesiredState:
    objects: list[DesiredObject] = []
    replay_anchor_keys: set[ObjectKey] = set()
    mutable_ref_warning_keys: set[ObjectKey] = set()
    external_configs: list[ExternalSourceReplayConfig] = []
    relation_key_by_name: dict[str, ObjectKey] = {}
    source: CompiledSource
    for source in project.sources:
        realization: AdapterSourceRealization = source_realizations[source.key]
        objects.extend(_source_desired_objects(source=source, realization=realization))
        source_key: ObjectKey = _object_key(
            name=realization.relation_name,
            object_type=DESIRED_OBJECT_TYPE_TABLE,
        )
        relation_key_by_name[source.key.name] = source_key
        replay_anchor_keys.add(source_key)
        external_config: ExternalSourceReplayConfig | None = _external_replay_config(
            source=source,
            key=source_key,
        )
        if external_config is not None:
            external_configs.append(external_config)
    model: CompiledModel
    for model in project.models:
        relation_key_by_name[model.key.name] = _object_key(
            name=model_realizations[model.key].relation_name,
            object_type=(
                DESIRED_OBJECT_TYPE_TABLE
                if isinstance(model, CompiledTableModel)
                else DESIRED_OBJECT_TYPE_VIEW
            ),
        )
    for model in project.models:
        if isinstance(model, CompiledViewModel):
            objects.append(
                _view_model_desired_object(
                    model=model,
                    realization=model_realizations[model.key],
                    relation_key_by_name=relation_key_by_name,
                )
            )
        elif isinstance(model, CompiledTableModel):
            model_objects: tuple[DesiredTable, DesiredMaterializedView] = _model_desired_objects(
                model=model,
                realization=model_realizations[model.key],
                relation_key_by_name=relation_key_by_name,
            )
            objects.extend(model_objects)
            if model.replay_anchor_eligible:
                replay_anchor_keys.add(model_objects[0].key)
            if model.has_mutable_refs:
                mutable_ref_warning_keys.add(model_objects[0].key)
    return DesiredState(
        objects=tuple(sorted(objects, key=lambda item: (item.key.object_type, item.key.name))),
        replay_anchor_keys=frozenset(replay_anchor_keys),
        mutable_ref_warning_keys=frozenset(mutable_ref_warning_keys),
        external_source_replay_configs=tuple(
            sorted(external_configs, key=lambda config: config.table_name)
        ),
    )


def _source_desired_objects(
    *, source: CompiledSource, realization: AdapterSourceRealization
) -> tuple[DesiredObject, ...]:
    if isinstance(source.source, (ExternalTableSourceStep, PostgresRefreshSourceStep)):
        if realization.resources:
            raise PipelineCompileError(
                f"Adapter claimed resources for adopted source '{source.key.name}'"
            )
        return ()
    managed_source: AdapterManagedSource = _managed_source_resource(
        resources=realization.resources,
        logical_name=source.key.name,
    )
    table: AdapterTable = _table_resource(
        resources=realization.resources,
        logical_name=source.key.name,
    )
    view: AdapterMaterializedView = _view_resource(
        resources=realization.resources,
        logical_name=source.key.name,
    )
    source_key: ObjectKey = _object_key(
        name=managed_source.name,
        object_type=DESIRED_OBJECT_TYPE_KAFKA_TABLE,
    )
    table_key: ObjectKey = _object_key(name=table.name, object_type=DESIRED_OBJECT_TYPE_TABLE)
    return (
        _desired_managed_source(resource=managed_source, key=source_key),
        _desired_table(
            resource=table,
            key=table_key,
            deps=(),
            logical_model_name=source.key.name,
        ),
        _desired_view(
            resource=view,
            deps=(source_key, table_key),
            logical_model_name=source.key.name,
        ),
    )


def _model_desired_objects(
    *,
    model: CompiledTableModel,
    realization: AdapterModelRealization,
    relation_key_by_name: dict[str, ObjectKey],
) -> tuple[DesiredTable, DesiredMaterializedView]:
    table: AdapterTable = _table_resource(
        resources=realization.resources,
        logical_name=model.key.name,
    )
    view: AdapterMaterializedView = _view_resource(
        resources=realization.resources,
        logical_name=model.key.name,
    )
    table_key: ObjectKey = _object_key(name=table.name, object_type=DESIRED_OBJECT_TYPE_TABLE)
    ref_keys: tuple[ObjectKey, ...] = tuple(
        dict.fromkeys(relation_key_by_name[parsed_ref.name] for parsed_ref in model.parsed_refs)
    )
    return (
        _desired_table(
            resource=table,
            key=table_key,
            deps=(relation_key_by_name[model.transform.source],),
            logical_model_name=model.key.name,
            model=model,
        ),
        _desired_view(
            resource=view,
            deps=(*ref_keys, table_key),
            logical_model_name=model.key.name,
        ),
    )


def _view_model_desired_object(
    *,
    model: CompiledViewModel,
    realization: AdapterModelRealization,
    relation_key_by_name: dict[str, ObjectKey],
) -> DesiredView:
    view: AdapterView = _ordinary_view_resource(
        resources=realization.resources,
        logical_name=model.key.name,
    )
    return DesiredView(
        key=_object_key(name=view.name, object_type=DESIRED_OBJECT_TYPE_VIEW),
        deps=tuple(
            dict.fromkeys(relation_key_by_name[parsed_ref.name] for parsed_ref in model.parsed_refs)
        ),
        spec=ViewSpec(
            query=view.query,
            database_template=view.database_template,
        ),
        logical_model_name=model.key.name,
    )


def _desired_managed_source(*, resource: AdapterManagedSource, key: ObjectKey) -> DesiredKafkaTable:
    return DesiredKafkaTable(
        key=key,
        deps=(),
        spec=KafkaTableSpec(
            columns=tuple(_column(column) for column in resource.columns),
            kafka=KafkaSettings(
                broker_list=resource.broker_list,
                topic=resource.topic,
                consumer_group=resource.consumer_group,
                format=resource.format,
                settings=None if not resource.settings else dict(resource.settings),
            ),
            naming_macro_fingerprint=resource.naming_macro_fingerprint,
        ),
    )


def _desired_table(
    *,
    resource: AdapterTable,
    key: ObjectKey,
    deps: tuple[ObjectKey, ...],
    logical_model_name: str,
    model: CompiledTableModel | None = None,
) -> DesiredTable:
    replay_fallback: BoundedReplayFallback = (
        BoundedReplayFallback.FULL if model is None else model.effective_bounded_replay_fallback
    )
    return DesiredTable(
        key=key,
        deps=deps,
        spec=TableSpec(
            columns=tuple(_column(column) for column in resource.columns),
            storage=TableStorage(
                engine=resource.engine,
                order_by=resource.order_by,
                partition_by=resource.partition_by,
                ttl=resource.ttl,
                settings=None if not resource.settings else dict(resource.settings),
            ),
        ),
        logical_model_name=logical_model_name,
        replay_on_change=None if model is None else model.replay_on_change,
        bounded_replay_fallback=replay_fallback,
        replay_settings=(
            None
            if model is None or model.transform.execution_settings.replay is None
            else dict(model.transform.execution_settings.replay)
        ),
    )


def _desired_view(
    *,
    resource: AdapterMaterializedView,
    deps: tuple[ObjectKey, ...],
    logical_model_name: str,
) -> DesiredMaterializedView:
    return DesiredMaterializedView(
        key=_object_key(
            name=resource.name,
            object_type=DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
        ),
        deps=deps,
        spec=MaterializedViewSpec(
            source_table_name=resource.source_relation_name,
            target_table_name=resource.target_relation_name,
            query=resource.query,
            database_template=resource.database_template,
        ),
        logical_model_name=logical_model_name,
        refresh=resource.refresh,
        append=resource.append,
    )


def _external_replay_config(
    *, source: CompiledSource, key: ObjectKey
) -> ExternalSourceReplayConfig | None:
    if not isinstance(source.source, ExternalTableSourceStep):
        return None
    boundary: ReplayBoundary = source.source.replay_boundary
    return ExternalSourceReplayConfig(
        key=key,
        table_name=source.source.table_name,
        source_kind=source.source.kind,
        replay_boundary_mode=boundary.mode,
        partition_column_name=boundary.columns.partition,
        offset_column_name=boundary.columns.offset,
        timestamp_column_name=boundary.columns.timestamp,
        landed_at_column_name=boundary.columns.landed_at,
        cursor_column_name=boundary.columns.cursor,
    )


def _managed_source_resource(
    *, resources: tuple[AdapterResource, ...], logical_name: str
) -> AdapterManagedSource:
    matches: tuple[AdapterManagedSource, ...] = tuple(
        resource for resource in resources if isinstance(resource, AdapterManagedSource)
    )
    if len(matches) != 1:
        raise PipelineCompileError(
            f"Adapter realization for '{logical_name}' requires exactly one managed source; "
            f"got {len(matches)}"
        )
    return matches[0]


def _table_resource(
    *,
    resources: tuple[AdapterResource, ...] | tuple[AdapterTable | AdapterMaterializedView, ...],
    logical_name: str,
) -> AdapterTable:
    matches: tuple[AdapterTable, ...] = tuple(
        resource for resource in resources if isinstance(resource, AdapterTable)
    )
    if len(matches) != 1:
        raise PipelineCompileError(
            f"Adapter realization for '{logical_name}' requires exactly one table; "
            f"got {len(matches)}"
        )
    return matches[0]


def _view_resource(
    *,
    resources: tuple[AdapterResource, ...] | tuple[AdapterTable | AdapterMaterializedView, ...],
    logical_name: str,
) -> AdapterMaterializedView:
    matches: tuple[AdapterMaterializedView, ...] = tuple(
        resource for resource in resources if isinstance(resource, AdapterMaterializedView)
    )
    if len(matches) != 1:
        raise PipelineCompileError(
            f"Adapter realization for '{logical_name}' requires exactly one materialized view; "
            f"got {len(matches)}"
        )
    return matches[0]


def _ordinary_view_resource(
    *,
    resources: tuple[AdapterResource, ...],
    logical_name: str,
) -> AdapterView:
    matches: tuple[AdapterView, ...] = tuple(
        resource for resource in resources if isinstance(resource, AdapterView)
    )
    if len(matches) != 1:
        raise PipelineCompileError(
            f"Adapter realization for '{logical_name}' requires exactly one ordinary view; "
            f"got {len(matches)}"
        )
    return matches[0]


def _object_key(*, name: str, object_type: str) -> ObjectKey:
    return ObjectKey(database=None, object_type=object_type, name=name)


def _column(column: AdapterColumn) -> Column:
    return Column(name=column.name, type=column.type, default=column.default_expression)
