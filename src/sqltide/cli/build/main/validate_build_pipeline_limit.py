"""Absolute build-scope validation resolved from committed project configuration."""

from sqltide._native import resolve_selection
from sqltide.cli.build.exceptions import BuildPipelineLimitError
from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
)
from sqltide.compiler.compile.models import LogicalResourceKey
from sqltide.compiler.discovery.models import EffectiveProjectConfiguration, LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis


def validate_build_pipeline_limit(
    *,
    analysis: CompileAnalysis,
    selectors: tuple[str, ...] = (),
    preparation: (
        DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation | None
    ) = None,
) -> None:
    """Reject a build whose final execution closure spans too many pipelines."""

    if preparation is None:
        execution_keys: frozenset[LogicalResourceKey] = resolve_selection(
            realized_project=analysis.realized_project,
            graph=analysis.graph,
            selectors=selectors,
        ).execution_logical_model_keys
    else:
        execution_keys = _prepared_execution_keys(preparation)
    _validate_execution_keys(analysis=analysis, execution_keys=execution_keys)


def _validate_execution_keys(
    *, analysis: CompileAnalysis, execution_keys: frozenset[LogicalResourceKey]
) -> None:
    loaded_project: LoadedProject | None = analysis.discovered_inputs.loaded_project
    effective: EffectiveProjectConfiguration | None = (
        None if loaded_project is None else loaded_project.effective_configuration
    )
    if effective is None or effective.build.max_pipelines is None:
        return
    max_pipelines: int = effective.build.max_pipelines
    pipeline_names: tuple[str, ...] = tuple(
        sorted(
            {
                model.pipeline_name
                for model in analysis.compiled_project.models
                if model.key in execution_keys
            }
        )
    )
    if len(pipeline_names) <= max_pipelines:
        return
    raise BuildPipelineLimitError(
        f"Build affects {len(pipeline_names)} pipelines, exceeding max_pipelines={max_pipelines} "
        f"for target '{effective.target_name}': {', '.join(pipeline_names)}"
    )


def _prepared_execution_keys(
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
) -> frozenset[LogicalResourceKey]:
    if isinstance(preparation, MixedWorkflowPreparation):
        return frozenset(
            (
                *preparation.virtual.preview.run_execution_scope,
                *preparation.direct.preview.plan.execution_scope,
            )
        )
    if isinstance(preparation, VirtualWorkflowPreparation):
        return frozenset(preparation.preview.run_execution_scope)
    return frozenset(preparation.preview.plan.execution_scope)
