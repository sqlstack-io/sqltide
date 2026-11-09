"""Metadata persistence for backfill bootstrap execution."""

from sqltide.adapter.constants import VIRTUAL_OBJECT_STATE_KIND_DEPLOYMENT
from sqltide.adapter.models import AdapterMetadataState
from sqltide.compiler.compile.models import DesiredMaterializedView, DesiredView, ObjectKey
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.planner.main.build_adapter_metadata_state import (
    build_adapter_metadata_state,
)
from sqltide.compiler.planner.main.build_metadata_state import build_metadata_state
from sqltide.compiler.planner.main.build_normalized_fingerprint import (
    build_normalized_fingerprint,
)
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    DeploymentRecord,
    MetadataState,
    ObjectStateRecord,
    PreparedObjectMapping,
)
from sqltide.compiler.planner.types import DesiredObject
from sqltide.executor.backfill.constants import DEPLOYMENT_STATUS_BACKFILLING


def build_deployment_metadata_state(
    *,
    deployment_plan: DeploymentPlan,
    desired_objects: tuple[DesiredObject, ...],
    deployment_id: str,
    created_at: str,
    replay_lineage_mode: ReplayLineageMode,
    workflow_fingerprint: str,
    tool_version: str,
) -> AdapterMetadataState:
    """Build the complete candidate metadata batch without warehouse mutation."""

    metadata_state: MetadataState = build_metadata_state(
        object_states=_build_object_state_records(
            desired_objects=desired_objects,
            deployment_id=deployment_id,
            recorded_at=created_at,
        ),
        deployments=(
            DeploymentRecord(
                deployment_id=deployment_id,
                created_at=created_at,
                status=DEPLOYMENT_STATUS_BACKFILLING,
                replay_lineage_mode=replay_lineage_mode,
                selected_root_keys=tuple(
                    subtree.root_key for subtree in deployment_plan.rebuild_subtrees
                ),
                warning_codes=tuple(warning.warning_code for warning in deployment_plan.warnings),
                prepared_object_mappings=tuple(
                    PreparedObjectMapping(
                        logical_key=prepared_object.logical_key,
                        physical_name=prepared_object.physical_name,
                        logical_model_name=prepared_object.logical_model_name,
                    )
                    for prepared_object in deployment_plan.prepared_shadow_objects
                ),
                workflow_fingerprint=workflow_fingerprint,
                tool_version=tool_version,
            ),
        ),
        deployment_watermarks=(),
        publish_events=(),
    )
    return build_adapter_metadata_state(metadata_state)


def _build_object_state_records(
    *,
    desired_objects: tuple[DesiredObject, ...],
    deployment_id: str,
    recorded_at: str,
) -> tuple[ObjectStateRecord, ...]:
    desired_object: DesiredObject
    records: list[ObjectStateRecord] = []
    for desired_object in desired_objects:
        normalized_query: str | None = None
        if isinstance(desired_object, (DesiredMaterializedView, DesiredView)):
            normalized_query = (
                desired_object.comparison_spec.query
                if isinstance(desired_object, DesiredMaterializedView)
                else desired_object.query
            )
        records.append(
            ObjectStateRecord(
                deployment_id=deployment_id,
                key=ObjectKey(
                    database=desired_object.key.database,
                    object_type=desired_object.key.object_type,
                    name=desired_object.key.name,
                ),
                normalized_fingerprint=build_normalized_fingerprint(
                    desired_object.comparison_spec
                    if isinstance(desired_object, DesiredMaterializedView)
                    else desired_object.spec
                ),
                normalized_query=normalized_query,
                recorded_at=recorded_at,
                state_kind=VIRTUAL_OBJECT_STATE_KIND_DEPLOYMENT,
            )
        )

    return tuple(records)
