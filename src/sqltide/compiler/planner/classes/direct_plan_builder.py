"""Build one deterministic direct-mode plan for a selected downstream closure."""

from __future__ import annotations

from difflib import unified_diff

from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterMaterializedView,
    AdapterReplayColumns,
    AdapterTable,
    AdapterView,
    CatalogRelation,
)
from sqltide.adapter.types import AdapterOptionalStateStatus
from sqltide.compiler.compile.constants import (
    REPLAY_CURSOR_COLUMN_NAME,
    REPLAY_LANDED_AT_COLUMN_NAME,
    REPLAY_OFFSET_COLUMN_NAME,
    REPLAY_PARTITION_COLUMN_NAME,
    REPLAY_TIMESTAMP_COLUMN_NAME,
)
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledTableModel,
    ExternalSourceReplayConfig,
    LogicalResourceKey,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.models import PostgresRefreshSourceStep
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.graph.constants import ALL_DEPENDENCY_EDGE_TYPES
from sqltide.compiler.graph.main.collect_reachable_keys import collect_reachable_keys
from sqltide.compiler.graph.models import DependencyEdge, ProjectGraph
from sqltide.compiler.graph.types import DependencyEdgeType, GraphTraversalDirection
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.classes.direct_model_fingerprint import DirectModelFingerprint
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.compiler.planner.main.find_direct_out_of_scope_consumers import (
    find_direct_out_of_scope_consumers,
)
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
from sqltide.compiler.planner.types import (
    DirectPlanReason,
    DirectRelationAction,
    DirectResourceKind,
    DirectSelectionMode,
    DirectSqlBaselineStatus,
)

_CANONICAL_REPLAY_COLUMNS: AdapterReplayColumns = AdapterReplayColumns(
    partition=REPLAY_PARTITION_COLUMN_NAME,
    offset=REPLAY_OFFSET_COLUMN_NAME,
    timestamp=REPLAY_TIMESTAMP_COLUMN_NAME,
    landed_at=REPLAY_LANDED_AT_COLUMN_NAME,
    cursor=REPLAY_CURSOR_COLUMN_NAME,
)


