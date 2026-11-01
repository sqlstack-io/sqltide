"""Pipeline loading helpers."""

from __future__ import annotations

import inspect
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import sqltide._native as _native
from sqltide.compiler.discovery._helpers.configuration import (
    find_project_configuration_dir,
    load_project_configuration,
)
from sqltide.compiler.discovery._helpers.effective_configuration import (
    resolve_effective_project_configuration,
)
from sqltide.compiler.discovery._helpers.model_sql import load_transform_from_sql_file
from sqltide.compiler.discovery._helpers.replay_policy_validation import (
    validate_replay_policies_for_mode,
)
from sqltide.compiler.discovery._helpers.source_registry import (
    discover_source_registry,
    source_registry_by_name,
)
from sqltide.compiler.discovery.constants import (
    PIPELINE_CONFIG_FILE_NAME,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    AuditDefaults,
    DiscoveredPipelineDirectory,
    DiscoveredProjectFile,
    EffectiveProjectConfiguration,
    ExecutionSettings,
    ExternalTableSourceStep,
    KafkaLandingStep,
    LoadedPipeline,
    ModelRetentionPolicy,
    Pipeline,
    PipelineNaming,
    PipelineProtection,
    PostgresRefreshSourceStep,
    Project,
    ProjectNaming,
    ReplayOnChangePolicy,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    PipelineMode,
)
from sqltide.compiler.macros.constants import (
    MACRO_CONTEXT_PARAMETER_NAME,
    PIPELINE_NAMING_CONTEXT_ANNOTATION_NAME,
)
from sqltide.compiler.macros.main._discover_macro_files import discover_macro_files
from sqltide.compiler.macros.main._load_macro_registry import load_macro_registry
from sqltide.compiler.macros.models import (
    LoadedMacro,
    MacroContext,
    MacroRegistry,
    PipelineNamingContext,
)


@dataclass(frozen=True)
class _PipelineDraft:
    pipeline_dir: Path
    config_path: Path
    transforms: tuple[TransformStep | ViewStep, ...]
    mode: PipelineMode
    replay_on_change: ReplayOnChangePolicy | None
    bounded_replay_fallback: BoundedReplayFallback | None
    naming: PipelineNaming
    protection: PipelineProtection | None
    audit_defaults: AuditDefaults
    execution_settings: ExecutionSettings
    model_retention: ModelRetentionPolicy | Literal[False] | None


def load_pipeline_directory(pipeline_dir: Path) -> LoadedPipeline:
    """Load one pipeline directory without the project compiler's macro expansion phase."""

    project_dir: Path | None = find_project_configuration_dir(pipeline_dir)
    if project_dir is None:
        raise PipelineDiscoveryError(
            f"Pipeline directory '{pipeline_dir}' is not inside a sqltide_project.toml project"
        )
    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=load_project_configuration(project_dir=project_dir),
        selected_target=None,
        cli_variables={},
        environment={},
    )
    macro_registry: MacroRegistry = (
        MacroRegistry()
        if effective.defaults.sources.kafka.naming_macro is None
        and effective.naming.pipeline_naming_macro is None
        else load_macro_registry(macro_files=discover_macro_files(project_dir=project_dir))
    )
    sources_by_name: dict[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ] = source_registry_by_name(
        discover_source_registry(
            project_dir=project_dir,
            variables=dict(effective.variables),
            environment={},
            default_managed_source_ttl=effective.defaults.managed_source_ttl,
            default_managed_source_retention=effective.defaults.sources.kafka.retention,
            default_kafka_broker_list=effective.defaults.kafka_broker_list,
            default_freshness=effective.defaults.freshness,
            default_kafka_naming_macro=effective.defaults.sources.kafka.naming_macro,
            macro_registry=macro_registry,
        )
    )
    config_path: Path = pipeline_dir / PIPELINE_CONFIG_FILE_NAME
    config_file: DiscoveredProjectFile | None = (
        DiscoveredProjectFile(
            file_path=config_path,
            relative_path=config_path.relative_to(project_dir),
            contents=config_path.read_text(encoding="utf-8"),
        )
        if config_path.is_file()
        else None
    )
    pipeline: Pipeline = load_pipeline_directories(
        pipeline_directories=(
            DiscoveredPipelineDirectory(
                pipeline_dir=pipeline_dir,
                config_file=config_file,
            ),
        ),
        sources_by_name=sources_by_name,
        macro_registry=macro_registry,
        project_naming=effective.naming,
        default_mode=PipelineMode(effective.defaults.pipeline_mode),
    )[0]
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=pipeline,
        file_path=pipeline_dir,
        project=Project(
            replay_on_change=effective.defaults.replay_on_change,
            bounded_replay_fallback=effective.defaults.bounded_replay_fallback,
            model_ttl=effective.defaults.model_ttl,
            model_retention=effective.defaults.models.retention,
            default_database=effective.database,
            adapter=effective.adapter,
            naming=effective.naming,
            audit_defaults=effective.defaults.audits,
            audit_scheduler=effective.audit_scheduler,
            dependencies=effective.dependencies,
        ),
    )
    validate_replay_policies_for_mode(
        default_pipeline_mode=PipelineMode(effective.defaults.pipeline_mode),
        project=loaded_pipeline.project,
        project_file_path=project_dir / "sqltide_project.toml",
        loaded_pipelines=(loaded_pipeline,),
    )
    return loaded_pipeline


