"""Stable logical view creation helpers for publish."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterPreparedObjectMapping,
    AdapterStableBinding,
    AdapterStableBindingRemoval,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.executor.promotion.exceptions import PublishExecutionError
from sqltide.executor.promotion.models import (
    DeploymentPromotionPreview,
    PromotionBindingAddition,
    PromotionBindingRemoval,
    PromotionBindingReplacement,
    PromotionOrphanedRelation,
)
from sqltide.executor.promotion.types import PromotionPreviewClassification


def build_publish_binding_request(
    *,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
    deployment_id: str,
    inspected_state: InspectedManagedTableState | None = None,
) -> AdapterBindingReplacementRequest:
    """Build validated stable bindings for a staged deployment."""

    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
    deployment: AdapterDeploymentRecord | None = next(
        (
            candidate
            for candidate in inventory.deployments
            if candidate.deployment_id == deployment_id
        ),
        None,
    )
    if deployment is None:
        return _publish_inspected_stable_views(
            client=client,
            default_database=default_database,
            deployment_id=deployment_id,
            inspected_state=inspected_state,
        )
    if deployment.status == VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE:
        raise PublishExecutionError(
            f"Deployment '{deployment_id}' is incomplete and cannot be published"
        )
    existing_names: frozenset[str] = client.load_catalog(default_database).relation_names()
    bindings: tuple[AdapterStableBinding, ...] = _publish_bindings(
        relations=[
            (
                mapping.logical_key.database,
                mapping.logical_key.object_type,
                mapping.logical_key.name,
                mapping.physical_name,
            )
            for mapping in deployment.prepared_object_mappings
        ],
        existing_names=existing_names,
        default_database=default_database,
        deployment_id=deployment_id,
    )
    managed_state: InspectedManagedTableState = (
        inspected_state
        if inspected_state is not None
        else client.inspect_managed_table_state(default_database)
    )
    removals: list[tuple[str, str]] = _native.obsolete_binding_removals(
        _native_mappings(deployment.prepared_object_mappings),
        [
            (
                candidate.deployment_id,
                candidate.created_at,
                candidate.status == VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE,
                _native_mappings(candidate.prepared_object_mappings),
            )
            for candidate in inventory.deployments
        ],
        [
            (event.deployment_id, list(event.logical_view_names))
            for event in inventory.publish_events
        ],
        [
            (binding.database, binding.logical_name, binding.physical_name)
            for binding in managed_state.active_bindings
        ],
        default_database,
    )
    return AdapterBindingReplacementRequest(
        bindings=bindings,
        removals=tuple(
            AdapterStableBindingRemoval(database=database, logical_name=logical_name)
            for database, logical_name in removals
        ),
    )


def build_binding_replacement_preview(
    *,
    binding_request: AdapterBindingReplacementRequest,
    active_bindings: tuple[InspectedActiveTableBinding, ...],
) -> DeploymentPromotionPreview:
    """Classify the exact live-binding effects of an executable replacement request."""

    initial_publish: bool
    additions: list[int]
    replacements: list[tuple[int, str]]
    removals: list[tuple[int, str]]
    orphaned: list[tuple[str, str]]
    initial_publish, additions, replacements, removals, orphaned = (
        _native.binding_replacement_preview(
            _native_bindings(binding_request.bindings),
            [(removal.database, removal.logical_name) for removal in binding_request.removals],
            _native_bindings(active_bindings),
        )
    )
    bindings: tuple[AdapterStableBinding, ...] = binding_request.bindings
    return DeploymentPromotionPreview(
        classification=(
            PromotionPreviewClassification.INITIAL_PUBLISH
            if initial_publish
            else PromotionPreviewClassification.PROMOTION
        ),
        additions=tuple(
            PromotionBindingAddition(
                database=bindings[position].database,
                logical_name=bindings[position].logical_name,
                physical_name=bindings[position].physical_name,
            )
            for position in additions
        ),
        replacements=tuple(
            PromotionBindingReplacement(
                database=bindings[position].database,
                logical_name=bindings[position].logical_name,
                from_physical_name=from_physical_name,
                to_physical_name=bindings[position].physical_name,
            )
            for position, from_physical_name in replacements
        ),
        removals=tuple(
            PromotionBindingRemoval(
                database=binding_request.removals[position].database,
                logical_name=binding_request.removals[position].logical_name,
                physical_name=physical_name,
            )
            for position, physical_name in removals
        ),
        orphaned_relations=tuple(
            PromotionOrphanedRelation(database=database, physical_name=physical_name)
            for database, physical_name in orphaned
        ),
    )


def _publish_inspected_stable_views(
    *,
    client: AdapterConnection,
    default_database: str,
    deployment_id: str,
    inspected_state: InspectedManagedTableState | None,
) -> AdapterBindingReplacementRequest:
    managed_state: InspectedManagedTableState = (
        inspected_state
        if inspected_state is not None
        else client.inspect_managed_table_state(default_database)
    )
    try:
        positions: list[int] = _native.inspected_publish_candidates(
            [
                (candidate.physical_name, candidate.object_type)
                for candidate in managed_state.physical_candidates
            ],
            deployment_id,
        )
    except ValueError as error:
        raise PublishExecutionError(str(error)) from None
    candidates: tuple[InspectedPhysicalTableCandidate, ...] = tuple(
        managed_state.physical_candidates[position] for position in positions
    )
    existing_names: frozenset[str] = client.load_catalog(default_database).relation_names()
    return AdapterBindingReplacementRequest(
        bindings=_publish_bindings(
            relations=[
                (
                    candidate.database,
                    candidate.object_type,
                    candidate.logical_name,
                    candidate.physical_name,
                )
                for candidate in candidates
            ],
            existing_names=existing_names,
            default_database=default_database,
            deployment_id=deployment_id,
        )
    )


def _publish_bindings(
    *,
    relations: list[tuple[str | None, str, str, str]],
    existing_names: frozenset[str],
    default_database: str,
    deployment_id: str,
) -> tuple[AdapterStableBinding, ...]:
    try:
        bindings: list[tuple[str, str, str]] = _native.publish_stable_bindings(
            relations, set(existing_names), default_database, deployment_id
        )
    except ValueError as error:
        raise PublishExecutionError(str(error)) from None
    return tuple(
        AdapterStableBinding(
            database=database, logical_name=logical_name, physical_name=physical_name
        )
        for database, logical_name, physical_name in bindings
    )


def _native_mappings(
    mappings: tuple[AdapterPreparedObjectMapping, ...],
) -> list[tuple[str | None, bool, str, str, str]]:
    return [
        (
            mapping.logical_key.database,
            mapping.logical_key.object_type
            in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW},
            mapping.logical_key.name,
            mapping.physical_name,
            mapping.logical_model_name,
        )
        for mapping in mappings
    ]


def _native_bindings(
    bindings: tuple[AdapterStableBinding, ...] | tuple[InspectedActiveTableBinding, ...],
) -> list[tuple[str, str, str]]:
    return [(binding.database, binding.logical_name, binding.physical_name) for binding in bindings]
