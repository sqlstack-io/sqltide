"""Apache-2.0: SQLBuild compiler/discovery/_helpers/filesystem/aggregation.py@7e3b2f854f05."""

from collections.abc import Mapping
from pathlib import Path

from sqltide.compiler.discovery._helpers.load import load_pipeline_directories
from sqltide.compiler.discovery.constants import PIPELINE_CONFIG_FILE_NAME
from sqltide.compiler.discovery.models import (
    DiscoveredPipelineDirectory,
    DiscoveredProjectFile,
    ExternalTableSourceStep,
    KafkaLandingStep,
    LoadedPipeline,
    Pipeline,
    PostgresRefreshSourceStep,
    Project,
    ProjectNaming,
)
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.macros.models import MacroContext, MacroRegistry


def read_discovered_files(
    *, file_paths: tuple[Path, ...], project_dir: Path
) -> tuple[DiscoveredProjectFile, ...]:
    """Read sorted project paths into immutable source snapshots."""

    return tuple(
        DiscoveredProjectFile(
            file_path=file_path,
            relative_path=file_path.relative_to(project_dir),
            contents=file_path.read_bytes().decode("utf-8"),
        )
        for file_path in sorted(file_paths)
    )


def discover_pipeline_directories(
    *, pipelines_root: Path, project_dir: Path
) -> tuple[DiscoveredPipelineDirectory, ...]:
    """Discover direct pipeline directories and retain optional TOML configuration."""

    if not pipelines_root.is_dir():
        return ()
    pipeline_dirs: tuple[Path, ...] = tuple(
        sorted(path for path in pipelines_root.iterdir() if path.is_dir())
    )
    config_files: tuple[DiscoveredProjectFile, ...] = read_discovered_files(
        file_paths=tuple(
            pipeline_dir / PIPELINE_CONFIG_FILE_NAME
            for pipeline_dir in pipeline_dirs
            if (pipeline_dir / PIPELINE_CONFIG_FILE_NAME).is_file()
        ),
        project_dir=project_dir,
    )
    config_by_dir: dict[Path, DiscoveredProjectFile] = {
        config_file.file_path.parent: config_file for config_file in config_files
    }
    return tuple(
        DiscoveredPipelineDirectory(
            pipeline_dir=pipeline_dir,
            config_file=config_by_dir.get(pipeline_dir),
        )
        for pipeline_dir in pipeline_dirs
    )


def load_discovered_pipelines(
    *,
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...],
    model_files: tuple[DiscoveredProjectFile, ...],
    macro_registry: MacroRegistry,
    macro_context: MacroContext,
    sources_by_name: Mapping[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ],
    project: Project | None,
    project_naming: ProjectNaming,
    default_mode: PipelineMode,
) -> tuple[LoadedPipeline, ...]:
    """Parse loaded pipeline and model sources without rereading either kind."""

    model_contents_by_path: dict[Path, str] = {
        model_file.file_path: model_file.contents for model_file in model_files
    }
    pipelines: tuple[Pipeline, ...] = load_pipeline_directories(
        pipeline_directories=pipeline_directories,
        model_contents_by_path=model_contents_by_path,
        macro_registry=macro_registry,
        macro_context=macro_context,
        sources_by_name=sources_by_name,
        project_naming=project_naming,
        default_mode=default_mode,
    )
    return tuple(
        LoadedPipeline(
            pipeline=pipeline,
            file_path=pipeline_directory.pipeline_dir,
            project=project,
        )
        for pipeline_directory, pipeline in zip(pipeline_directories, pipelines, strict=True)
    )