def load_pipeline_directories(
    *,
    pipeline_directories: tuple[DiscoveredPipelineDirectory, ...],
    model_contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
    sources_by_name: Mapping[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ],
    project_naming: ProjectNaming | None = None,
    default_mode: PipelineMode = PipelineMode.DIRECT,
) -> tuple[Pipeline, ...]:
    """Load pipeline directories and infer each source from driving-input chains."""

    drafts: tuple[_PipelineDraft, ...] = tuple(
        _load_pipeline_draft(
            pipeline_directory=pipeline_directory,
            model_contents_by_path=model_contents_by_path,
            macro_registry=macro_registry,
            macro_context=macro_context,
            default_mode=default_mode,
        )
        for pipeline_directory in pipeline_directories
    )
    transforms_by_name: dict[str, TransformStep] = _transforms_by_name(drafts=drafts)
    source_names: list[str | None]
    source_error: str | None
    source_names, source_error = _native.infer_pipeline_sources(
        [_native_pipeline_transforms(draft=draft) for draft in drafts],
        {name: transform.source for name, transform in transforms_by_name.items()},
        set(sources_by_name),
    )
    pipelines: list[Pipeline] = []
    index: int
    draft: _PipelineDraft
    for index, draft in enumerate(drafts):
        if index == len(source_names) and source_error is not None:
            raise PipelineDiscoveryError(source_error)
        source_name: str | None = source_names[index]
        pipeline_source: (
            KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep | None
        ) = None if source_name is None else sources_by_name[source_name]
        if project_naming is not None:
            _validate_pipeline_name(
                draft=draft,
                source=pipeline_source,
                project_naming=project_naming,
                macro_registry=macro_registry,
            )
        pipelines.append(
            Pipeline(
                name=draft.pipeline_dir.name,
                source=pipeline_source,
                transforms=draft.transforms,
                mode=draft.mode,
                replay_on_change=draft.replay_on_change,
                bounded_replay_fallback=draft.bounded_replay_fallback,
                naming=draft.naming,
                protection=draft.protection,
                audit_defaults=draft.audit_defaults,
                execution_settings=draft.execution_settings,
                model_retention=draft.model_retention,
            )
        )
    return tuple(pipelines)


