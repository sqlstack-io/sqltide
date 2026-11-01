"""Load persisted object state into one planning warehouse snapshot."""

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import (
    VIRTUAL_OBJECT_STATE_KIND_RECONCILE,
)
from sqltide.adapter.exceptions import AdapterRelationNotFoundError
from sqltide.adapter.models import (
    AdapterDeploymentRecord,
    AdapterDeploymentWatermarkRecord,
    AdapterMetadataObjectKey,
    AdapterMetadataState,
    AdapterObjectStateRecord,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterStableBinding,
)
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.compile.types import DesiredObjectType
from sqltide.compiler.planner.models import (
    DeploymentRecord,
    MetadataState,
    ObjectStateMetadataRow,
    ObjectStateRecord,
    PreparedObjectMapping,
    PublishEventRecord,
)


def load_all_object_state_records(
    *,
    client: AdapterConnection,
    metadata_database: str,
) -> tuple[ObjectStateRecord, ...]:
    """Load every persisted object-state row into one planning snapshot."""

    try:
        rows: tuple[ObjectStateMetadataRow, ...] = client.query_many(
            statement=_native.render_clickhouse_object_state_records_query(metadata_database),
            decode=_decode_object_state_metadata_row,
        )
    except AdapterRelationNotFoundError:
        return ()
    return tuple(
        ObjectStateRecord(
            deployment_id=row.deployment_id,
            key=ObjectKey(
                database=row.database_name,
                object_type=row.object_type,
                name=row.object_name,
            ),
            normalized_fingerprint=row.normalized_fingerprint,
            normalized_query=row.normalized_query,
            recorded_at=row.recorded_at,
            observation_id=row.observation_id,
            state_kind=row.state_kind,
        )
        for row in rows
    )


def build_adapter_metadata_state(state: MetadataState) -> AdapterMetadataState:
    """Convert logical metadata records into the neutral persistence contract."""

    deployment_by_id: dict[str, DeploymentRecord] = {
        deployment.deployment_id: deployment for deployment in state.deployments
    }
    mapping_by_deployment_and_key: dict[tuple[str, ObjectKey], PreparedObjectMapping] = {}
    deployment: DeploymentRecord
    for deployment in state.deployments:
        mapping: PreparedObjectMapping
        for mapping in deployment.prepared_object_mappings:
            mapping_by_deployment_and_key[(deployment.deployment_id, mapping.logical_key)] = mapping
    return AdapterMetadataState(
        object_states=tuple(
            AdapterObjectStateRecord(
                deployment_id=record.deployment_id,
                key=_adapter_key(record.key),
                normalized_fingerprint=record.normalized_fingerprint,
                normalized_query=record.normalized_query,
                recorded_at=record.recorded_at,
                observation_id=record.observation_id,
                state_kind=record.state_kind,
                physical_database_name=record.key.database,
                physical_relation_name=(
                    mapping_by_deployment_and_key[(record.deployment_id, record.key)].physical_name
                    if (record.deployment_id, record.key) in mapping_by_deployment_and_key
                    else (
                        record.key.name
                        if record.state_kind == VIRTUAL_OBJECT_STATE_KIND_RECONCILE
                        else None
                    )
                ),
                logical_model_database=record.key.database,
                logical_model_name=(
                    mapping_by_deployment_and_key[
                        (record.deployment_id, record.key)
                    ].logical_model_name
                    if (record.deployment_id, record.key) in mapping_by_deployment_and_key
                    else record.key.name
                ),
                is_selected_root=(
                    record.deployment_id in deployment_by_id
                    and record.key in deployment_by_id[record.deployment_id].selected_root_keys
                ),
            )
            for record in state.object_states
        ),
        deployments=tuple(_adapter_deployment(record) for record in state.deployments),
        deployment_watermarks=tuple(
            AdapterDeploymentWatermarkRecord(
                deployment_id=record.deployment_id,
                root_key=_adapter_key(record.root_key),
                anchor_key=_adapter_key(record.anchor_key),
                boundary_key=record.boundary_key,
                cutoff_value=record.cutoff_value,
            )
            for record in state.deployment_watermarks
        ),
        publish_events=tuple(_adapter_publish_event(record) for record in state.publish_events),
    )


def _decode_object_state_metadata_row(row: Mapping[str, object]) -> ObjectStateMetadataRow:
    return ObjectStateMetadataRow(
        deployment_id=str(row["deployment_id"]),
        database_name=None if row["database_name"] is None else str(row["database_name"]),
        object_type=DesiredObjectType(str(row["object_type"])),
        object_name=str(row["object_name"]),
        normalized_fingerprint=str(row["normalized_fingerprint"]),
        normalized_query=None if row["normalized_query"] is None else str(row["normalized_query"]),
        recorded_at=str(row["recorded_at"]),
        observation_id=str(row["observation_id"]),
        state_kind=str(row["state_kind"]),
    )


def _adapter_key(key: ObjectKey) -> AdapterMetadataObjectKey:
    return AdapterMetadataObjectKey(
        database=key.database,
        object_type=str(key.object_type),
        name=key.name,
    )


def _adapter_mapping(mapping: PreparedObjectMapping) -> AdapterPreparedObjectMapping:
    return AdapterPreparedObjectMapping(
        logical_key=_adapter_key(mapping.logical_key),
        physical_name=mapping.physical_name,
        logical_model_name=mapping.logical_model_name,
    )


def _adapter_publish_event(record: PublishEventRecord) -> AdapterPublishEventRecord:
    return AdapterPublishEventRecord(
        deployment_id=record.deployment_id,
        published_at=record.published_at,
        logical_view_names=record.logical_view_names,
        bindings=tuple(
            AdapterStableBinding(
                database=record.database,
                logical_name=logical_name,
                physical_name=physical_name,
            )
            for logical_name, physical_name in zip(
                record.logical_view_names,
                record.physical_relation_names,
                strict=True,
            )
        ),
    )


def _adapter_deployment(deployment: DeploymentRecord) -> AdapterDeploymentRecord:
    return AdapterDeploymentRecord(
        deployment_id=deployment.deployment_id,
        created_at=deployment.created_at,
        status=deployment.status,
        replay_lineage_mode=str(deployment.replay_lineage_mode),
        selected_root_keys=tuple(_adapter_key(key) for key in deployment.selected_root_keys),
        warning_codes=deployment.warning_codes,
        prepared_object_mappings=tuple(
            _adapter_mapping(mapping) for mapping in deployment.prepared_object_mappings
        ),
        workflow_fingerprint=deployment.workflow_fingerprint,
        boundary_time=deployment.boundary_time,
        tool_version=deployment.tool_version,
    )
