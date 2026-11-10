"""Prepare one build and enforce its static, dynamic, and authorized scope."""

from sqltide._native import validate_build_pipeline_limit
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.build.classes.prepared_build_scope import PreparedBuildScope
from sqltide.cli.build.main.prepare_build_workflow import prepare_build_workflow
from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.pipeline.models import CompileAnalysis


def prepare_validated_build(
    *,
    analysis: CompileAnalysis,
    options: WorkflowPreparationOptions,
    client: AdapterConnection,
    adapter_profile: CompilerAdapterProfile,
) -> DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation:
    """Prepare the exact connected workflow and enforce every configured scope gate."""

    dynamic_selection: bool = options.changed or options.include_missing_upstream
    if not dynamic_selection:
        validate_build_pipeline_limit(analysis=analysis, selectors=options.selectors)
    preparation: (
        DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation
    ) = prepare_build_workflow(
        analysis=analysis,
        options=options,
        client=client,
        adapter_profile=adapter_profile,
    )
    if dynamic_selection:
        validate_build_pipeline_limit(analysis=analysis, preparation=preparation)
    PreparedBuildScope.validate_expected(preparation=preparation, analysis=analysis)
    return preparation