def _validate_pipeline_name(
    *,
    draft: _PipelineDraft,
    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep | None,
    project_naming: ProjectNaming,
    macro_registry: MacroRegistry | None,
) -> None:
    name: str = draft.pipeline_dir.name
    prefix: str = project_naming.pipeline_prefix
    if not name.startswith(prefix):
        raise PipelineDiscoveryError(
            f"Pipeline directory '{draft.pipeline_dir}' must start with configured "
            f"naming.pipeline_prefix '{prefix}'"
        )
    macro_name: str | None = project_naming.pipeline_naming_macro
    if macro_name is None:
        return
    loaded_macro: LoadedMacro | None = (
        None if macro_registry is None else macro_registry.macros.get(macro_name)
    )
    if loaded_macro is None:
        available: str = (
            "none" if macro_registry is None else ", ".join(sorted(macro_registry.macros)) or "none"
        )
        raise PipelineDiscoveryError(
            f"Pipeline naming macro '{macro_name}' is not defined. Available macros: {available}"
        )
    parameters: tuple[inspect.Parameter, ...] = tuple(
        inspect.signature(loaded_macro.function).parameters.values()
    )
    if (
        len(parameters) != 1
        or parameters[0].name != MACRO_CONTEXT_PARAMETER_NAME
        or parameters[0].kind
        not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        or parameters[0].annotation
        not in {PipelineNamingContext, PIPELINE_NAMING_CONTEXT_ANNOTATION_NAME}
    ):
        raise PipelineDiscoveryError(
            f"Pipeline naming macro '{macro_name}' in '{loaded_macro.file_path}' must have the "
            "signature def <name>(ctx: PipelineNamingContext) -> str"
        )
    context: PipelineNamingContext = PipelineNamingContext(
        name=name,
        source_name=None if source is None else source.name,
        model_names=tuple(sorted(transform.name for transform in draft.transforms)),
        relative_directory=f"pipelines/{name}",
        mode=draft.mode,
    )
    try:
        expected_name: object = loaded_macro.function(context)
    except TypeError as error:
        raise PipelineDiscoveryError(
            f"Pipeline naming macro '{macro_name}' in '{loaded_macro.file_path}' must accept "
            f"exactly one PipelineNamingContext: {error}"
        ) from error
    except Exception as error:
        raise PipelineDiscoveryError(
            f"Pipeline naming macro '{macro_name}' in '{loaded_macro.file_path}' failed for "
            f"pipeline '{name}': {error}"
        ) from error
    if not isinstance(expected_name, str) or not expected_name:
        raise PipelineDiscoveryError(
            f"Pipeline naming macro '{macro_name}' in '{loaded_macro.file_path}' must return a "
            "non-empty string"
        )
    if expected_name != name:
        raise PipelineDiscoveryError(
            f"Pipeline directory '{draft.pipeline_dir}' does not satisfy naming macro "
            f"'{macro_name}'; expected 'pipelines/{expected_name}'"
        )


def _load_pipeline_draft(
    *,
    pipeline_directory: DiscoveredPipelineDirectory,
    model_contents_by_path: Mapping[Path, str] | None,
    macro_registry: MacroRegistry | None,
    macro_context: MacroContext | None,
    default_mode: PipelineMode,
) -> _PipelineDraft:
    pipeline_values: dict[str, object] = _load_pipeline_config(
        pipeline_directory=pipeline_directory
    )
    config_path: Path = (
        pipeline_directory.pipeline_dir / PIPELINE_CONFIG_FILE_NAME
        if pipeline_directory.config_file is None
        else pipeline_directory.config_file.file_path
    )
    mode: PipelineMode = PipelineMode(
        _native.parse_pipeline_mode(pipeline_values, str(config_path), default_mode)
    )
    effective_macro_context: MacroContext | None = (
        macro_context
        if macro_context is None
        or macro_context.virtual_environments == (mode == PipelineMode.VIRTUAL)
        else replace(
            macro_context,
            virtual_environments=mode == PipelineMode.VIRTUAL,
        )
    )
    transforms: tuple[TransformStep | ViewStep, ...] = _load_pipeline_transforms(
        pipeline_root=pipeline_directory.pipeline_dir,
        model_contents_by_path=model_contents_by_path,
        macro_registry=macro_registry,
        macro_context=effective_macro_context,
    )
    settings: tuple[
        ReplayOnChangePolicy | None,
        BoundedReplayFallback | None,
        PipelineNaming,
        PipelineProtection | None,
        AuditDefaults,
        ExecutionSettings,
        ModelRetentionPolicy | Literal[False] | None,
    ] = _native.parse_pipeline_settings(
        pipeline_values,
        str(config_path),
        pipeline_directory.pipeline_dir.name,
        mode,
    )
    return _PipelineDraft(
        pipeline_dir=pipeline_directory.pipeline_dir,
        config_path=config_path,
        transforms=transforms,
        mode=mode,
        replay_on_change=settings[0],
        bounded_replay_fallback=settings[1],
        naming=settings[2],
        protection=settings[3],
        audit_defaults=settings[4],
        execution_settings=settings[5],
        model_retention=settings[6],
    )


