"""Selection resolution helpers for CLI commands."""

from __future__ import annotations

from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterTable,
    AdapterView,
)
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    DesiredView,
    LogicalResourceKey,
    ObjectKey,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.graph.exceptions import GraphInputError
from sqltide.compiler.graph.main.build_reverse_deps import build_reverse_deps
from sqltide.compiler.graph.main.resolve_named_model_keys import resolve_named_model_keys
from sqltide.compiler.graph.main.topologically_order_keys import topologically_order_keys
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.pipeline.types import AdapterResource


def resolve_selected_logical_model_keys(
    *,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selectors: tuple[str, ...],
) -> frozenset[LogicalResourceKey]:
    try:
        return resolve_named_model_keys(compiled_pipelines=compiled_pipelines, selectors=selectors)
    except GraphInputError as error:
        raise CliUserError(str(error)) from error


def expand_included_keys(
    *,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    realized_project: RealizedProject,
    desired_state: DesiredState,
    selected_model_keys: frozenset[ObjectKey],
) -> frozenset[ObjectKey]:
    reverse_deps: dict[ObjectKey, tuple[ObjectKey, ...]] = build_reverse_deps(desired_state)
    object_by_key: dict[ObjectKey, DesiredTable | DesiredMaterializedView | DesiredView] = {
        object_.key: object_
        for object_ in desired_state.objects
        if isinstance(object_, (DesiredTable, DesiredMaterializedView, DesiredView))
    }
    included_keys: set[ObjectKey] = set(selected_model_keys)
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in compiled_pipelines:
        pipeline_model_keys: frozenset[ObjectKey] = physical_model_keys(
            realized_project=realized_project,
            logical_model_keys=frozenset(model.key for model in compiled_pipeline.models),
        )
        if not (pipeline_model_keys & selected_model_keys):
            continue
        included_keys.update(
            pipeline_source_keys(
                compiled_pipeline=compiled_pipeline,
                realized_project=realized_project,
            )
        )
    stack: list[ObjectKey] = list(selected_model_keys)

    while stack:
        current_key: ObjectKey = stack.pop()
        downstream_key: ObjectKey
        for downstream_key in reverse_deps.get(current_key, ()):
            if downstream_key in included_keys:
                continue
            included_keys.add(downstream_key)
            stack.append(downstream_key)

    changed: bool = True
    while changed:
        changed = False
        key: ObjectKey
        for key in tuple(included_keys):
            desired_object: DesiredTable | DesiredMaterializedView | DesiredView | None = (
                object_by_key.get(key)
            )
            if desired_object is None:
                continue
            dep_key: ObjectKey
            for dep_key in desired_object.deps:
                if dep_key in included_keys:
                    continue
                included_keys.add(dep_key)
                changed = True

    return frozenset(included_keys)


def filter_desired_state(
    *,
    desired_state: DesiredState,
    included_keys: frozenset[ObjectKey],
) -> DesiredState:
    ordered_keys: tuple[ObjectKey, ...] = topologically_order_keys(
        desired_state=desired_state, included_keys=set(included_keys)
    )
    object_by_key: dict[
        ObjectKey, DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
    ] = {object_.key: object_ for object_ in desired_state.objects if object_.key in included_keys}
    filtered_objects: tuple[
        DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView, ...
    ] = tuple(object_by_key[key] for key in ordered_keys)
    return DesiredState(
        objects=filtered_objects,
        replay_anchor_keys=frozenset(
            key for key in desired_state.replay_anchor_keys if key in included_keys
        ),
        mutable_ref_warning_keys=frozenset(
            key for key in desired_state.mutable_ref_warning_keys if key in included_keys
        ),
        external_source_replay_configs=tuple(
            config
            for config in desired_state.external_source_replay_configs
            if config.key in included_keys
        ),
    )


def pipeline_source_keys(
    *, compiled_pipeline: CompiledPipeline, realized_project: RealizedProject
) -> frozenset[ObjectKey]:
    if compiled_pipeline.source is None:
        return frozenset()
    resources: tuple[AdapterResource, ...] = realized_project.resources_by_logical_key[
        compiled_pipeline.source.key
    ]
    keys: set[ObjectKey] = {_resource_key(resource) for resource in resources}
    keys.add(
        ObjectKey(
            database=None,
            object_type="table",
            name=realized_project.relation_name_by_logical_key[compiled_pipeline.source.key],
        )
    )
    return frozenset(keys)


def resolve_replay_lineage_mode(
    *,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selected_model_keys: frozenset[LogicalResourceKey],
) -> ReplayLineageMode | None:
    replay_lineage_modes: set[ReplayLineageMode] = set()
    selected_pipeline_modes: list[tuple[str, ReplayLineageMode]] = []
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in compiled_pipelines:
        pipeline_model_keys: frozenset[LogicalResourceKey] = frozenset(
            model.key for model in compiled_pipeline.models
        )
        if selected_model_keys and not (pipeline_model_keys & selected_model_keys):
            continue
        replay_lineage_mode: ReplayLineageMode | None = (
            compiled_pipeline.effective_replay_lineage_mode
        )
        if replay_lineage_mode is None:
            continue
        replay_lineage_modes.add(replay_lineage_mode)
        selected_pipeline_modes.append(
            (
                compiled_pipeline.pipeline.name,
                replay_lineage_mode,
            )
        )
    if len(replay_lineage_modes) > 1:
        mode_details: str = ", ".join(
            f"{pipeline_name}={replay_lineage_mode}"
            for pipeline_name, replay_lineage_mode in sorted(selected_pipeline_modes)
        )
        raise CliUserError(f"Selected pipelines disagree on replay_lineage_mode: {mode_details}")
    return next(iter(replay_lineage_modes), None)


def physical_model_keys(
    *,
    realized_project: RealizedProject,
    logical_model_keys: frozenset[LogicalResourceKey],
) -> frozenset[ObjectKey]:
    keys: set[ObjectKey] = set()
    logical_key: LogicalResourceKey
    for logical_key in logical_model_keys:
        if logical_key.resource_type != LogicalResourceType.MODEL:
            raise CliUserError(f"Cannot select non-model logical resource '{logical_key.name}'")
        resource: AdapterTable | AdapterView = next(
            resource
            for resource in realized_project.resources_by_logical_key[logical_key]
            if isinstance(resource, (AdapterTable, AdapterView))
        )
        keys.add(_resource_key(resource))
    return frozenset(keys)


def _resource_key(
    resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
) -> ObjectKey:
    object_type: str
    if isinstance(resource, AdapterManagedSource):
        object_type = "kafka_table"
    elif isinstance(resource, AdapterMaterializedView):
        object_type = "materialized_view"
    elif isinstance(resource, AdapterView):
        object_type = "view"
    else:
        object_type = "table"
    return ObjectKey(database=None, object_type=object_type, name=resource.name)
