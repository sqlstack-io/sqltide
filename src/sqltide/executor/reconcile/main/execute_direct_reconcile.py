"""Direct-mode reconcile execution entrypoint."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import CatalogSnapshot
from sqltide.compiler.compile.constants import RAW_TABLE_NAME_PREFIX
from sqltide.compiler.compile.models import (
    CompiledModel,
    DesiredState,
    DesiredTable,
    ObjectKey,
)
from sqltide.compiler.planner.models import ActualState
from sqltide.executor.direct.main._persist_reconciled_direct_fingerprints import (
    persist_reconciled_direct_fingerprints,
)
from sqltide.executor.reconcile._helpers.preview import build_reconcile_preview
from sqltide.executor.reconcile.exceptions import ReconcileError
from sqltide.executor.reconcile.models import ReconcilePreview, ReconcileResult
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock


def execute_direct_reconcile(
    *,
    client: AdapterConnection,
    target_database: str,
    metadata_database: str,
    desired_state: DesiredState,
    catalog: CatalogSnapshot,
    models: tuple[CompiledModel, ...],
    selected_model_keys: frozenset[ObjectKey],
    tool_version: str,
    apply: bool = False,
) -> ReconcilePreview | ReconcileResult:
    """Preview or persist structurally compatible direct model baselines."""

    actual_state: ActualState = _native.build_direct_reconcile_actual_state(
        desired_state=desired_state,
        catalog=catalog,
    )
    preview: ReconcilePreview = build_reconcile_preview(
        metadata_database=metadata_database,
        desired_state=desired_state,
        actual_state=actual_state,
        selected_model_keys=selected_model_keys,
    )
    if not apply:
        return preview
    rejected_keys: frozenset[ObjectKey] = frozenset(
        target.target_key for target in preview.rejected_targets
    )
    eligible_model_names: frozenset[str] = frozenset(
        object_.logical_model_name or object_.name
        for object_ in desired_state.objects
        if isinstance(object_, DesiredTable)
        and not object_.name.startswith(RAW_TABLE_NAME_PREFIX)
        and object_.key not in rejected_keys
        and (not selected_model_keys or object_.key in selected_model_keys)
    )
    eligible_models: tuple[CompiledModel, ...] = tuple(
        model for model in models if model.key.name in eligible_model_names
    )
    resolved_model_names: frozenset[str] = frozenset(model.key.name for model in eligible_models)
    unresolved_model_names: tuple[str, ...] = tuple(
        sorted(eligible_model_names - resolved_model_names)
    )
    if unresolved_model_names:
        raise ReconcileError(
            "Direct reconcile could not resolve compiled models for eligible targets: "
            f"{', '.join(unresolved_model_names)}"
        )
    with target_mutation_lock(connection=client, database=target_database):
        persist_reconciled_direct_fingerprints(
            models=eligible_models,
            target_database=target_database,
            metadata_database=metadata_database,
            workflow_id=preview.reconcile_id,
            tool_version=tool_version,
            connection=client,
        )
    return ReconcileResult(
        database=preview.database,
        reconcile_id=preview.reconcile_id,
        reconciled_records=preview.eligible_records,
        rejected_targets=preview.rejected_targets,
    )
