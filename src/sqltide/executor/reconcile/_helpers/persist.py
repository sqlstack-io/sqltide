"""Persistence helpers for reconcile execution."""

from __future__ import annotations

from sqltide.adapter.constants import VIRTUAL_OBJECT_STATE_KIND_RECONCILE
from sqltide.adapter.models import AdapterMetadataState
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    DesiredView,
)
from sqltide.compiler.planner.main.build_adapter_metadata_state import (
    build_adapter_metadata_state,
)
from sqltide.compiler.planner.main.build_normalized_fingerprint import (
    build_normalized_fingerprint,
)
from sqltide.compiler.planner.models import MetadataState, ObjectStateRecord
from sqltide.executor.reconcile.models import ReconcilePreview


def build_reconcile_metadata_state(preview: ReconcilePreview) -> AdapterMetadataState:
    """Build reconciled object-state metadata for persistence."""

    state: MetadataState = MetadataState(
        object_states=preview.eligible_records,
        deployments=(),
        deployment_watermarks=(),
        publish_events=(),
    )
    return build_adapter_metadata_state(state)


def build_object_state_record(
    *,
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
    reconcile_id: str,
    recorded_at: str,
) -> ObjectStateRecord:
    normalized_query: str | None = (
        desired_object.query
        if isinstance(desired_object, (DesiredMaterializedView, DesiredView))
        else None
    )
    return ObjectStateRecord(
        deployment_id=reconcile_id,
        key=desired_object.key,
        normalized_fingerprint=build_normalized_fingerprint(desired_object.spec),
        normalized_query=normalized_query,
        recorded_at=recorded_at,
        state_kind=VIRTUAL_OBJECT_STATE_KIND_RECONCILE,
    )
