"""Apache-2.0: SQLBuild compiler/discovery/_helpers/filesystem/aggregation.py@7e3b2f854f05."""

import sqltide._native as _native
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.discovery._helpers.replay_policy_validation import (
    pipeline_facts,
    project_policy_facts,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    DiscoveredSourceFile,
    LoadedPipeline,
    LoadedProject,
)
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.test_discovery.models import LoadedSqlTest


def validate_discovered_project_inputs(
    *,
    source_files: tuple[DiscoveredSourceFile, ...],
    loaded_project: LoadedProject | None,
    loaded_pipelines: tuple[LoadedPipeline, ...],
    loaded_tests: tuple[LoadedSqlTest, ...],
    loaded_audits: tuple[LoadedSqlAudit, ...],
) -> None:
    """Reject duplicate project identities after semantic inputs are attached."""

    try:
        _native.validate_project_inputs(
            _source_names(source_files),
            pipeline_facts(loaded_pipelines),
            _project_references(loaded_project),
            project_policy_facts(
                default_pipeline_mode=(
                    PipelineMode.DIRECT
                    if loaded_project is None or loaded_project.effective_configuration is None
                    else PipelineMode(loaded_project.effective_configuration.defaults.pipeline_mode)
                ),
                project=None if loaded_project is None else loaded_project.project,
                project_file_path=(
                    None if loaded_project is None else loaded_project.source_file.file_path
                ),
            ),
            [
                (test.name if test.name is not None else test.file_path.stem, str(test.file_path))
                for test in loaded_tests
            ],
            [
                (
                    audit.name if audit.name is not None else audit.file_path.stem,
                    str(audit.file_path),
                )
                for audit in loaded_audits
            ],
        )
    except ValueError as error:
        raise PipelineDiscoveryError(str(error)) from None


def _source_names(source_files: tuple[DiscoveredSourceFile, ...]) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    source_file: DiscoveredSourceFile
    for source_file in source_files:
        names.extend(
            (source.name, str(source_file.source_file.file_path)) for source in source_file.sources
        )
    return names


def _project_references(
    loaded_project: LoadedProject | None,
) -> tuple[str, list[tuple[str, str]]] | None:
    if loaded_project is None:
        return None
    return (
        str(loaded_project.source_file.file_path),
        [
            (reference.upstream_pipeline, reference.downstream_pipeline)
            for reference in loaded_project.project.dependencies.allowed_cross_pipeline_references
        ],
    )