class DirectPlanBuilder:
    """Resolve scope, replay roots, and relation actions for one plan."""

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
        source_drift_model_keys: set[LogicalResourceKey] = set()
        for selection in source_drift_selections:
            source_drift_model_keys.update(selection.model_keys)
        self._source_drift_model_keys: frozenset[LogicalResourceKey] = frozenset(
            source_drift_model_keys
        )
        self._model_by_key: dict[LogicalResourceKey, CompiledModel] = {
            model.key: model for model in realized_project.project.models
        }
        self._normal_execution_scope: tuple[LogicalResourceKey, ...] = (
            self._resolve_normal_execution_scope()
        )
        self._prerequisite_execution_scope: tuple[LogicalResourceKey, ...] = (
            self._resolve_prerequisite_execution_scope()
        )
        execution_keys: frozenset[LogicalResourceKey] = frozenset(
            (*self._normal_execution_scope, *self._prerequisite_execution_scope)
        )
        self._execution_scope: tuple[LogicalResourceKey, ...] = _model_keys(
            keys=tuple(key for key in self._graph.ordered_keys if key in execution_keys)
        )
        self._executed_keys: frozenset[LogicalResourceKey] = frozenset(self._execution_scope)
        self._driving_parent_by_key: dict[LogicalResourceKey, LogicalResourceKey] = (
            self._resolve_driving_parents()
        )
        self._replay_lineage_mode_by_key: dict[LogicalResourceKey, ReplayLineageMode] = (
            _replay_lineage_modes_by_key(realized_project=realized_project)
        )
        self._aggregate_model_keys: frozenset[LogicalResourceKey] = frozenset(
            model.key
            for model in realized_project.project.models
            if isinstance(model, CompiledTableModel) and model.has_aggregate_semantics
        )
        self._external_config_by_relation_name: dict[str, ExternalSourceReplayConfig] = {
            config.table_name: config
            for config in realized_project.desired_state.external_source_replay_configs
        }

    def build(self) -> DirectPlan:
        """Return the complete execution closure without consulting equality."""

        self._reject_adopted_source_target_collisions()
        self._reject_out_of_scope_driving_consumers()
        prerequisites: tuple[DirectPrerequisite, ...] = self._build_prerequisites()
        self._reject_missing_prerequisites(prerequisites=prerequisites)
        entries: tuple[DirectPlanEntry, ...] = self._build_entries()
        replay_roots: tuple[DirectReplayRoot, ...] = self._build_replay_roots()
        self._reject_incompatible_replay_inputs(replay_roots=replay_roots)
        return DirectPlan(
            database=self._database,
            selection_mode=self._selection_mode,
            effective_start_time=self._effective_start_time,
            user_scope=tuple(
                key for key in self._execution_scope if key in self._selected_model_keys
            ),
            execution_scope=self._execution_scope,
            prerequisite_execution_scope=self._prerequisite_execution_scope,
            prerequisite_scope=prerequisites,
            entries=entries,
            replay_roots=replay_roots,
            teardown_operations=_teardown_operations(entries=entries),
            creation_operations=_creation_operations(entries=entries),
            warnings=self._build_warnings(entries=entries),
            source_drift_selections=self._source_drift_selections,
        )

    def _resolve_normal_execution_scope(self) -> tuple[LogicalResourceKey, ...]:
        if self._selection_mode == DirectSelectionMode.ALL_MODELS:
            return _model_keys(keys=self._graph.ordered_keys)
        if not self._selected_model_keys:
            return ()
        return _model_keys(
            keys=collect_reachable_keys(
                graph=self._graph,
                root_keys=self._selected_model_keys,
                direction=GraphTraversalDirection.DOWNSTREAM,
                edge_types=ALL_DEPENDENCY_EDGE_TYPES,
            )
        )

    def _resolve_prerequisite_execution_scope(self) -> tuple[LogicalResourceKey, ...]:
        if not self._include_missing_upstream:
            return ()
        existing_names: frozenset[str] = self._snapshot.catalog.relation_names()
        execution_keys: set[LogicalResourceKey] = set(self._normal_execution_scope)
        added_keys: set[LogicalResourceKey] = set()
        while True:
            missing_model_keys: set[LogicalResourceKey] = set()
            for model_key in execution_keys:
                for edge in self._graph.upstream_edges_by_key.get(model_key, ()):
                    upstream_key: LogicalResourceKey = edge.upstream_key
                    if upstream_key in execution_keys or upstream_key not in self._model_by_key:
                        continue
                    if self._relation_name(key=upstream_key) not in existing_names:
                        missing_model_keys.add(upstream_key)
            if not missing_model_keys:
                break
            execution_keys.update(missing_model_keys)
            added_keys.update(missing_model_keys)
        return _model_keys(keys=tuple(key for key in self._graph.ordered_keys if key in added_keys))

    def _resolve_driving_parents(self) -> dict[LogicalResourceKey, LogicalResourceKey]:
        driving_parent_by_key: dict[LogicalResourceKey, LogicalResourceKey] = {}
        key: LogicalResourceKey
        for key in self._graph.ordered_keys:
            edge: DependencyEdge
            for edge in self._graph.upstream_edges_by_key.get(key, ()):
                if edge.edge_type == DependencyEdgeType.DRIVING_INPUT:
                    driving_parent_by_key[key] = edge.upstream_key
        return driving_parent_by_key

    def _build_entries(self) -> tuple[DirectPlanEntry, ...]:
        return tuple(self._build_entry(model_key=model_key) for model_key in self._execution_scope)

    def _build_entry(self, *, model_key: LogicalResourceKey) -> DirectPlanEntry:
        model: CompiledModel = self._model_by_key[model_key]
        driving_parent_key: LogicalResourceKey | None = self._driving_parent_by_key.get(model_key)
        relations: tuple[tuple[str, DirectResourceKind], ...] = self._model_relations(
            model_key=model_key
        )
        if isinstance(model, CompiledTableModel) and driving_parent_key is None:
            driving_parent_key = self._require_driving_parent(model_key=model_key)
        relation_names: tuple[str, ...] = tuple(name for name, _kind in relations)
        return DirectPlanEntry(
            model_key=model_key,
            reason=self._plan_reason(model_key=model_key),
            relation_names=relation_names,
            resource_kinds=tuple(kind for _name, kind in relations),
            driving_input_key=driving_parent_key,
            is_replay_root=(
                isinstance(model, CompiledTableModel)
                and driving_parent_key not in self._executed_keys
            ),
            sql_change=self._sql_change(model=model),
        )

    def _sql_change(self, *, model: CompiledModel) -> DirectSqlChange:
        current_sql: str = model.query
        current_hash: str = DirectModelFingerprint.query_hash(current_sql)
        if self._snapshot.fingerprints.status == AdapterOptionalStateStatus.UNAVAILABLE:
            return DirectSqlChange(
                status=DirectSqlBaselineStatus.BASELINE_UNAVAILABLE,
                current_sql=current_sql,
                current_hash=current_hash,
                previous_sql=None,
                previous_hash=None,
                unified_diff=None,
                warning=self._snapshot.fingerprints.warning,
            )
        identity: str = f"{self._database}.{model.key.name}"
        baseline: AdapterDirectFingerprintRecord | None = next(
            (
                record
                for record in self._snapshot.fingerprints.baselines
                if record.logical_model_identity == identity
            ),
            None,
        )
        if baseline is None:
            return DirectSqlChange(
                status=DirectSqlBaselineStatus.FIRST_BASELINE,
                current_sql=current_sql,
                current_hash=current_hash,
                previous_sql=None,
                previous_hash=None,
                unified_diff=None,
            )
        status: DirectSqlBaselineStatus = (
            DirectSqlBaselineStatus.NO_QUERY_CHANGE
            if baseline.definition_hash == current_hash
            else DirectSqlBaselineStatus.QUERY_CHANGED
        )
        diff: str | None = (
            None
            if status == DirectSqlBaselineStatus.NO_QUERY_CHANGE
            else "".join(
                unified_diff(
                    baseline.definition_sql.splitlines(keepends=True),
                    current_sql.splitlines(keepends=True),
                    fromfile="previous",
                    tofile="current",
                )
            )
        )
        return DirectSqlChange(
            status=status,
            current_sql=current_sql,
            current_hash=current_hash,
            previous_sql=baseline.definition_sql,
            previous_hash=baseline.definition_hash,
            unified_diff=diff,
        )

    def _plan_reason(self, *, model_key: LogicalResourceKey) -> DirectPlanReason:
        if self._selection_mode == DirectSelectionMode.ALL_MODELS:
            return DirectPlanReason.ALL_MODELS
        if model_key in self._prerequisite_execution_scope:
            return DirectPlanReason.MISSING_UPSTREAM
        if model_key in self._source_drift_model_keys:
            return DirectPlanReason.SOURCE_DRIFT
        if model_key in self._selected_model_keys:
            return (
                DirectPlanReason.CHANGED
                if self._selection_mode == DirectSelectionMode.CHANGED
                else DirectPlanReason.SELECTED
            )
        return DirectPlanReason.DOWNSTREAM_OF_SELECTED

    def _build_prerequisites(self) -> tuple[DirectPrerequisite, ...]:
        existing_names: frozenset[str] = self._snapshot.catalog.relation_names()
        return tuple(
            DirectPrerequisite(
                key=prerequisite_key,
                relation_names=(self._relation_name(key=prerequisite_key),),
                present=self._relation_name(key=prerequisite_key) in existing_names,
                framework_managed=self._is_framework_managed_source(key=prerequisite_key),
            )
            for prerequisite_key in self._prerequisite_keys()
        )

    def _is_framework_managed_source(self, *, key: LogicalResourceKey) -> bool:
        if key.resource_type != LogicalResourceType.SOURCE:
            return False
        if key.name in self._postgres_source_names():
            return True
        return bool(self._realized_project.resources_by_logical_key[key])

    def _postgres_source_names(self) -> frozenset[str]:
        """Name every scheduled postgres source, which the warehouse refreshes itself."""

        return frozenset(
            source.key.name
            for source in self._realized_project.project.sources
            if isinstance(source.source, PostgresRefreshSourceStep)
        )

    def _prerequisite_keys(self) -> tuple[LogicalResourceKey, ...]:
        prerequisite_keys: set[LogicalResourceKey] = set()
        model_key: LogicalResourceKey
        for model_key in self._execution_scope:
            edge: DependencyEdge
            for edge in self._graph.upstream_edges_by_key.get(model_key, ()):
                if edge.upstream_key not in self._executed_keys:
                    prerequisite_keys.add(edge.upstream_key)
        return tuple(key for key in self._graph.ordered_keys if key in prerequisite_keys)

    def _build_replay_roots(self) -> tuple[DirectReplayRoot, ...]:
        propagated_by_root: dict[LogicalResourceKey, list[LogicalResourceKey]] = {}
        model_key: LogicalResourceKey
        postgres_source_names: frozenset[str] = self._postgres_source_names()
        root_by_key: dict[LogicalResourceKey, LogicalResourceKey] = {}
        for model_key in self._execution_scope:
            parent_key: LogicalResourceKey | None = self._driving_parent_by_key.get(model_key)
            root_by_key[model_key] = (
                root_by_key[parent_key] if parent_key in self._executed_keys else model_key
            )
            if not isinstance(self._model_by_key[model_key], CompiledTableModel):
                continue
            root_key: LogicalResourceKey = root_by_key[model_key]
            driving_parent_key: LogicalResourceKey | None = self._driving_parent_by_key.get(
                root_key
            )
            if driving_parent_key is not None and driving_parent_key.name in postgres_source_names:
                continue
            propagated_by_root.setdefault(root_key, []).append(model_key)
        root_key: LogicalResourceKey
        return tuple(
            self._build_replay_root(
                root_key=root_key, propagated_model_keys=tuple(propagated_by_root[root_key])
            )
            for root_key in self._execution_scope
            if root_key in propagated_by_root
        )

    def _build_replay_root(
        self,
        *,
        root_key: LogicalResourceKey,
        propagated_model_keys: tuple[LogicalResourceKey, ...],
    ) -> DirectReplayRoot:
        driving_input_key: LogicalResourceKey = self._require_driving_parent(model_key=root_key)
        root_model: CompiledModel = self._model_by_key[root_key]
        if not isinstance(root_model, CompiledTableModel):
            raise DirectPlanError(f"Replay root '{root_key.name}' is not a table model")
        return DirectReplayRoot(
            model_key=root_key,
            driving_input_key=driving_input_key,
            driving_input_relation_name=self._relation_name(key=driving_input_key),
            driving_input_replay_columns=self._driving_input_replay_columns(
                driving_input_key=driving_input_key
            ),
            replay_boundary_mode=self._replay_boundary_mode(model_key=root_key),
            propagated_model_keys=propagated_model_keys,
            has_aggregate_semantics=root_key in self._aggregate_model_keys,
            replay_settings=tuple(
                sorted((root_model.transform.execution_settings.replay or {}).items())
            ),
        )

    def _reject_adopted_source_target_collisions(self) -> None:
        target_names: set[str] = set()
        model_key: LogicalResourceKey
        for model_key in self._execution_scope:
            target_names.update(name for name, _kind in self._model_relations(model_key=model_key))
        collisions: tuple[str, ...] = tuple(
            sorted(target_names & self._external_config_by_relation_name.keys())
        )
        if collisions:
            raise DirectPlanError(
                "Direct mode refuses adopted source relations that collide with managed "
                f"targets: {', '.join(collisions)}"
            )

    def _reject_out_of_scope_driving_consumers(self) -> None:
        """Avoid replaying into live materialized views outside the requested scope."""

        consumers: tuple[str, ...] = find_direct_out_of_scope_consumers(
            realized_project=self._realized_project,
            catalog=self._snapshot.catalog,
            prerequisite_execution_scope=self._prerequisite_execution_scope,
            execution_scope=self._execution_scope,
        )
        if consumers:
            raise DirectPlanError(
                "Direct plan cannot rebuild missing upstream models while out-of-scope "
                f"materialized views consume them: {', '.join(consumers)}; include or remove "
                "those consumers before retrying"
            )

    def _driving_input_replay_columns(
        self, *, driving_input_key: LogicalResourceKey
    ) -> AdapterReplayColumns:
        if driving_input_key.resource_type != LogicalResourceType.SOURCE:
            return _CANONICAL_REPLAY_COLUMNS
        relation_name: str = self._relation_name(key=driving_input_key)
        config: ExternalSourceReplayConfig | None = self._external_config_by_relation_name.get(
            relation_name
        )
        if config is None:
            return _CANONICAL_REPLAY_COLUMNS
        return AdapterReplayColumns(
            partition=config.partition_column_name or REPLAY_PARTITION_COLUMN_NAME,
            offset=config.offset_column_name or REPLAY_OFFSET_COLUMN_NAME,
            timestamp=config.timestamp_column_name or REPLAY_TIMESTAMP_COLUMN_NAME,
            landed_at=(
                config.landed_at_column_name
                or config.timestamp_column_name
                or REPLAY_LANDED_AT_COLUMN_NAME
            ),
            cursor=config.cursor_column_name or REPLAY_CURSOR_COLUMN_NAME,
        )

    def _build_warnings(
        self, *, entries: tuple[DirectPlanEntry, ...]
    ) -> tuple[PlannerWarning, ...]:
        target_names: set[str] = set()
        entry: DirectPlanEntry
        for entry in entries:
            relation_name: str
            resource_kind: DirectResourceKind
            for relation_name, resource_kind in zip(
                entry.relation_names, entry.resource_kinds, strict=True
            ):
                if resource_kind == DirectResourceKind.TABLE:
                    target_names.add(relation_name)
        return tuple(
            PlannerWarning(
                warning_code="mutable_ref_replay_not_guaranteed",
                message=(
                    "Transform uses mutable side refs; exact historical replay equivalence "
                    "cannot be guaranteed because side-table state may differ from the original "
                    "processing time."
                ),
                root_key=key,
                target_key=key,
            )
            for key in sorted(
                self._realized_project.desired_state.mutable_ref_warning_keys,
                key=lambda candidate: candidate.name,
            )
            if key.name in target_names
        )

    def _replay_boundary_mode(self, *, model_key: LogicalResourceKey) -> ReplayLineageMode:
        replay_lineage_mode: ReplayLineageMode | None = self._replay_lineage_mode_by_key.get(
            model_key
        )
        if replay_lineage_mode is None:
            raise DirectPlanError(
                f"Direct plan cannot resolve the replay mode of '{model_key.name}'"
            )
        return replay_lineage_mode

    def _relation_name(self, *, key: LogicalResourceKey) -> str:
        return self._realized_project.relation_name_by_logical_key[key]

    def _model_relations(
        self, *, model_key: LogicalResourceKey
    ) -> tuple[tuple[str, DirectResourceKind], ...]:
        resources: tuple[object, ...] = self._realized_project.resources_by_logical_key[model_key]
        table_names: tuple[str, ...] = tuple(
            resource.name for resource in resources if isinstance(resource, AdapterTable)
        )
        view_names: tuple[str, ...] = tuple(
            resource.name for resource in resources if isinstance(resource, AdapterMaterializedView)
        )
        ordinary_view_names: tuple[str, ...] = tuple(
            resource.name for resource in resources if isinstance(resource, AdapterView)
        )
        if len(ordinary_view_names) == 1 and not table_names and not view_names:
            return ((ordinary_view_names[0], DirectResourceKind.VIEW),)
        if len(table_names) != 1 or len(view_names) != 1 or ordinary_view_names:
            raise DirectPlanError(
                f"Direct plan received invalid realized resources for '{model_key.name}'"
            )
        return (
            (table_names[0], DirectResourceKind.TABLE),
            (view_names[0], DirectResourceKind.MATERIALIZED_VIEW),
        )

    def _require_driving_parent(self, *, model_key: LogicalResourceKey) -> LogicalResourceKey:
        driving_parent_key: LogicalResourceKey | None = self._driving_parent_by_key.get(model_key)
        if driving_parent_key is None:
            raise DirectPlanError(
                f"Direct plan cannot find the driving input of model '{model_key.name}'"
            )
        return driving_parent_key

    def _reject_missing_prerequisites(
        self, *, prerequisites: tuple[DirectPrerequisite, ...]
    ) -> None:
        missing: tuple[DirectPrerequisite, ...] = tuple(
            prerequisite
            for prerequisite in prerequisites
            if not prerequisite.present and not prerequisite.framework_managed
        )
        if missing:
            details: tuple[str, ...] = tuple(
                self._missing_prerequisite_detail(prerequisite=item) for item in missing
            )
            raise DirectPlanError(
                "Direct plan requires preserved upstream relations that do not exist: "
                f"{', '.join(details)}"
            )

    def _missing_prerequisite_detail(self, *, prerequisite: DirectPrerequisite) -> str:
        """Name the pipeline that would build a missing upstream so the fix is obvious."""

        relation_name: str = prerequisite.relation_names[0]
        pipeline_name: str | None = self._producing_pipeline_name(key=prerequisite.key)
        if pipeline_name is None:
            return relation_name
        return f"{relation_name} (built by pipeline {pipeline_name}; add it to the selection)"

    def _producing_pipeline_name(self, *, key: LogicalResourceKey) -> str | None:
        compiled_pipeline: CompiledPipeline
        for compiled_pipeline in self._realized_project.project.pipelines:
            if any(model.key == key for model in compiled_pipeline.models):
                return compiled_pipeline.pipeline.name
        return None

    def _reject_incompatible_replay_inputs(
        self, *, replay_roots: tuple[DirectReplayRoot, ...]
    ) -> None:
        for root in replay_roots:
            relation: CatalogRelation | None = self._snapshot.catalog.relation(
                root.driving_input_relation_name
            )
            if relation is None:
                continue
            available_columns: frozenset[str] = frozenset(
                column.name for column in relation.columns
            )
            required_columns: tuple[str, ...] = self._required_replay_input_columns(root=root)
            missing_columns: tuple[str, ...] = tuple(
                column for column in required_columns if column not in available_columns
            )
            if not missing_columns:
                continue
            upstream_name: str = root.driving_input_key.name
            raise DirectPlanError(
                f"Direct plan cannot replay '{root.model_key.name}' from preserved upstream "
                f"relation '{root.driving_input_relation_name}': required replay columns are "
                f"missing: {', '.join(missing_columns)}. Rebuild the upstream scope in the same "
                f"invocation (for example, --select {upstream_name}) or select its pipeline."
            )

    def _required_replay_input_columns(self, *, root: DirectReplayRoot) -> tuple[str, ...]:
        columns: AdapterReplayColumns = root.driving_input_replay_columns
        mode: ReplayLineageMode = ReplayLineageMode(root.replay_boundary_mode)
        required_by_mode: dict[ReplayLineageMode, tuple[str, ...]] = {
            ReplayLineageMode.OFFSETS: (columns.partition, columns.offset),
            ReplayLineageMode.TIMESTAMP: (columns.timestamp,),
            ReplayLineageMode.LANDED_AT: (columns.landed_at,),
            ReplayLineageMode.CURSOR: (columns.cursor,),
        }
        required: list[str] = list(required_by_mode[mode])
        if self._effective_start_time is not None:
            if mode == ReplayLineageMode.OFFSETS:
                required.append(columns.landed_at or columns.timestamp)
            if mode == ReplayLineageMode.CURSOR:
                required.append(columns.timestamp)
        return tuple(dict.fromkeys(column for column in required if column))


