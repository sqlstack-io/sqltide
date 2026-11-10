"""Filesystem discovery for authored pipeline folders."""

from __future__ import annotations

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.discovery._helpers.project_inputs import (
    discover_pipeline_directories,
)
from sqltide.compiler.discovery._helpers.source_registry import source_registry_by_name
from sqltide.compiler.discovery.main.load_project_input_for_path import (
    load_project_input_for_path,
)
from sqltide.compiler.discovery.models import (
    DiscoveredPipelineDirectory,
    ExternalTableSourceStep,
    KafkaLandingStep,
    LoadedPipeline,
    LoadedProject,
    Pipeline,
    PostgresRefreshSourceStep,
    ProjectNaming,
)
from sqltide.compiler.discovery.types import PipelineMode


def discover_pipelines(root: Path) -> list[LoadedPipeline]:
    """Load all pipeline roots under a pipelines root directory."""

    loaded_project: LoadedProject | None = load_project_input_for_path(path=root)
    if loaded_project is None:
        return []
    sources_by_name: dict[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ] = source_registry_by_name(loaded_project.source_files)
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...] = discover_pipeline_directories(
        pipelines_root=root,
        project_dir=root.parent,
    )
    pipelines: tuple[Pipeline, ...] = _native.load_pipeline_directories(
        pipeline_directories=pipeline_directories,
        macro_registry=loaded_project.macro_registry,
        sources_by_name=sources_by_name,
        project_naming=(
            ProjectNaming()
            if loaded_project.effective_configuration is None
            else loaded_project.effective_configuration.naming
        ),
        default_mode=(
            PipelineMode.DIRECT
            if loaded_project.effective_configuration is None
            else PipelineMode(loaded_project.effective_configuration.defaults.pipeline_mode)
        ),
    )
    return [
        LoadedPipeline(
            pipeline=pipeline,
            file_path=pipeline_directory.pipeline_dir,
            project=loaded_project.project,
        )
        for pipeline_directory, pipeline in zip(pipeline_directories, pipelines, strict=True)
    ]
