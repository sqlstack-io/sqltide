"""Canonical project manifest construction."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterManifest,
    AdapterManifestResource,
    AdapterTable,
    AdapterView,
)
from sqltide.compiler.compile.models import CompiledProject, LogicalResourceKey
from sqltide.compiler.graph.constants import ALL_DEPENDENCY_EDGE_TYPES
from sqltide.compiler.graph.main.collect_reachable_keys import collect_reachable_keys
from sqltide.compiler.graph.types import GraphTraversalDirection
from sqltide.compiler.manifest.constants import MANIFEST_VERSION
from sqltide.compiler.manifest.types import ManifestAdapterResource
from sqltide.compiler.pipeline.models import CompileAnalysis


def build_manifest_record(
    *,
    analysis: CompileAnalysis,
    invocation_id: str,
    project_identity: str,
    target_database: str,
    tool_version: str,
    project_revision: str | None,
    published_at: str | None,
) -> AdapterManifest:
    """Return one complete, deterministically fingerprinted project manifest."""

    project: CompiledProject = analysis.realized_project.project
    target_name: str = project.target_name or ""
    pipeline_names, resource_identities, fingerprint = _native.build_manifest_contents(
        project_identity,
        target_name,
        target_database,
        project.production_target,
        [pipeline.pipeline.name for pipeline in project.pipelines],
        _logical_resources(analysis=analysis),
    )
    resources: tuple[AdapterManifestResource, ...] = tuple(
        AdapterManifestResource(
            pipeline_name=pipeline_name,
            logical_type=logical_type,
            logical_name=logical_name,
            resource_role=resource_role,
            resource_database=resource_database,
            resource_name=resource_name,
            resource_kind=resource_kind,
        )
        for (
            pipeline_name,
            logical_type,
            logical_name,
            resource_role,
            resource_database,
            resource_name,
            resource_kind,
        ) in resource_identities
    )
    return AdapterManifest(
        manifest_id=str(uuid4()),
        invocation_id=invocation_id,
        project_identity=project_identity,
        target_name=target_name,
        target_database=target_database,
        is_production=project.production_target,
        project_revision=project_revision,
        manifest_fingerprint=fingerprint,
        manifest_version=MANIFEST_VERSION,
        pipelines=tuple(pipeline_names),
        resources=resources,
        tool_version=tool_version,
        published_at=published_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
    )


def _logical_resources(
    *, analysis: CompileAnalysis
) -> list[tuple[str, str, list[str], list[tuple[str, str]]]]:
    pipeline_names_by_key: dict[LogicalResourceKey, tuple[str, ...]] = (
        _pipeline_names_by_logical_key(analysis=analysis)
    )
    logical_resources: list[tuple[str, str, list[str], list[tuple[str, str]]]] = []
    for key, realized_resources in analysis.realized_project.resources_by_logical_key.items():
        resources: list[tuple[str, str]] = [
            (_resource_kind(resource), resource.name) for resource in realized_resources
        ]
        logical_resources.append(
            (str(key.resource_type), key.name, list(pipeline_names_by_key.get(key, ())), resources)
        )
    return logical_resources


def _pipeline_names_by_logical_key(
    *, analysis: CompileAnalysis
) -> dict[LogicalResourceKey, tuple[str, ...]]:
    names: dict[LogicalResourceKey, set[str]] = {}
    for pipeline in analysis.realized_project.project.pipelines:
        if pipeline.source is not None:
            names.setdefault(pipeline.source.key, set()).add(pipeline.pipeline.name)
        for model in pipeline.models:
            names.setdefault(model.key, set()).add(pipeline.pipeline.name)
    pipeline_name_by_model_key: dict[LogicalResourceKey, str] = {
        model.key: model.pipeline_name for model in analysis.realized_project.project.models
    }
    for source in analysis.realized_project.project.sources:
        downstream_keys: tuple[LogicalResourceKey, ...] = collect_reachable_keys(
            graph=analysis.graph,
            root_keys=frozenset((source.key,)),
            direction=GraphTraversalDirection.DOWNSTREAM,
            edge_types=ALL_DEPENDENCY_EDGE_TYPES,
        )
        names.setdefault(source.key, set()).update(
            pipeline_name_by_model_key[key]
            for key in downstream_keys
            if key in pipeline_name_by_model_key
        )
    return {key: tuple(sorted(values)) for key, values in names.items()}


def _resource_kind(resource: ManifestAdapterResource) -> str:
    if isinstance(resource, AdapterManagedSource):
        return "managed_source"
    if isinstance(resource, AdapterTable):
        return "table"
    if isinstance(resource, AdapterView):
        return "view"
    return "materialized_view"
