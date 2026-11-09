"""Resolve audit warmup against existing warehouse-native model history."""

from __future__ import annotations

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDirectFingerprintSnapshot,
)
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult


def load_model_anchors_impl(
    *,
    client: AdapterConnection,
    metadata_database: str,
    target_database: str,
    model_names: tuple[str, ...],
    virtual_environments: bool,
) -> dict[str, str]:
    """Load latest successful apply or publication timestamps by logical model."""

    if virtual_environments:
        inventory: AdapterDeploymentInventory = client.load_deployment_inventory(metadata_database)
        anchors: dict[str, str] = {}
        for event in inventory.publish_events:
            for model_name in event.logical_view_names:
                if model_name in model_names and event.published_at > anchors.get(model_name, ""):
                    anchors[model_name] = event.published_at
        return anchors
    snapshot: AdapterDirectFingerprintSnapshot = client.load_direct_fingerprints(
        database=metadata_database,
        logical_model_identities=tuple(
            f"{target_database}.{model_name}" for model_name in model_names
        ),
    )
    return {
        record.logical_model_identity.removeprefix(f"{target_database}."): record.applied_at
        for record in snapshot.baselines
        if record.applied_at is not None
    }


def deferred_audit_result_impl(*, audit: LoadedSqlAudit, state: AuditWarmupState) -> SqlAuditResult:
    """Represent a warming audit without fabricating a pass or failure."""

    return SqlAuditResult(
        file_path=audit.file_path,
        referenced_model_names=audit.referenced_model_names,
        severity=audit.severity,
        passed=False,
        failing_row_count=0,
        sample_column_names=(),
        sample_rows=(),
        description=audit.description,
        name=audit.name,
        deferred_until=state.eligible_at,
        missing_relation_names=state.missing_model_names,
    )
