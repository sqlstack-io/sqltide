"""Apache-2.0: SQLBuild compiler/discovery/main/discover.py@7e3b2f854f05."""

from pathlib import Path

from sqltide.compiler.access.constants import ACCESS_POLICY_FILE_NAME
from sqltide.compiler.discovery._helpers.project_inputs import (
    discover_pipeline_directories,
    read_discovered_files,
)
from sqltide.compiler.discovery.models import (
    DiscoveredPipelineDirectory,
    DiscoveredProjectFile,
    DiscoveredProjectInputs,
    DiscoveredSourceFile,
    LoadedProject,
)
from sqltide.compiler.macros.constants import CONSTANTS_DIRECTORY_NAME, ENUMS_DIRECTORY_NAME
from sqltide.compiler.macros.main._discover_macro_files import discover_macro_files


def discover_project_inputs(
    *, pipelines_root: Path, loaded_project: LoadedProject | None
) -> DiscoveredProjectInputs:
    """Load all project source kinds once in stable path order, then validate the aggregate."""

    project_dir: Path = pipelines_root.parent
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...] = discover_pipeline_directories(
        pipelines_root=pipelines_root,
        project_dir=project_dir,
    )
    model_files: tuple[DiscoveredProjectFile, ...] = read_discovered_files(
        file_paths=tuple(pipelines_root.rglob("*.sql")),
        project_dir=project_dir,
    )
    test_files: tuple[DiscoveredProjectFile, ...] = read_discovered_files(
        file_paths=tuple((project_dir / "tests").rglob("*.sql")),
        project_dir=project_dir,
    )
    audit_files: tuple[DiscoveredProjectFile, ...] = read_discovered_files(
        file_paths=tuple((project_dir / "audits").rglob("*.sql")),
        project_dir=project_dir,
    )
    macro_files: tuple[DiscoveredProjectFile, ...] = (
        discover_macro_files(project_dir=project_dir)
        if loaded_project is None
        else loaded_project.macro_files
    )
    source_files: tuple[DiscoveredSourceFile, ...] = (
        () if loaded_project is None else loaded_project.source_files
    )
    access_path: Path = project_dir / ACCESS_POLICY_FILE_NAME
    access_files: tuple[DiscoveredProjectFile, ...] = read_discovered_files(
        file_paths=(access_path,) if access_path.is_file() else (),
        project_dir=project_dir,
    )
    return DiscoveredProjectInputs(
        project_dir=project_dir,
        loaded_project=loaded_project,
        source_files=source_files,
        pipeline_directories=pipeline_directories,
        model_files=model_files,
        test_files=test_files,
        audit_files=audit_files,
        macro_files=macro_files,
        access_file=access_files[0] if access_files else None,
        constant_files=read_discovered_files(
            file_paths=tuple((project_dir / CONSTANTS_DIRECTORY_NAME).rglob("*.sql")),
            project_dir=project_dir,
        ),
        enum_files=read_discovered_files(
            file_paths=tuple((project_dir / ENUMS_DIRECTORY_NAME).rglob("*.sql")),
            project_dir=project_dir,
        ),
    )
