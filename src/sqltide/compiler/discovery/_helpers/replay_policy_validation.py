"""Validation for virtual-environment-only authored replay policies."""

from collections.abc import Sequence
from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import LoadedPipeline, Project, TransformStep, ViewStep
from sqltide.compiler.discovery.types import (
    ModelFacts,
    PipelineFacts,
    PipelineMode,
    ProjectPolicyFacts,
)


def validate_replay_policies_for_mode(
    *,
    default_pipeline_mode: PipelineMode,
    project: Project | None,
    project_file_path: Path | None,
    loaded_pipelines: tuple[LoadedPipeline, ...],
) -> None:
    """Reject change-driven policy at every authored level in direct mode."""

    try:
        _native.validate_replay_policies(
            project_policy_facts(
                default_pipeline_mode=default_pipeline_mode,
                project=project,
                project_file_path=project_file_path,
            ),
            pipeline_facts(loaded_pipelines),
        )
    except ValueError as error:
        raise PipelineDiscoveryError(str(error)) from None


def project_policy_facts(
    *,
    default_pipeline_mode: PipelineMode,
    project: Project | None,
    project_file_path: Path | None,
) -> ProjectPolicyFacts | None:
    """Project-level replay policy facts for native validation."""

    if project is None:
        return None
    return (
        str(project_file_path),
        default_pipeline_mode == PipelineMode.DIRECT,
        project.replay_on_change is not None,
        project.bounded_replay_fallback is not None,
    )


def pipeline_facts(loaded_pipelines: tuple[LoadedPipeline, ...]) -> list[PipelineFacts]:
    """Pipeline identity and replay policy facts for native validation."""

    return [
        (
            loaded_pipeline.pipeline.name,
            str(loaded_pipeline.file_path),
            loaded_pipeline.pipeline.mode == PipelineMode.VIRTUAL,
            loaded_pipeline.pipeline.replay_on_change is not None,
            loaded_pipeline.pipeline.bounded_replay_fallback is not None,
            _model_facts(loaded_pipeline.pipeline.transforms),
        )
        for loaded_pipeline in loaded_pipelines
    ]


def _model_facts(models: Sequence[TransformStep | ViewStep]) -> list[ModelFacts]:
    facts: list[ModelFacts] = []
    model: TransformStep | ViewStep
    for model in models:
        if isinstance(model, TransformStep):
            facts.append(
                (
                    model.name,
                    True,
                    model.replay_on_change is not None,
                    model.bounded_replay_fallback is not None,
                )
            )
        else:
            facts.append((model.name, False, False, False))
    return facts
