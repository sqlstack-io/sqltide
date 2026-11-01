"""Preserve managed landing resources, recreate drifted Kafka sources, block on other drift."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterTable,
    AdapterView,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import (
    CompiledPipeline,
    CompiledTableModel,
    DesiredMaterializedView,
    LogicalResourceKey,
)
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.pipeline.models import RealizedProject
from sqltide.compiler.planner.models import DirectPlan
from sqltide.executor.direct.exceptions import DirectBuildError
from sqltide.executor.direct.models import DriftedManagedSource
from sqltide.executor.population.models import (
    PopulationManagedSource,
    PopulationRealization,
    PopulationSourcePreparation,
    PopulationSourceRecreation,
)

type _Resource = AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView
type _NativeResource = tuple[str, str, list[str], list[tuple[str, str]]]
type _NativePipeline = tuple[str, str, bool, int | None, list[int], list[str]]
type _NativeProject = tuple[
    list[str], list[_NativePipeline], list[int], list[list[_NativeResource]]
]
type _NativeRelation = tuple[str, list[tuple[str, str]], list[tuple[str, str]], str | None]


class _KeyIndex:
    """Positions of the logical keys passed to the native planner, in first-seen order."""

    def __init__(self) -> None:
        self.keys: list[LogicalResourceKey] = []
        self._positions: dict[LogicalResourceKey, int] = {}

    def position(self, key: LogicalResourceKey) -> int:
        if key not in self._positions:
            self._positions[key] = len(self.keys)
            self.keys.append(key)
        return self._positions[key]


def plan_preserved_managed_sources(
    *,
    plan: DirectPlan,
    realized_project: RealizedProject,
    catalog: CatalogSnapshot,
    database: str,
) -> tuple[PopulationSourcePreparation, tuple[PopulationRealization, ...]]:
    """Validate preserved sources and plan absent or recreated resources without mutation."""

    index: _KeyIndex = _KeyIndex()
    native_plan: tuple[list[int], list[int], list[tuple[str, int]]] = (
        [index.position(key) for key in plan.execution_scope],
        [index.position(item.key) for item in plan.prerequisite_scope],
        [
            (selection.pipeline_name, index.position(selection.source_key))
            for selection in plan.source_drift_selections
        ],
    )
    project: _NativeProject = _native_project(realized_project=realized_project, index=index)
    try:
        preserved, created, landing_view_names, managed, recreations, realizations = (
            _native.plan_preserved_managed_sources(
                native_plan, project, _native_relations(catalog), database
            )
        )
    except ValueError as error:
        raise DirectBuildError(str(error)) from None

    def resource(reference: tuple[int, int]) -> _Resource:
        return realized_project.resources_by_logical_key[index.keys[reference[0]]][reference[1]]

    landing_names: frozenset[str] = frozenset(landing_view_names)
    recreated: list[PopulationSourceRecreation] = []
    for source_reference, view_reference, consumer_group, drifted in recreations:
        source: _Resource = resource(source_reference)
        view: _Resource = resource(view_reference)
        if not isinstance(source, AdapterManagedSource) or not isinstance(
            view, AdapterMaterializedView
        ):
            raise DirectBuildError("Managed source recreation resolved unexpected resources")
        recreated.append(
            PopulationSourceRecreation(
                managed_source=PopulationManagedSource(resource=source, database=database),
                landing_view_name=view.name,
                landing_relation_name=view.target_relation_name,
                consumer_group=consumer_group,
                drifted_setting_names=tuple(drifted),
            )
        )
    return (
        PopulationSourcePreparation(
            preserved_relation_names=tuple(preserved),
            created_relation_names=tuple(created),
            landing_views=tuple(
                desired
                for desired in realized_project.desired_state.objects
                if isinstance(desired, DesiredMaterializedView) and desired.name in landing_names
            ),
            managed_sources=tuple(
                PopulationManagedSource(
                    resource=_managed_source(resource(reference)), database=database
                )
                for reference in managed
            ),
            recreated_sources=tuple(recreated),
        ),
        tuple(
            PopulationRealization(resource=_realized(resource(reference)), database=database)
            for reference in realizations
        ),
    )


def drifted_managed_sources(
    *, realized_project: RealizedProject, catalog: CatalogSnapshot
) -> tuple[DriftedManagedSource, ...]:
    """Return each deployed managed source whose settings drifted, with its sorted owners."""

    index: _KeyIndex = _KeyIndex()
    project: _NativeProject = _native_project(realized_project=realized_project, index=index)
    return tuple(
        DriftedManagedSource(
            source_key=index.keys[source],
            owner_pipeline_names=tuple(owners),
            drifted_setting_names=tuple(drifted),
        )
        for source, owners, drifted in _native.drifted_managed_sources(
            project, _native_relations(catalog)
        )
    )


def _native_project(*, realized_project: RealizedProject, index: _KeyIndex) -> _NativeProject:
    pipelines: list[_NativePipeline] = [
        _native_pipeline(pipeline=pipeline, index=index)
        for pipeline in realized_project.project.pipelines
    ]
    sources: list[int] = [index.position(source.key) for source in realized_project.project.sources]
    resources: list[list[_NativeResource]] = [
        _native_resources(realized_project=realized_project, key=key) for key in index.keys
    ]
    return ([key.name for key in index.keys], pipelines, sources, resources)


def _native_pipeline(*, pipeline: CompiledPipeline, index: _KeyIndex) -> _NativePipeline:
    return (
        pipeline.pipeline.name,
        repr(pipeline.pipeline.name),
        PipelineMode(pipeline.pipeline.mode) == PipelineMode.DIRECT,
        None if pipeline.source is None else index.position(pipeline.source.key),
        [index.position(model.key) for model in pipeline.models],
        [
            model.transform.source
            for model in pipeline.models
            if isinstance(model, CompiledTableModel)
        ],
    )


def _native_resources(
    *, realized_project: RealizedProject, key: LogicalResourceKey
) -> list[_NativeResource]:
    return [
        _native_resource(resource)
        for resource in realized_project.resources_by_logical_key.get(key, ())
    ]


def _native_resource(resource: _Resource) -> _NativeResource:
    if isinstance(resource, AdapterManagedSource):
        return (
            "managed_source",
            resource.name,
            [resource.broker_list, resource.topic, resource.format, resource.consumer_group],
            list(resource.settings),
        )
    if isinstance(resource, AdapterTable):
        return (
            "table",
            resource.name,
            [],
            [(column.name, column.type) for column in resource.columns],
        )
    if isinstance(resource, AdapterMaterializedView):
        return (
            "materialized_view",
            resource.name,
            [resource.source_relation_name, resource.target_relation_name],
            [],
        )
    return ("other", resource.name, [], [])


def _native_relations(catalog: CatalogSnapshot) -> list[_NativeRelation]:
    relations: list[_NativeRelation] = []
    for relation in catalog.relations:
        columns: list[tuple[str, str]] = [(column.name, column.type) for column in relation.columns]
        relations.append(
            (relation.name, list(relation.settings), columns, relation.target_relation_name)
        )
    return relations


def _managed_source(resource: _Resource) -> AdapterManagedSource:
    if isinstance(resource, AdapterManagedSource):
        return resource
    raise DirectBuildError(f"Resource '{resource.name}' is not a managed source")


def _realized(resource: _Resource) -> AdapterManagedSource | AdapterTable:
    if isinstance(resource, (AdapterManagedSource, AdapterTable)):
        return resource
    raise DirectBuildError(f"Resource '{resource.name}' cannot be realized directly")
