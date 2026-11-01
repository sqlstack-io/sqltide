"""Read-only assembly of frozen destruction plans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterManagedSource,
    AdapterManifestSnapshot,
    AdapterMaterializedView,
    AdapterQueryResult,
    AdapterTable,
    AdapterView,
    CatalogSnapshot,
)
from sqltide.adapter.types import AdapterOptionalStateStatus
from sqltide.compiler.compile.models import CompiledProject, LogicalResourceKey
from sqltide.compiler.compile.types import DesiredObjectType, LogicalResourceType
from sqltide.compiler.manifest.constants import MANIFEST_VERSION
from sqltide.compiler.manifest.main.resolve_manifest_project_identity import (
    resolve_manifest_project_identity,
)
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction._helpers.ordering import (
    reverse_topologically_order_relations,
)
from sqltide.executor.destruction.constants import (
    DEFAULT_DESTRUCTION_PLAN_TTL,
    PRODUCTION_TARGET_NAMES,
)
from sqltide.executor.destruction.exceptions import (
    DestructionDependencyError,
    DestructionExternalDependencyError,
    DestructionResourceError,
    DestructionSelectionError,
    DestructionValidationError,
)
from sqltide.executor.destruction.models import (
    DestructionPlan,
    DestructionPlanParts,
    DestructionRelationEvidence,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.destruction.types import (
    DestructionOperation,
    DestructionOwnership,
    DestructionPlanningConnection,
    DestructionPlanningErrorCode,
    DestructionRelationKind,
)

type _NativeKey = tuple[str, bool, str | None, list[tuple[str, str]]]
type _NativeProject = tuple[
    list[_NativeKey],
    list[list[int]],
    list[list[int]],
    list[tuple[str, int | None]],
    list[int],
    list[int],
]
type _NativeRequest = tuple[str, str, list[str], list[str], bool]
type _NativeOwned = tuple[str, str, list[str], list[str], list[str]]
type _NativeEvidence = tuple[
    str,
    str,
    str,
    bool,
    int | None,
    int | None,
    str | None,
    list[str],
    list[str],
    list[str],
    list[str],
]


def plan_destruction(
    *,
    request: DestructionRequest,
    analysis: CompileAnalysis,
    connection: DestructionPlanningConnection,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_DESTRUCTION_PLAN_TTL,
    plan_id: str | None = None,
) -> DestructionPlan:
    """Build a frozen impact plan using only read-only adapter interactions."""

    created_at: datetime = _validated_created_at(now=now, ttl=ttl)
    current_pipeline_names: tuple[str, ...] = _current_pipeline_names(
        request=request, analysis=analysis
    )
    supplied_pipeline_names: frozenset[str] = frozenset(
        (*request.pipeline_names, *request.included_dependent_pipeline_names)
    )
    historical_names_requested: bool = bool(supplied_pipeline_names - set(current_pipeline_names))
    historical_snapshot: AdapterManifestSnapshot | None = None
    if request.include_orphans or historical_names_requested:
        historical_snapshot = _load_manifest_snapshot(
            request=request,
            analysis=analysis,
            connection=connection,
        )
    project: _NativeProject = _native_project(analysis=analysis)
    native_request: _NativeRequest = (
        str(request.operation),
        request.database,
        list(request.pipeline_names),
        list(request.included_dependent_pipeline_names),
        request.include_orphans,
    )
    try:
        requested, included, affected = _native.resolve_destruction_selection(
            project,
            native_request,
            _native.destruction_available_pipelines(
                list(current_pipeline_names), historical_snapshot
            ),
        )
    except ValueError as error:
        _raise_planning_error(error)
    relation_drop_size_connection_limit: int | None = connection.load_relation_drop_size_limit()
    relation_drop_size_server_limit: int | None = (
        connection.load_relation_drop_size_server_default()
    )
    relation_drop_size_override: int | None = (
        analysis.compiled_project.destruction_relation_drop_size_limit
    )
    relation_drop_size_limit: int | None = (
        relation_drop_size_override
        if relation_drop_size_override is not None
        else relation_drop_size_connection_limit
    )
    catalog: CatalogSnapshot = connection.load_catalog(request.database)
    inventory: AdapterDeploymentInventory = connection.load_deployment_inventory(
        request.metadata_database
    )
    try:
        affected_model_names, affected_source_names, owned = _native.plan_destruction_scope(
            project,
            native_request,
            affected,
            list(current_pipeline_names),
            historical_snapshot,
            catalog,
            inventory,
        )
        relations: tuple[DestructionRelationEvidence, ...] = _relation_evidence(
            native_evidence=_native.build_destruction_evidence(
                request.database,
                owned,
                catalog,
                _load_relation_stats(
                    connection=connection,
                    database=request.database,
                    relation_names=tuple(relation[0] for relation in owned),
                ),
                relation_drop_size_limit,
            )
        )
    except ValueError as error:
        _raise_planning_error(error)
    _validate_external_dependants(
        connection=connection,
        database=request.database,
        owned_relation_names=frozenset(relation[0] for relation in owned),
    )
    _ = reverse_topologically_order_relations(relations)
    challenges: tuple[str, ...] = build_destruction_challenges(
        pipeline_names=tuple(affected),
        production_reset=(
            request.operation == DestructionOperation.RESET_TARGET
            and (
                request.target.casefold() in PRODUCTION_TARGET_NAMES
                or getattr(
                    getattr(analysis, "compiled_project", None),
                    "production_target",
                    False,
                )
            )
        ),
    )
    parts: DestructionPlanParts = DestructionPlanParts(
        plan_id=plan_id,
        created_at=created_at,
        ttl=ttl,
        requested_pipeline_names=tuple(requested),
        included_dependent_pipeline_names=tuple(included),
        affected_pipeline_names=tuple(affected),
        affected_model_names=tuple(affected_model_names),
        affected_source_names=tuple(affected_source_names),
        relations=relations,
        challenges=challenges,
        manifest_fingerprint=_fingerprint(_manifest_payload(analysis=analysis)),
        relation_drop_size_limit=relation_drop_size_limit,
        relation_drop_size_server_limit=relation_drop_size_server_limit,
        relation_drop_size_override=relation_drop_size_override,
        relation_drop_size_policy_observed=True,
        include_orphans=request.include_orphans or historical_names_requested,
    )
    return _build_plan(request=request, parts=parts)


def _validated_created_at(*, now: datetime | None, ttl: timedelta) -> datetime:
    created_at: datetime = now or datetime.now(tz=UTC)
    if created_at.tzinfo is None:
        raise DestructionValidationError("Destruction plan timestamps must be timezone-aware")
    if ttl <= timedelta(0):
        raise DestructionValidationError("Destruction plan TTL must be positive")
    return created_at


def inactive_pipelines(
    *,
    request: DestructionRequest,
    analysis: CompileAnalysis,
    connection: DestructionPlanningConnection,
) -> tuple[InactivePipeline, ...]:
    """List manifest-owned pipelines absent from the current compiled project."""

    current: tuple[str, ...] = _current_pipeline_names(request=request, analysis=analysis)
    snapshot: AdapterManifestSnapshot = _load_manifest_snapshot(
        request=request,
        analysis=analysis,
        connection=connection,
    )
    return tuple(
        InactivePipeline(
            name=name,
            model_count=model_count,
            resource_count=resource_count,
            last_published_at=last_published_at,
        )
        for name, model_count, resource_count, last_published_at in (
            _native.list_destruction_inactive_pipelines(list(current), snapshot)
        )
    )


def build_destruction_challenges(
    *, pipeline_names: tuple[str, ...], production_reset: bool = False
) -> tuple[str, ...]:
    """Return stable, ordered challenge values for the affected pipeline set."""

    try:
        return tuple(_native.build_destruction_challenges(list(pipeline_names), production_reset))
    except ValueError as error:
        _raise_planning_error(error)


def _current_pipeline_names(
    *, request: DestructionRequest, analysis: CompileAnalysis
) -> tuple[str, ...]:
    pipeline_names: tuple[str, ...] = tuple(
        sorted(pipeline.pipeline.name for pipeline in analysis.realized_project.project.pipelines)
    )
    compiled_target: str | None = analysis.realized_project.project.target_name
    if compiled_target is not None and request.target != compiled_target:
        raise DestructionSelectionError(
            f"Requested target {request.target!r} does not match compiled target "
            f"{compiled_target!r}"
        )
    return pipeline_names


def _native_project(*, analysis: CompileAnalysis) -> _NativeProject:
    project: CompiledProject = analysis.realized_project.project
    resources_by_key: Mapping[LogicalResourceKey, tuple[object, ...]] = (
        analysis.realized_project.resources_by_logical_key
    )
    index_by_key: dict[LogicalResourceKey, int] = {}
    candidate_keys: list[LogicalResourceKey] = [
        *analysis.graph.ordered_keys,
        *(model.key for model in project.models),
        *(source.key for source in project.sources),
        *(pipeline.source.key for pipeline in project.pipelines if pipeline.source is not None),
        *resources_by_key,
    ]
    for key in candidate_keys:
        index_by_key.setdefault(key, len(index_by_key))
    pipeline_by_model_key: dict[LogicalResourceKey, str] = {
        model.key: model.pipeline_name for model in project.models
    }
    keys: list[_NativeKey] = []
    upstream: list[list[int]] = []
    downstream: list[list[int]] = []
    for key in index_by_key:
        resources: list[tuple[str, str]] = [
            (resource.name, str(_resource_kind(resource)))
            for resource in resources_by_key.get(key, ())
        ]
        keys.append(
            (
                key.name,
                key.resource_type == LogicalResourceType.SOURCE,
                pipeline_by_model_key.get(key),
                resources,
            )
        )
        upstream.append(
            [
                index_by_key[edge.upstream_key]
                for edge in analysis.graph.upstream_edges_by_key.get(key, ())
            ]
        )
        downstream.append(
            [
                index_by_key[edge.downstream_key]
                for edge in analysis.graph.downstream_edges_by_key.get(key, ())
            ]
        )
    return (
        keys,
        upstream,
        downstream,
        [
            (
                pipeline.pipeline.name,
                None if pipeline.source is None else index_by_key[pipeline.source.key],
            )
            for pipeline in project.pipelines
        ],
        [index_by_key[source.key] for source in project.sources],
        [index_by_key[model.key] for model in project.models],
    )


def _raise_planning_error(error: ValueError) -> NoReturn:
    if not error.args or not isinstance(error.args[0], tuple):
        raise error
    code: DestructionPlanningErrorCode = DestructionPlanningErrorCode(error.args[0][0])
    detail: object = error.args[0][1]
    if code == DestructionPlanningErrorCode.SELECTION:
        raise DestructionSelectionError(str(detail)) from None
    if code == DestructionPlanningErrorCode.UNKNOWN_PIPELINES and isinstance(detail, list):
        raise DestructionSelectionError(f"Unknown pipelines: {tuple(detail)!r}") from None
    if code == DestructionPlanningErrorCode.DEPENDENCY and isinstance(detail, list):
        raise DestructionDependencyError(tuple(detail)) from None
    if code == DestructionPlanningErrorCode.UNSUPPORTED_HISTORICAL_KIND and isinstance(
        detail, tuple
    ):
        kind, name = detail
        raise DestructionResourceError(
            f"Unsupported historical manifest resource kind {kind!r} for {name!r}"
        ) from None
    if code == DestructionPlanningErrorCode.INVALID_OBJECT_TYPE:
        DesiredObjectType(str(detail))
    raise DestructionResourceError(str(detail)) from None


def _relation_evidence(
    *, native_evidence: list[_NativeEvidence]
) -> tuple[DestructionRelationEvidence, ...]:
    return tuple(_evidence(native=native) for native in native_evidence)


def _evidence(*, native: _NativeEvidence) -> DestructionRelationEvidence:
    (
        database,
        name,
        kind,
        exists,
        total_bytes,
        active_parts,
        catalog_fingerprint,
        logical_names,
        pipeline_names,
        ownership,
        dependency_relation_names,
    ) = native
    return DestructionRelationEvidence(
        database=database,
        name=name,
        kind=kind,
        exists=exists,
        total_bytes=total_bytes,
        active_parts=active_parts,
        catalog_fingerprint=catalog_fingerprint,
        logical_names=tuple(logical_names),
        pipeline_names=tuple(pipeline_names),
        ownership=tuple(DestructionOwnership(value) for value in ownership),
        dependency_relation_names=tuple(dependency_relation_names),
    )


def _native_evidence(relation: DestructionRelationEvidence) -> _NativeEvidence:
    return (
        relation.database,
        relation.name,
        str(relation.kind),
        relation.exists,
        relation.total_bytes,
        relation.active_parts,
        relation.catalog_fingerprint,
        list(relation.logical_names),
        list(relation.pipeline_names),
        [str(value) for value in relation.ownership],
        list(relation.dependency_relation_names),
    )


def _validate_external_dependants(
    *,
    connection: DestructionPlanningConnection,
    database: str,
    owned_relation_names: frozenset[str],
) -> None:
    blocked: tuple[str, ...] = connection.load_external_dependants(
        database=database,
        relation_names=tuple(sorted(owned_relation_names)),
    )
    if blocked:
        raise DestructionExternalDependencyError(tuple(sorted(set(blocked))))


def _build_plan(
    *,
    request: DestructionRequest,
    parts: DestructionPlanParts,
) -> DestructionPlan:
    plan_fingerprint: str = _native.destruction_plan_fingerprint(
        operation=str(request.operation),
        target=request.target,
        database=request.database,
        metadata_database=request.metadata_database,
        requested_pipeline_names=list(parts.requested_pipeline_names),
        included_dependent_pipeline_names=list(parts.included_dependent_pipeline_names),
        affected_pipeline_names=list(parts.affected_pipeline_names),
        affected_model_names=list(parts.affected_model_names),
        affected_source_names=list(parts.affected_source_names),
        relations=[_native_evidence(relation) for relation in parts.relations],
        challenges=list(parts.challenges),
        manifest_fingerprint=parts.manifest_fingerprint,
        relation_drop_size_limit=parts.relation_drop_size_limit,
        relation_drop_size_server_limit=parts.relation_drop_size_server_limit,
        relation_drop_size_override=parts.relation_drop_size_override,
        relation_drop_size_policy_observed=parts.relation_drop_size_policy_observed,
        include_orphans=parts.include_orphans,
    )
    return DestructionPlan(
        plan_id=parts.plan_id or f"destruction_{uuid4().hex}",
        operation=DestructionOperation(request.operation),
        target=request.target,
        database=request.database,
        metadata_database=request.metadata_database,
        requested_pipeline_names=parts.requested_pipeline_names,
        included_dependent_pipeline_names=parts.included_dependent_pipeline_names,
        affected_pipeline_names=parts.affected_pipeline_names,
        affected_model_names=parts.affected_model_names,
        affected_source_names=parts.affected_source_names,
        relations=parts.relations,
        challenges=parts.challenges,
        preserves_sources=False,
        preserves_replay_data=False,
        manifest_fingerprint=parts.manifest_fingerprint,
        plan_fingerprint=plan_fingerprint,
        created_at=parts.created_at,
        expires_at=parts.created_at + parts.ttl,
        relation_drop_size_limit=parts.relation_drop_size_limit,
        relation_drop_size_server_limit=parts.relation_drop_size_server_limit,
        relation_drop_size_override=parts.relation_drop_size_override,
        relation_drop_size_policy_observed=parts.relation_drop_size_policy_observed,
        include_orphans=parts.include_orphans,
    )


def _load_manifest_snapshot(
    *,
    request: DestructionRequest,
    analysis: CompileAnalysis,
    connection: DestructionPlanningConnection,
) -> AdapterManifestSnapshot:
    snapshot: AdapterManifestSnapshot = connection.load_manifests(
        database=request.metadata_database,
        project_identity=resolve_manifest_project_identity(analysis=analysis),
        target_name=request.target,
        target_database=request.database,
    )
    if snapshot.status == AdapterOptionalStateStatus.UNAVAILABLE:
        raise DestructionResourceError(snapshot.warning or "Manifest history is unavailable")
    for manifest in snapshot.manifests:
        if manifest.manifest_version != MANIFEST_VERSION:
            raise DestructionResourceError(
                f"Unsupported manifest version {manifest.manifest_version}; "
                f"expected {MANIFEST_VERSION}"
            )
    return snapshot


def _load_relation_stats(
    *,
    connection: DestructionPlanningConnection,
    database: str,
    relation_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    if not relation_names:
        return {}
    names_sql: str = ", ".join(_quote_string(name) for name in relation_names)
    result: AdapterQueryResult = connection.query(
        "SELECT table AS relation_name, sum(bytes_on_disk) AS total_bytes, "
        "count() AS active_parts FROM system.parts WHERE active = 1 "
        f"AND database = {_quote_string(database)} AND table IN ({names_sql}) "
        "GROUP BY table ORDER BY table"
    )
    stats: dict[str, tuple[int, int]] = {}
    for row in result.named_rows():
        stats[str(row["relation_name"])] = (
            int(str(row["total_bytes"])),
            int(str(row["active_parts"])),
        )
    return stats


def _manifest_payload(*, analysis: CompileAnalysis) -> dict[str, object]:
    project: CompiledProject = analysis.realized_project.project
    return {
        "project_name": project.project_name,
        "target_name": project.target_name,
        "effective_target": getattr(
            getattr(analysis, "compile_inputs", None), "effective_target", None
        ),
        "variables": getattr(getattr(analysis, "compile_inputs", None), "variables", ()),
        "access_policy_fingerprint": getattr(
            getattr(analysis, "access_policy", None), "fingerprint", None
        ),
        "pipelines": tuple(sorted(pipeline.pipeline.name for pipeline in project.pipelines)),
        "models": tuple(
            {
                "name": model.key.name,
                "pipeline": model.pipeline_name,
                "kind": model.kind,
                "relation_name": analysis.realized_project.relation_name_by_logical_key.get(
                    model.key
                ),
                "query": analysis.realized_project.resolved_query_by_model_key.get(model.key),
                "resources": analysis.realized_project.resources_by_logical_key.get(model.key, ()),
            }
            for model in sorted(project.models, key=lambda value: value.key.name)
        ),
        "sources": tuple(
            {
                "name": source.key.name,
                "definition": getattr(source, "source", None),
                "relation_name": analysis.realized_project.relation_name_by_logical_key.get(
                    source.key
                ),
                "resources": analysis.realized_project.resources_by_logical_key.get(source.key, ()),
            }
            for source in sorted(project.sources, key=lambda value: value.key.name)
        ),
        "edges": _manifest_edges(analysis=analysis),
    }


def _manifest_edges(*, analysis: CompileAnalysis) -> tuple[tuple[str, str, object], ...]:
    edge_payloads: list[tuple[str, str, object]] = []
    for edges in analysis.graph.downstream_edges_by_key.values():
        for edge in edges:
            edge_payloads.append(
                (
                    edge.upstream_key.name,
                    edge.downstream_key.name,
                    edge.edge_type,
                )
            )
    return tuple(sorted(edge_payloads, key=lambda value: (value[0], value[1], str(value[2]))))


def _resource_kind(resource: object) -> DestructionRelationKind:
    if isinstance(resource, AdapterView):
        return DestructionRelationKind.VIEW
    if isinstance(resource, AdapterMaterializedView):
        return DestructionRelationKind.MATERIALIZED_VIEW
    if isinstance(resource, AdapterManagedSource):
        return DestructionRelationKind.MANAGED_SOURCE
    if isinstance(resource, AdapterTable):
        return DestructionRelationKind.TABLE
    raise DestructionResourceError(f"Unsupported manifest resource: {type(resource).__name__}")


def _quote_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _fingerprint(value: object) -> str:
    canonical: str = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        items: list[object] = [_canonical(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
        return items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)
