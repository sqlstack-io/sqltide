"""Route connected workflow preparation through the effective project mode."""

import json
from dataclasses import replace

import sqltide._native as _native
from sqltide._native import (
    assemble_direct_build_workflow,
    assemble_virtual_build_workflow,
    normalize_cli_start_time,
    resolve_selected_logical_model_keys,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.build._helpers.audits import prepare_direct_build_audits
from sqltide.cli.build._helpers.confirmation import build_protection_requirements
from sqltide.cli.build._helpers.preview import build_direct_build_preview
from sqltide.cli.build._helpers.virtual_preview import build_virtual_build_preview
from sqltide.cli.build.constants import SQLTIDE_TOOL_VERSION
from sqltide.cli.build.models import (
    DirectBuildPreviewContext,
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualBuildPreviewContext,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.plan.main.render_direct_plan_text import render_direct_plan_text
from sqltide.cli.plan.main.render_plan_result import render_plan_result
from sqltide.compiler.compile.models import (
    CompiledTableModel,
    CompilerAdapterProfile,
    LogicalResourceKey,
)
from sqltide.compiler.discovery.models import PostgresRefreshSourceStep
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.backfill.models import BackfillBootstrapRequest
from sqltide.executor.direct.models import (
    DirectBuildAudit,
    DirectBuildRequest,
    DirectBuildWorkflow,
)
from sqltide.executor.workflow.models import BuildWorkflow


def prepare_build_workflow(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
) -> DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation:
    """Return one complete workflow assembled from fresh connected inspection."""

    _validate_common_flags(options=options)
    _reject_replayless_start_time(analysis=analysis, options=options)
    start_time_utc: str | None = _normalized_utc_start_time(options=options)
    if options.changed:
        _reject_direct_unsupported_flags(options=options)
        return prepare_direct_build_workflow(
            analysis=analysis,
            options=options,
            client=client,
            adapter_profile=adapter_profile,
            effective_start_time=start_time_utc,
        )
    selected_names_by_mode: dict[PipelineMode, tuple[str, ...]] = _selected_model_names_by_mode(
        analysis=analysis,
        selectors=options.selectors,
    )
    selected_modes: frozenset[PipelineMode] = frozenset(selected_names_by_mode)
    if selected_modes == {PipelineMode.VIRTUAL}:
        if options.include_missing_upstream:
            raise CliUserError("--include-missing-upstream is only supported for direct models")
        return prepare_virtual_build_workflow(
            analysis=analysis,
            options=options,
            start_time_utc=start_time_utc,
            client=client,
        )
    if selected_modes == {PipelineMode.DIRECT}:
        _reject_direct_unsupported_flags(options=options)
        return prepare_direct_build_workflow(
            analysis=analysis,
            options=options,
            client=client,
            adapter_profile=adapter_profile,
            effective_start_time=start_time_utc,
        )
    if selected_modes == {PipelineMode.DIRECT, PipelineMode.VIRTUAL}:
        virtual_options: WorkflowPreparationOptions = replace(
            options,
            selectors=selected_names_by_mode[PipelineMode.VIRTUAL],
            include_missing_upstream=False,
        )
        direct_options: WorkflowPreparationOptions = replace(
            options,
            selectors=selected_names_by_mode[PipelineMode.DIRECT],
            deployment_id=None,
            full_refresh=False,
        )
        virtual: VirtualWorkflowPreparation = prepare_virtual_build_workflow(
            analysis=analysis,
            options=virtual_options,
            start_time_utc=start_time_utc,
            client=client,
        )
        direct: DirectWorkflowPreparation = prepare_direct_build_workflow(
            analysis=analysis,
            options=direct_options,
            client=client,
            adapter_profile=adapter_profile,
            effective_start_time=start_time_utc,
        )
        return MixedWorkflowPreparation(
            virtual=virtual,
            direct=direct,
            plan_text=_render_mixed_plan_text(virtual=virtual, direct=direct),
            plan_json=_render_mixed_plan_json(virtual=virtual, direct=direct),
            protection_requirements=(
                *virtual.protection_requirements,
                *direct.protection_requirements,
            ),
        )
    if analysis.compile_inputs.virtual_environments:
        return prepare_virtual_build_workflow(
            analysis=analysis,
            options=options,
            start_time_utc=start_time_utc,
            client=client,
        )
    _reject_direct_unsupported_flags(options=options)
    return prepare_direct_build_workflow(
        analysis=analysis,
        options=options,
        client=client,
        adapter_profile=adapter_profile,
        effective_start_time=start_time_utc,
    )


def _selected_model_names_by_mode(
    *, analysis: CompileAnalysis, selectors: tuple[str, ...]
) -> dict[PipelineMode, tuple[str, ...]]:
    selected_keys: frozenset[LogicalResourceKey] = (
        resolve_selected_logical_model_keys(
            compiled_pipelines=analysis.compiled_project.pipelines,
            selectors=selectors,
        )
        if selectors
        else frozenset(model.key for model in analysis.compiled_project.models)
    )
    names_by_mode: dict[PipelineMode, list[str]] = {}
    for pipeline in analysis.compiled_project.pipelines:
        names: list[str] = [
            model.key.name for model in pipeline.models if model.key in selected_keys
        ]
        if names:
            names_by_mode.setdefault(PipelineMode(pipeline.pipeline.mode), []).extend(names)
    return {mode: tuple(sorted(names)) for mode, names in names_by_mode.items()}


def _render_mixed_plan_text(
    *, virtual: VirtualWorkflowPreparation, direct: DirectWorkflowPreparation
) -> str:
    return "\n".join(
        (
            "Mixed Build Plan",
            "Execution order  virtual (staged) -> direct (applied immediately)",
            "",
            "Phase 1/2  VIRTUAL - staged for later promotion",
            virtual.plan_text,
            "",
            "Phase 2/2  DIRECT - applied immediately after phase 1 succeeds",
            direct.plan_text,
            "",
            "The virtual deployment remains staged until it is promoted.",
        )
    )


def _render_mixed_plan_json(
    *, virtual: VirtualWorkflowPreparation, direct: DirectWorkflowPreparation
) -> str:
    return json.dumps(
        {
            "mode": "mixed",
            "execution_order": ["virtual", "direct"],
            "virtual": json.loads(virtual.workflow.plan_json),
            "direct": json.loads(direct.workflow.plan_json),
        },
        indent=2,
    )


def prepare_direct_build_workflow(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
    effective_start_time: str | None,
) -> DirectWorkflowPreparation:
    """Inspect connected direct state and assemble the complete workflow once."""

    preview: DirectBuildPreviewContext = build_direct_build_preview(
        options=options,
        client=client,
        analysis=analysis,
        effective_start_time=effective_start_time,
    )
    audits: tuple[DirectBuildAudit, ...] = prepare_direct_build_audits(
        preview=preview,
        adapter_profile=adapter_profile,
    )
    request: DirectBuildRequest = DirectBuildRequest(
        plan=preview.plan,
        realized_project=preview.analysis.realized_project,
        database=preview.database,
        metadata_database=preview.metadata_database,
        tool_version=SQLTIDE_TOOL_VERSION,
        audits=audits,
        row_error_policy_changes=preview.row_error_policy_changes,
    )
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=client,
        snapshot=preview.warehouse_snapshot,
        plan_json=_native.render_direct_plan_json(
            plan=preview.plan,
            adapter_name=preview.adapter_name,
            row_error_policy_changes=preview.row_error_policy_changes,
        ),
    )
    if workflow.recreated_sources:
        workflow = replace(
            workflow,
            template=replace(
                workflow.template,
                plan_json=_native.render_direct_plan_json(
                    plan=preview.plan,
                    adapter_name=preview.adapter_name,
                    source_recreations=workflow.recreated_sources,
                    row_error_policy_changes=preview.row_error_policy_changes,
                ),
            ),
        )
    return DirectWorkflowPreparation(
        preview=preview,
        request=request,
        workflow=workflow,
        plan_text=render_direct_plan_text(
            plan=preview.plan,
            adapter_name=preview.adapter_name,
            verbose=options.verbose,
            source_recreations=workflow.recreated_sources,
            row_error_policy_changes=preview.row_error_policy_changes,
        ),
        protection_requirements=build_protection_requirements(
            compiled_project=analysis.compiled_project,
            execution_model_keys=frozenset(preview.plan.execution_scope),
        ),
    )


def prepare_virtual_build_workflow(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    start_time_utc: str | None,
    client: AdapterConnection,
) -> VirtualWorkflowPreparation:
    """Inspect connected virtual state and assemble the complete workflow once."""

    preview: VirtualBuildPreviewContext = build_virtual_build_preview(
        options=options,
        start_time_utc=start_time_utc,
        client=client,
        analysis=analysis,
    )
    plan_text: str = render_plan_result(
        plan=preview.plan,
        desired_state=preview.desired_state,
        database=preview.database,
        adapter_name=client.adapter_identity.name,
        json_output=False,
        verbose=options.verbose,
    )
    serialized_plan_payload: dict[str, object] = json.loads(
        render_plan_result(
            plan=preview.plan,
            desired_state=preview.desired_state,
            database=preview.database,
            adapter_name=client.adapter_identity.name,
            json_output=True,
            verbose=options.verbose,
        )
    )
    serialized_plan_payload["deployment_created_at"] = preview.created_at
    if preview.row_error_policy_changes:
        serialized_plan_payload["row_error_policy_changes"] = [
            {
                "model_key": change.model_key.name,
                "desired_policy": str(change.desired_policy),
                "view": change.view_name,
                "dead_letter_view": change.dead_letter_view_name,
                "operation": "repair_landing_view" if change.landing_repair_only else "view_only",
                "replay_roots": 0,
            }
            for change in preview.row_error_policy_changes
        ]
        plan_text = "\n".join(
            [
                plan_text,
                *map(_native.render_row_error_policy_line, preview.row_error_policy_changes),
            ]
        )
    request: BackfillBootstrapRequest = BackfillBootstrapRequest(
        desired_state=preview.desired_state,
        default_database=preview.database,
        metadata_database=preview.metadata_database,
        replay_lineage_mode=preview.replay_lineage_mode,
        confirmed_plan=preview.plan,
        deployment_id=preview.deployment_id,
        full_refresh_keys=preview.full_refresh_keys,
        start_time_keys=preview.start_time_keys,
        start_time=preview.start_time,
        created_at=preview.created_at,
        confirmed_target_catalog=preview.target_catalog,
        confirmed_metadata_catalog=preview.metadata_catalog,
        row_error_policy_changes=preview.row_error_policy_changes,
    )
    workflow: BuildWorkflow = assemble_virtual_build_workflow(
        request=request,
        client=client,
        plan_json=json.dumps(serialized_plan_payload, indent=2) + "\n",
    )
    return VirtualWorkflowPreparation(
        preview=preview,
        request=request,
        workflow=workflow,
        plan_text=plan_text,
        protection_requirements=build_protection_requirements(
            compiled_project=analysis.compiled_project,
            execution_model_keys=preview.execution_logical_model_keys,
        ),
    )


def _validate_common_flags(*, options: WorkflowPreparationOptions) -> None:
    if options.changed and options.selectors:
        raise CliUserError("--changed cannot be combined with --select")
    if options.include_missing_upstream and not (options.changed or options.selectors):
        raise CliUserError("--include-missing-upstream requires --changed or --select")
    if options.full_refresh and options.start_time is not None:
        raise CliUserError("--full-refresh cannot be combined with --start-time")
    if (
        (options.full_refresh or options.start_time is not None)
        and not options.selectors
        and not options.changed
    ):
        required_flag: str = "--full-refresh" if options.full_refresh else "--start-time"
        raise CliUserError(f"{required_flag} requires --changed or at least one --select")


def _reject_replayless_start_time(
    *, analysis: CompileAnalysis, options: WorkflowPreparationOptions
) -> None:
    """Refuse a replay window when every selected model is refreshed by the warehouse."""

    if options.start_time is None or not options.selectors:
        return
    selected_keys: frozenset[LogicalResourceKey] = resolve_selected_logical_model_keys(
        compiled_pipelines=analysis.compiled_project.pipelines,
        selectors=options.selectors,
    )
    postgres_source_names: frozenset[str] = frozenset(
        source.key.name
        for source in analysis.compiled_project.sources
        if isinstance(source.source, PostgresRefreshSourceStep)
    )
    if not postgres_source_names:
        return
    replayless_names: tuple[str, ...] = tuple(
        sorted(
            model.key.name
            for model in analysis.compiled_project.models
            if model.key in selected_keys
            and isinstance(model, CompiledTableModel)
            and model.transform.source in postgres_source_names
        )
    )
    if replayless_names and len(replayless_names) == len(selected_keys):
        raise CliUserError(
            "--start-time is not available for scheduled postgres sources, which the "
            f"warehouse refreshes in full: {', '.join(replayless_names)}"
        )


def _normalized_utc_start_time(*, options: WorkflowPreparationOptions) -> str | None:
    if options.start_time is None:
        return None
    return normalize_cli_start_time(options.start_time)


def _reject_direct_unsupported_flags(*, options: WorkflowPreparationOptions) -> None:
    if options.deployment_id is not None:
        raise CliUserError("--deployment-id requires virtual environments")
    if options.full_refresh:
        raise CliUserError(
            "--full-refresh is a virtual-environment replay control and is not available in "
            "direct mode. Set defaults.pipeline_mode = 'virtual' or select a virtual pipeline "
            "to use it."
        )