def _teardown_operations(
    *, entries: tuple[DirectPlanEntry, ...]
) -> tuple[DirectRelationOperation, ...]:
    reversed_entries: tuple[DirectPlanEntry, ...] = tuple(reversed(entries))
    operations: list[DirectRelationOperation] = []
    resource_kind: DirectResourceKind
    for resource_kind in (
        DirectResourceKind.VIEW,
        DirectResourceKind.MATERIALIZED_VIEW,
        DirectResourceKind.TABLE,
    ):
        operations.extend(
            _relation_operations(
                entries=reversed_entries,
                action=DirectRelationAction.DROP,
                resource_kind=resource_kind,
            )
        )
    return tuple(operations)


def _creation_operations(
    *, entries: tuple[DirectPlanEntry, ...]
) -> tuple[DirectRelationOperation, ...]:
    operations: list[DirectRelationOperation] = []
    resource_kind: DirectResourceKind
    for resource_kind in (
        DirectResourceKind.TABLE,
        DirectResourceKind.MATERIALIZED_VIEW,
        DirectResourceKind.VIEW,
    ):
        operations.extend(
            _relation_operations(
                entries=entries,
                action=DirectRelationAction.CREATE,
                resource_kind=resource_kind,
            )
        )
    return tuple(operations)


def _relation_operations(
    *,
    entries: tuple[DirectPlanEntry, ...],
    action: DirectRelationAction,
    resource_kind: DirectResourceKind,
) -> tuple[DirectRelationOperation, ...]:
    operations: list[DirectRelationOperation] = []
    entry: DirectPlanEntry
    for entry in entries:
        relation_name: str
        relation_kind: DirectResourceKind
        for relation_name, relation_kind in zip(
            entry.relation_names, entry.resource_kinds, strict=True
        ):
            if relation_kind == resource_kind:
                operations.append(
                    DirectRelationOperation(
                        relation_name=relation_name,
                        action=action,
                        model_key=entry.model_key,
                        resource_kind=relation_kind,
                    )
                )
    return tuple(operations)


def _replay_lineage_modes_by_key(
    *, realized_project: RealizedProject
) -> dict[LogicalResourceKey, ReplayLineageMode]:
    replay_lineage_mode_by_key: dict[LogicalResourceKey, ReplayLineageMode] = {}
    pipeline: CompiledPipeline
    for pipeline in realized_project.project.pipelines:
        if pipeline.source is None or pipeline.effective_replay_lineage_mode is None:
            continue
        replay_lineage_mode_by_key[pipeline.source.key] = pipeline.effective_replay_lineage_mode
        model: CompiledModel
        for model in pipeline.models:
            replay_lineage_mode_by_key[model.key] = pipeline.effective_replay_lineage_mode
    return replay_lineage_mode_by_key


def _model_keys(*, keys: tuple[LogicalResourceKey, ...]) -> tuple[LogicalResourceKey, ...]:
    return tuple(key for key in keys if key.resource_type == LogicalResourceType.MODEL)
