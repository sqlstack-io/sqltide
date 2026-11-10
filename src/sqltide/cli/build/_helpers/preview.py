"""Resolve the direct plan a build renders and confirms before it writes."""

from __future__ import annotations

import sqltide._native as _native
from sqltide._native import (
    DirectModelFingerprint,
    collect_reachable_keys,
    load_direct_warehouse_snapshot,
    plan_direct_build,
    plan_row_error_policy_changes,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterDirectFingerprintRecord
from sqltide.adapter.types import AdapterOptionalStateStatus
from sqltide.cli.build._helpers.confirmation import build_protection_requirements
from sqltide.cli.build.models import DirectBuildPreviewContext, WorkflowPreparationOptions
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.main._resolve_default_database import resolve_default_database
from sqltide.cli.selection.main._selection import resolve_selection
from sqltide.cli.selection.models import SelectionResolution
from sqltide.compiler.compile.models import CompiledTableModel, LogicalResourceKey
from sqltide.compiler.discovery.models import PostgresRefreshSourceStep
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.graph.constants import ALL_DEPENDENCY_EDGE_TYPES
from sqltide.compiler.graph.types import GraphTraversalDirection
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.compiler.planner.models import (
    DirectPlan,
    DirectRowErrorPolicyChange,
    DirectSourceDriftSelection,
    DirectWarehouseSnapshot,
)
from sqltide.compiler.planner.types import DirectSelectionMode
from sqltide.executor.direct.main.drifted_managed_sources import drifted_managed_sources
from sqltide.executor.direct.models import DriftedManagedSource


def build_direct_build_preview(
    *,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    analysis: CompileAnalysis,
    effective_start_time: str | None = None,
) -> DirectBuildPreviewContext:
    """Plan the selected direct closure from one shared compile analysis."""
    database: str = resolve_default_database(
        loaded_pipelines=list(analysis.compile_inputs.pipelines),
        override=options.database,
    )
    metadata_database: str = options.metadata_database or database
    snapshot: DirectWarehouseSnapshot = load_direct_warehouse_snapshot(
        client=client,
        database=database,
        metadata_database=metadata_database,
        logical_model_identities=tuple(
            f"{database}.{model.key.name}" for model in analysis.realized_project.project.models
        ),
    )
    start_time: str | None = effective_start_time
    _native.validate_declared_external_sources(
        catalog=snapshot.catalog,
        external_source_replay_configs=(
            analysis.realized_project.desired_state.external_source_replay_configs
        ),
        database=database,
    )
    selection_mode: DirectSelectionMode
    selected_model_keys: frozenset[LogicalResourceKey]
    source_drift_selections: tuple[DirectSourceDriftSelection, ...] = ()
    if options.changed:
        selection_mode = DirectSelectionMode.CHANGED
        sql_changed_model_keys: frozenset[LogicalResourceKey] = _changed_direct_model_keys(
            analysis=analysis,
            snapshot=snapshot,
            database=database,
        )
        source_drift_selections = _source_drift_selections(
            analysis=analysis,
            snapshot=snapshot,
            sql_changed_model_keys=sql_changed_model_keys,
        )
        _reject_source_drift_start_time(
            source_drift_selections=source_drift_selections,
            start_time=options.start_time,
        )
        selected_model_key_set: set[LogicalResourceKey] = set(sql_changed_model_keys)
        drift_selection: DirectSourceDriftSelection
        for drift_selection in source_drift_selections:
            selected_model_key_set.update(drift_selection.model_keys)
        selected_model_keys = frozenset(selected_model_key_set)
        _reject_changed_replayless_start_time(
            analysis=analysis,
            selected_model_keys=selected_model_keys,
            start_time=options.start_time,
        )
    else:
        selection: SelectionResolution = resolve_selection(
            realized_project=analysis.realized_project,
            graph=analysis.graph,
            selectors=options.selectors,
        )
        selected_model_keys = selection.selected_logical_model_keys
        selection_mode = (
            DirectSelectionMode.EXPLICIT if options.selectors else DirectSelectionMode.ALL_MODELS
        )
    plan: DirectPlan = plan_direct_build(
        graph=analysis.graph,
        realized_project=analysis.realized_project,
        snapshot=snapshot,
        database=database,
        selected_model_keys=selected_model_keys,
        selection_mode=selection_mode,
        include_missing_upstream=options.include_missing_upstream,
        effective_start_time=start_time,
        source_drift_selections=source_drift_selections,
    )
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = (
        plan_row_error_policy_changes(
            analysis=analysis, plan=plan, relations=snapshot.catalog.relations
        )
        if options.changed
        else ()
    )
    return DirectBuildPreviewContext(
        row_error_policy_changes=row_error_policy_changes,
        analysis=analysis,
        plan=plan,
        warehouse_snapshot=snapshot,
        database=database,
        metadata_database=metadata_database,
        adapter_name=client.adapter_identity.name,
        effective_start_time=start_time,
    )


def _changed_direct_model_keys(
    *, analysis: CompileAnalysis, snapshot: DirectWarehouseSnapshot, database: str
) -> frozenset[LogicalResourceKey]:
    if snapshot.fingerprints.status == AdapterOptionalStateStatus.UNAVAILABLE:
        detail: str = snapshot.fingerprints.warning or "direct fingerprints are unavailable"
        raise DirectPlanError(f"Cannot select changed models: {detail}")
    direct_model_key_set: set[LogicalResourceKey] = set()
    for pipeline in analysis.compiled_project.pipelines:
        if PipelineMode(pipeline.pipeline.mode) == PipelineMode.DIRECT:
            direct_model_key_set.update(model.key for model in pipeline.models)
    direct_model_keys: frozenset[LogicalResourceKey] = frozenset(direct_model_key_set)
    if not direct_model_keys:
        raise DirectPlanError("--changed is only supported for direct models")
    baseline_by_identity: dict[str, AdapterDirectFingerprintRecord] = {
        baseline.logical_model_identity: baseline for baseline in snapshot.fingerprints.baselines
    }
    return frozenset(
        model.key
        for model in analysis.compiled_project.models
        if model.key in direct_model_keys
        and DirectModelFingerprint.drift_reasons(
            model=model,
            baseline=baseline_by_identity.get(f"{database}.{model.key.name}"),
        )
    )


def _source_drift_selections(
    *,
    analysis: CompileAnalysis,
    snapshot: DirectWarehouseSnapshot,
    sql_changed_model_keys: frozenset[LogicalResourceKey],
) -> tuple[DirectSourceDriftSelection, ...]:
    """Add one direct owner per drifted source, preferring unprotected owners, then by name."""

    direct_models_by_pipeline: dict[str, tuple[LogicalResourceKey, ...]] = {}
    for pipeline in analysis.compiled_project.pipelines:
        if PipelineMode(pipeline.pipeline.mode) == PipelineMode.DIRECT:
            direct_models_by_pipeline[pipeline.pipeline.name] = tuple(
                model.key for model in pipeline.models
            )
    selected_keys: set[LogicalResourceKey] = set(sql_changed_model_keys)
    selections: list[DirectSourceDriftSelection] = []
    drifted: DriftedManagedSource
    for drifted in drifted_managed_sources(
        realized_project=analysis.realized_project,
        catalog=snapshot.catalog,
    ):
        direct_owners: tuple[str, ...] = tuple(
            sorted(
                (
                    name
                    for name in drifted.owner_pipeline_names
                    if name in direct_models_by_pipeline
                ),
                key=lambda name: (
                    _is_protected(analysis=analysis, model_keys=direct_models_by_pipeline[name]),
                    name,
                ),
            )
        )
        if not direct_owners or _rebuilds_any_pipeline(
            analysis=analysis,
            selected_keys=frozenset(selected_keys),
            pipeline_keys=tuple(direct_models_by_pipeline[name] for name in direct_owners),
        ):
            continue
        owner_keys: tuple[LogicalResourceKey, ...] = direct_models_by_pipeline[direct_owners[0]]
        selections.append(
            DirectSourceDriftSelection(
                source_key=drifted.source_key,
                pipeline_name=direct_owners[0],
                reader_pipeline_names=drifted.owner_pipeline_names,
                drifted_setting_names=drifted.drifted_setting_names,
                model_keys=tuple(key for key in owner_keys if key not in selected_keys),
            )
        )
        selected_keys.update(owner_keys)
    return tuple(selections)


def _is_protected(*, analysis: CompileAnalysis, model_keys: tuple[LogicalResourceKey, ...]) -> bool:
    """Report whether rebuilding these models would require a protection confirmation."""

    return bool(
        build_protection_requirements(
            compiled_project=analysis.compiled_project,
            execution_model_keys=frozenset(model_keys),
        )
    )


def _rebuilds_any_pipeline(
    *,
    analysis: CompileAnalysis,
    selected_keys: frozenset[LogicalResourceKey],
    pipeline_keys: tuple[tuple[LogicalResourceKey, ...], ...],
) -> bool:
    """Report whether the downstream closure of the selection reaches any of the pipelines."""

    if not selected_keys:
        return False
    rebuilt_keys: frozenset[LogicalResourceKey] = frozenset(
        collect_reachable_keys(
            graph=analysis.graph,
            root_keys=selected_keys,
            direction=GraphTraversalDirection.DOWNSTREAM,
            edge_types=ALL_DEPENDENCY_EDGE_TYPES,
        )
    )
    return any(rebuilt_keys.intersection(keys) for keys in pipeline_keys)


def _reject_source_drift_start_time(
    *,
    source_drift_selections: tuple[DirectSourceDriftSelection, ...],
    start_time: str | None,
) -> None:
    """Refuse a replay window that would discard history of pipelines only drift selected."""

    if start_time is None or not source_drift_selections:
        return
    names: tuple[str, ...] = tuple(
        f"pipeline:{selection.pipeline_name}" for selection in source_drift_selections
    )
    selectors: str = ", ".join(names)
    sources: str = ", ".join(
        f"'{selection.source_key.name}'" for selection in source_drift_selections
    )
    raise CliUserError(
        f"--changed selects {selectors} because the settings of source {sources} drifted from "
        "the project, and --start-time would rebuild their models from that time only, "
        f"discarding earlier history. Run `sqt build --select {' '.join(names)}` without "
        "--start-time first, then rerun with --changed --start-time."
    )


def _reject_changed_replayless_start_time(
    *,
    analysis: CompileAnalysis,
    selected_model_keys: frozenset[LogicalResourceKey],
    start_time: str | None,
) -> None:
    if start_time is None or not selected_model_keys:
        return
    postgres_source_names: frozenset[str] = frozenset(
        source.key.name
        for source in analysis.compiled_project.sources
        if isinstance(source.source, PostgresRefreshSourceStep)
    )
    replayless_names: tuple[str, ...] = tuple(
        sorted(
            model.key.name
            for model in analysis.compiled_project.models
            if model.key in selected_model_keys
            and isinstance(model, CompiledTableModel)
            and model.transform.source in postgres_source_names
        )
    )
    if replayless_names and len(replayless_names) == len(selected_model_keys):
        raise CliUserError(
            "--start-time is not available for scheduled postgres sources, which the "
            f"warehouse refreshes in full: {', '.join(replayless_names)}"
        )
