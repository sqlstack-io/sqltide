"""Load project config with retained source for one connection-free invocation."""

from collections.abc import Mapping
from pathlib import Path

from sqltide.compiler.discovery._helpers.configuration import (
    find_project_configuration_dir,
    load_project_configuration,
)
from sqltide.compiler.discovery._helpers.effective_configuration import (
    resolve_effective_project_configuration,
)
from sqltide.compiler.discovery._helpers.source_registry import discover_source_registry
from sqltide.compiler.discovery.models import (
    DiscoveredProjectFile,
    EffectiveProjectConfiguration,
    LoadedProject,
    LoadedProjectConfiguration,
    Project,
)
from sqltide.compiler.macros.main._discover_macro_files import discover_macro_files
from sqltide.compiler.macros.main._load_macro_registry import load_macro_registry
from sqltide.compiler.macros.models import MacroRegistry


def load_project_input_for_path(
    *,
    path: Path,
    selected_target: str | None = None,
    cli_variables: Mapping[str, object] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LoadedProject | None:
    """Load the nearest project config and retain the exact authored contents."""

    project_dir: Path | None = find_project_configuration_dir(path)
    if project_dir is None:
        return None
    configuration: LoadedProjectConfiguration = load_project_configuration(project_dir=project_dir)
    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=configuration,
        selected_target=selected_target,
        cli_variables={} if cli_variables is None else cli_variables,
        environment={} if environment is None else environment,
    )
    macro_files: tuple[DiscoveredProjectFile, ...] = discover_macro_files(project_dir=project_dir)
    macro_registry: MacroRegistry | None = (
        None
        if effective.defaults.sources.kafka.naming_macro is None
        and effective.naming.pipeline_naming_macro is None
        else load_macro_registry(macro_files=macro_files)
    )
    project: Project = Project(
        replay_on_change=effective.defaults.replay_on_change,
        model_ttl=effective.defaults.model_ttl,
        model_retention=effective.defaults.models.retention,
        model_on_row_error=effective.defaults.models.on_row_error,
        default_database=effective.database,
        adapter=effective.adapter,
        bounded_replay_fallback=effective.defaults.bounded_replay_fallback,
        naming=effective.naming,
        audit_defaults=effective.defaults.audits,
        audit_scheduler=effective.audit_scheduler,
        dependencies=effective.dependencies,
    )
    return LoadedProject(
        project=project,
        source_file=configuration.project_source,
        configuration=configuration,
        effective_configuration=effective,
        source_files=discover_source_registry(
            project_dir=project_dir,
            variables=dict(effective.variables),
            environment={} if environment is None else environment,
            default_managed_source_ttl=effective.defaults.managed_source_ttl,
            default_managed_source_retention=effective.defaults.sources.kafka.retention,
            default_kafka_broker_list=effective.defaults.kafka_broker_list,
            default_freshness=effective.defaults.freshness,
            default_kafka_naming_macro=effective.defaults.sources.kafka.naming_macro,
            macro_registry=macro_registry,
        ),
        macro_files=macro_files,
        macro_registry=macro_registry,
    )
