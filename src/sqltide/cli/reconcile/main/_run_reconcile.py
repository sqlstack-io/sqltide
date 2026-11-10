"""CLI command for metadata reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sqltide._native import analyze_project, load_actual_state
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.build.constants import SQLTIDE_TOOL_VERSION
from sqltide.cli.entry.main._resolve_default_database import resolve_default_database
from sqltide.cli.reconcile._helpers.rendering import (
    confirm_reconcile,
    render_reconcile_preview,
    render_reconcile_result,
)
from sqltide.cli.selection.main._selection import resolve_selection
from sqltide.cli.selection.models import SelectionResolution
from sqltide.compiler.compile.models import (
    CompilerAdapterProfile,
    DesiredState,
)
from sqltide.compiler.discovery.models import LoadedPipeline, LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import ActualState
from sqltide.executor.reconcile.main.execute_direct_reconcile import (
    execute_direct_reconcile,
)
from sqltide.executor.reconcile.main.execute_reconcile import execute_reconcile
from sqltide.executor.reconcile.models import (
    ReconcilePreview,
    ReconcileResult,
)


def run_reconcile(
    *,
    pipelines_root: Path,
    database: str | None,
    metadata_database: str | None,
    selectors: tuple[str, ...],
    json_output: bool,
    apply: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=pipelines_root,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
    )
    loaded_pipelines: tuple[LoadedPipeline, ...] = analysis.compile_inputs.pipelines
    resolved_database: str = resolve_default_database(
        loaded_pipelines=list(loaded_pipelines), override=database
    )
    resolved_metadata_database: str = metadata_database or resolved_database
    selection: SelectionResolution = resolve_selection(
        realized_project=analysis.realized_project,
        graph=analysis.graph,
        selectors=selectors,
    )
    desired_state: DesiredState = selection.desired_state
    preview: ReconcilePreview
    if analysis.compile_inputs.virtual_environments:
        actual_state: ActualState = load_actual_state(
            client=client, desired_state=desired_state, database=resolved_database
        )
        preview = cast(
            ReconcilePreview,
            execute_reconcile(
                client=client,
                target_database=resolved_database,
                metadata_database=resolved_metadata_database,
                desired_state=desired_state,
                actual_state=actual_state,
                selected_model_keys=selection.selected_model_keys,
            ),
        )
    else:
        preview = cast(
            ReconcilePreview,
            execute_direct_reconcile(
                client=client,
                target_database=resolved_database,
                metadata_database=resolved_metadata_database,
                desired_state=desired_state,
                catalog=client.load_catalog(resolved_database),
                models=analysis.realized_project.project.models,
                selected_model_keys=selection.selected_model_keys,
                tool_version=SQLTIDE_TOOL_VERSION,
            ),
        )
    print(render_reconcile_preview(preview=preview, json_output=json_output))
    if not apply:
        return 0
    if not confirm_reconcile():
        print("Reconcile cancelled.")
        return 1
    result: ReconcileResult
    if analysis.compile_inputs.virtual_environments:
        result = cast(
            ReconcileResult,
            execute_reconcile(
                client=client,
                target_database=resolved_database,
                metadata_database=resolved_metadata_database,
                desired_state=desired_state,
                actual_state=actual_state,
                selected_model_keys=selection.selected_model_keys,
                apply=True,
            ),
        )
    else:
        result = cast(
            ReconcileResult,
            execute_direct_reconcile(
                client=client,
                target_database=resolved_database,
                metadata_database=resolved_metadata_database,
                desired_state=desired_state,
                catalog=client.load_catalog(resolved_database),
                models=analysis.realized_project.project.models,
                selected_model_keys=selection.selected_model_keys,
                tool_version=SQLTIDE_TOOL_VERSION,
                apply=True,
            ),
        )
    print(render_reconcile_result(result=result, json_output=json_output))
    return 0