def _load_pipeline_config(*, pipeline_directory: DiscoveredPipelineDirectory) -> dict[str, object]:
    config_file: DiscoveredProjectFile | None = pipeline_directory.config_file
    if config_file is None:
        return {}
    try:
        pipeline_values: dict[str, object] = tomllib.loads(config_file.contents)
    except tomllib.TOMLDecodeError as error:
        raise PipelineDiscoveryError(
            f"Pipeline config '{config_file.file_path}' contains invalid TOML: {error}"
        ) from error
    return pipeline_values


def _native_pipeline_transforms(*, draft: _PipelineDraft) -> tuple[str, list[tuple[str, str]]]:
    return (
        str(draft.pipeline_dir),
        [
            (model.name, model.source)
            for model in draft.transforms
            if isinstance(model, TransformStep)
        ],
    )


def _transforms_by_name(*, drafts: tuple[_PipelineDraft, ...]) -> dict[str, TransformStep]:
    transforms_by_name: dict[str, TransformStep] = {}
    transform_paths_by_name: dict[str, Path] = {}
    draft: _PipelineDraft
    for draft in drafts:
        model: TransformStep | ViewStep
        for model in draft.transforms:
            existing_path: Path | None = transform_paths_by_name.get(model.name)
            if existing_path is not None:
                raise PipelineDiscoveryError(
                    f"Logical node name '{model.name}' is defined in both "
                    f"'{existing_path}' and '{model.source_file_path}'"
                )
            if isinstance(model, TransformStep):
                transforms_by_name[model.name] = model
            transform_paths_by_name[model.name] = model.source_file_path or draft.pipeline_dir
    return transforms_by_name


def _load_pipeline_transforms(
    *,
    pipeline_root: Path,
    model_contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> tuple[TransformStep | ViewStep, ...]:
    model_file_paths: list[Path] = sorted(pipeline_root.rglob("*.sql"))
    if not model_file_paths:
        raise PipelineDiscoveryError(
            f"Pipeline root '{pipeline_root}' must contain at least one SQL model file"
        )
    transforms: list[TransformStep | ViewStep] = []
    model_paths_by_name: dict[str, Path] = {}
    model_file_path: Path
    for model_file_path in model_file_paths:
        existing_path: Path | None = model_paths_by_name.get(model_file_path.stem)
        if existing_path is not None:
            raise PipelineDiscoveryError(
                f"Pipeline root '{pipeline_root}' defines duplicate model filename stem "
                f"'{model_file_path.stem}' in both '{existing_path}' and '{model_file_path}'"
            )
        model_paths_by_name[model_file_path.stem] = model_file_path
        transforms.append(
            load_transform_from_sql_file(
                file_path=model_file_path,
                contents=(
                    None
                    if model_contents_by_path is None
                    else model_contents_by_path[model_file_path]
                ),
                macro_registry=macro_registry,
                macro_context=macro_context,
            )
        )
    return tuple(transforms)
