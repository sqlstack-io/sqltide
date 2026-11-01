"""Build one deterministic direct-mode plan for a selected downstream closure."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterMaterializedView,
    AdapterReplayColumns,
    AdapterTable,
    AdapterView,
    CatalogRelation,
)
from sqltide.adapter.types import AdapterOptionalStateStatus
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledTableModel,
    LogicalResourceKey,
    ObjectKey,
)
from sqltide.compiler.discovery.models import PostgresRefreshSourceStep
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.graph.types import DependencyEdgeType
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.constants import (
    CATALOG_MATERIALIZED_VIEW_ENGINE,
    MUTABLE_REF_WARNING_CODE,
    MUTABLE_REF_WARNING_MESSAGE,
)
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.compiler.planner.models import (
    DirectPlan,
    DirectPlanEntry,
    DirectPrerequisite,
    DirectRelationOperation,
    DirectReplayRoot,
    DirectSourceDriftSelection,
    DirectSqlChange,
    DirectWarehouseSnapshot,
    PlannerWarning,
)
from sqltide.compiler.planner.types import DirectSelectionMode

type _NativeEntry = tuple[
    int,
    str,
    list[tuple[str, str]],
    int | None,
    bool,
    tuple[str, str, str | None, str | None, str | None, str | None],
]
type _NativeReplayRoot = tuple[
    int, int, str, list[str], str, list[int], bool, list[tuple[str, str]]
]


class DirectPlanBuilder:
    """Describe one project and snapshot to the native direct planner and convert its plan."""

    def __init__(
        self,
        *,
        graph: ProjectGraph,
        realized_project: RealizedProject,
        snapshot: DirectWarehouseSnapshot,
        database: str,
        selected_model_keys: frozenset[LogicalResourceKey],
        selection_mode: DirectSelectionMode,
        include_missing_upstream: bool,
        effective_start_time: str | None = None,
        source_drift_selections: tuple[DirectSourceDriftSelection, ...] = (),
    ) -> None:
        self._graph: ProjectGraph = graph
        self._realized_project: RealizedProject = realized_project
        self._snapshot: DirectWarehouseSnapshot = snapshot
        self._database: str = database
        self._selected_model_keys: frozenset[LogicalResourceKey] = selected_model_keys
        self._selection_mode: DirectSelectionMode = selection_mode
        self._include_missing_upstream: bool = include_missing_upstream
        self._effective_start_time: str | None = effective_start_time
        self._source_drift_selections: tuple[DirectSourceDriftSelection, ...] = (
            source_drift_selections
        )
        self._keys: tuple[LogicalResourceKey, ...] = graph.ordered_keys
        self._index_by_key: dict[LogicalResourceKey, int] = {
            key: index for index, key in enumerate(self._keys)
        }
        self._model_by_key: dict[LogicalResourceKey, CompiledModel] = {
            model.key: model for model in realized_project.project.models
        }
        self._postgres_source_names: frozenset[str] = frozenset(
            source.key.name
            for source in realized_project.project.sources
            if isinstance(source.source, PostgresRefreshSourceStep)
        )
        self._replay_lineage_mode_by_key: dict[LogicalResourceKey, ReplayLineageMode] = {}
        self._pipeline_name_by_model_key: dict[LogicalResourceKey, str] = {}
        for pipeline in realized_project.project.pipelines:
            for model in pipeline.models:
                self._pipeline_name_by_model_key.setdefault(model.key, pipeline.pipeline.name)
            if pipeline.source is None or pipeline.effective_replay_lineage_mode is None:
                continue
            self._replay_lineage_mode_by_key[pipeline.source.key] = (
                pipeline.effective_replay_lineage_mode
            )
            for model in pipeline.models:
                self._replay_lineage_mode_by_key[model.key] = pipeline.effective_replay_lineage_mode

    def build(self) -> DirectPlan:
        """Return the complete execution closure without consulting equality."""

        mutable_ref_keys: tuple[ObjectKey, ...] = tuple(
            self._realized_project.desired_state.mutable_ref_warning_keys
        )
        source_drift_keys: set[LogicalResourceKey] = set()
        for selection in self._source_drift_selections:
            source_drift_keys.update(selection.model_keys)
        try:
            (
                user_scope,
                execution_scope,
                prerequisite_execution_scope,
                prerequisites,
                entries,
                replay_roots,
                teardown,
                creation,
                warnings,
            ) = _native.plan_direct_build(
                nodes=[self._node(key=key) for key in self._keys],
                database=self._database,
                selection_mode=str(self._selection_mode),
                selected=self._indexes(keys=self._selected_model_keys),
                source_drift=self._indexes(keys=frozenset(source_drift_keys)),
                include_missing_upstream=self._include_missing_upstream,
                has_start_time=self._effective_start_time is not None,
                catalog=[
                    _catalog_relation(relation=relation)
                    for relation in self._snapshot.catalog.relations
                ],
                external_columns=[
                    (
                        config.table_name,
                        (
                            config.partition_column_name or None,
                            config.offset_column_name or None,
                            config.timestamp_column_name or None,
                            config.landed_at_column_name or None,
                            config.cursor_column_name or None,
                        ),
                    )
                    for config in (
                        self._realized_project.desired_state.external_source_replay_configs
                    )
                ],
                fingerprints=(
                    self._snapshot.fingerprints.status != AdapterOptionalStateStatus.UNAVAILABLE,
                    self._snapshot.fingerprints.warning,
                    [
                        (
                            record.logical_model_identity,
                            record.definition_sql,
                            record.definition_hash,
                        )
                        for record in self._snapshot.fingerprints.baselines
                    ],
                ),
                mutable_ref_names=[key.name for key in mutable_ref_keys],
            )
        except ValueError as error:
            raise DirectPlanError(str(error)) from None
        keys: tuple[LogicalResourceKey, ...] = self._keys
        return DirectPlan(
            database=self._database,
            selection_mode=self._selection_mode,
            effective_start_time=self._effective_start_time,
            user_scope=tuple(keys[index] for index in user_scope),
            execution_scope=tuple(keys[index] for index in execution_scope),
            prerequisite_execution_scope=tuple(
                keys[index] for index in prerequisite_execution_scope
            ),
            prerequisite_scope=tuple(
                DirectPrerequisite(
                    key=keys[index],
                    relation_names=(relation_name,),
                    present=present,
                    framework_managed=framework_managed,
                )
                for index, relation_name, present, framework_managed in prerequisites
            ),
            entries=tuple(
                self._entry(keys=keys, native_entry=native_entry) for native_entry in entries
            ),
            replay_roots=tuple(
                _replay_root(keys=keys, native_root=native_root) for native_root in replay_roots
            ),
            teardown_operations=_operations(keys=keys, operations=teardown, action="drop"),
            creation_operations=_operations(keys=keys, operations=creation, action="create"),
            warnings=tuple(
                PlannerWarning(
                    warning_code=MUTABLE_REF_WARNING_CODE,
                    message=MUTABLE_REF_WARNING_MESSAGE,
                    root_key=mutable_ref_keys[index],
                    target_key=mutable_ref_keys[index],
                )
                for index in warnings
            ),
            source_drift_selections=self._source_drift_selections,
        )

    def _entry(
        self, *, keys: tuple[LogicalResourceKey, ...], native_entry: _NativeEntry
    ) -> DirectPlanEntry:
        index, reason, relations, driving_input, is_replay_root, sql_change = native_entry
        status, current_hash, previous_sql, previous_hash, unified_diff, warning = sql_change
        return DirectPlanEntry(
            model_key=keys[index],
            reason=reason,
            relation_names=tuple(name for name, _kind in relations),
            resource_kinds=tuple(kind for _name, kind in relations),
            driving_input_key=None if driving_input is None else keys[driving_input],
            is_replay_root=is_replay_root,
            sql_change=DirectSqlChange(
                status=status,
                current_sql=self._model_by_key[keys[index]].query,
                current_hash=current_hash,
                previous_sql=previous_sql,
                previous_hash=previous_hash,
                unified_diff=unified_diff,
                warning=warning,
            ),
        )

    def _indexes(self, *, keys: frozenset[LogicalResourceKey]) -> set[int]:
        return {self._index_by_key[key] for key in keys if key in self._index_by_key}

    def _node(
        self, *, key: LogicalResourceKey
    ) -> tuple[
        str,
        tuple[bool, bool, str, list[tuple[str, str]]] | None,
        bool,
        str,
        list[tuple[str, str | None]],
        str | None,
        bool,
        str | None,
        list[tuple[int, bool]],
        list[int],
    ]:
        model: CompiledModel | None = self._model_by_key.get(key)
        resources: tuple[object, ...] = self._realized_project.resources_by_logical_key.get(key, ())
        lineage: ReplayLineageMode | None = self._replay_lineage_mode_by_key.get(key)
        return (
            key.name,
            None
            if model is None
            else (
                isinstance(model, CompiledTableModel),
                isinstance(model, CompiledTableModel) and model.has_aggregate_semantics,
                model.query,
                list((model.transform.execution_settings.replay or {}).items())
                if isinstance(model, CompiledTableModel)
                else [],
            ),
            bool(resources),
            self._realized_project.relation_name_by_logical_key[key],
            [(resource.name, _resource_kind(resource)) for resource in resources],
            None if lineage is None else str(lineage),
            key.name in self._postgres_source_names,
            self._pipeline_name_by_model_key.get(key),
            [
                (
                    self._index_by_key[edge.upstream_key],
                    edge.edge_type == DependencyEdgeType.DRIVING_INPUT,
                )
                for edge in self._graph.upstream_edges_by_key.get(key, ())
            ],
            [
                self._index_by_key[edge.downstream_key]
                for edge in self._graph.downstream_edges_by_key.get(key, ())
            ],
        )


def _catalog_relation(*, relation: CatalogRelation) -> tuple[str, bool, list[str], list[str]]:
    column_names: list[str] = [column.name for column in relation.columns]
    return (
        relation.name,
        relation.engine == CATALOG_MATERIALIZED_VIEW_ENGINE,
        column_names,
        list(relation.source_relation_names),
    )


def _replay_root(
    *, keys: tuple[LogicalResourceKey, ...], native_root: _NativeReplayRoot
) -> DirectReplayRoot:
    (
        index,
        driving_input,
        relation_name,
        columns,
        lineage,
        propagated,
        aggregate,
        settings,
    ) = native_root
    return DirectReplayRoot(
        model_key=keys[index],
        driving_input_key=keys[driving_input],
        driving_input_relation_name=relation_name,
        driving_input_replay_columns=AdapterReplayColumns(
            partition=columns[0],
            offset=columns[1],
            timestamp=columns[2],
            landed_at=columns[3],
            cursor=columns[4],
        ),
        replay_boundary_mode=lineage,
        propagated_model_keys=tuple(keys[key] for key in propagated),
        has_aggregate_semantics=aggregate,
        replay_settings=tuple(settings),
    )


def _resource_kind(resource: object) -> str | None:
    if isinstance(resource, AdapterTable):
        return "table"
    if isinstance(resource, AdapterMaterializedView):
        return "materialized_view"
    if isinstance(resource, AdapterView):
        return "view"
    return None


def _operations(
    *,
    keys: tuple[LogicalResourceKey, ...],
    operations: list[tuple[str, int, str]],
    action: str,
) -> tuple[DirectRelationOperation, ...]:
    return tuple(
        DirectRelationOperation(
            relation_name=relation_name,
            action=action,
            model_key=keys[index],
            resource_kind=kind,
        )
        for relation_name, index, kind in operations
    )
