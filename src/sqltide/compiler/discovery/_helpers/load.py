"""Pipeline loading helpers."""

from __future__ import annotations

import inspect
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import sqltide._native as _native
from sqltide.compiler.discovery._helpers.configuration import (
    find_project_configuration_dir,
    load_project_configuration,
)
from sqltide.compiler.discovery._helpers.effective_configuration import (
    resolve_effective_project_configuration,
)
from sqltide.compiler.discovery._helpers.model_header import parse_model_retention
from sqltide.compiler.discovery._helpers.model_sql import load_transform_from_sql_file
from sqltide.compiler.discovery._helpers.replay_policy_validation import (
    validate_replay_policies_for_mode,
)
from sqltide.compiler.discovery._helpers.source_registry import (
    discover_source_registry,
    source_registry_by_name,
)
from sqltide.compiler.discovery.constants import (
    AUDIT_DEFAULT_EVERY_KEY,
    AUDIT_DEFAULT_KEYS,
    AUDIT_DEFAULT_WARMUP_KEY,
    AUDIT_SEVERITIES,
    EXECUTION_SETTING_NAME_PATTERN,
    FULL_REPLAY_POLICY_VALUE,
    MODEL_DEFAULT_KEYS,
    NAMING_KEYS,
    PIPELINE_CONFIG_FILE_NAME,
    PIPELINE_CONFIG_KEYS,
    PIPELINE_DEFAULT_KEYS,
    PIPELINE_EXECUTION_KEYS,
    PIPELINE_REPLAY_EXECUTION_KEYS,
    PROTECTION_CONFIRMATION_PATTERN,
    PROTECTION_CONFIRMATION_UNSAFE_PATTERN,
    PROTECTION_KEYS,
    SCHEMA_CHANGE_RULE_KEYS,
    SECONDS_BY_DURATION_UNIT,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.main._parse_duration_seconds import parse_duration_seconds
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
    ReplayOnChangeRule,
    TransformStep,
    ViewStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    PipelineMode,
    ReplayOnChangeMode,
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
    mode: PipelineMode = _load_pipeline_mode(
        value=pipeline_values.get("mode"),
        default=default_mode,
        file_path=config_path,
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
    return _PipelineDraft(
        pipeline_dir=pipeline_directory.pipeline_dir,
        config_path=config_path,
        transforms=transforms,
        mode=mode,
        replay_on_change=_load_replay_on_change(
            value=pipeline_values.get("replay_on_change"),
            file_path=config_path,
        ),
        bounded_replay_fallback=_load_bounded_replay_fallback(
            value=pipeline_values.get("bounded_replay_fallback"),
            file_path=config_path,
        ),
        naming=_load_pipeline_naming(
            value=pipeline_values.get("naming"),
            file_path=config_path,
        ),
        protection=_load_pipeline_protection(
            value=pipeline_values.get("protection"),
            pipeline_name=pipeline_directory.pipeline_dir.name,
            file_path=config_path,
            mode=mode,
        ),
        audit_defaults=_load_pipeline_audit_defaults(
            value=pipeline_values.get("audit_defaults"),
            file_path=config_path,
        ),
        execution_settings=_load_pipeline_execution_settings(
            value=pipeline_values.get("execution"), file_path=config_path
        ),
        model_retention=_load_pipeline_model_retention(
            value=pipeline_values.get("defaults"), file_path=config_path
        ),
    )


def _load_pipeline_model_retention(
    *, value: object, file_path: Path
) -> ModelRetentionPolicy | Literal[False] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define defaults as a mapping"
        )
    defaults: dict[str, object] = cast(dict[str, object], value)
    unknown_defaults: tuple[str, ...] = tuple(sorted(set(defaults) - PIPELINE_DEFAULT_KEYS))
    if unknown_defaults:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported defaults keys: "
            f"{', '.join(unknown_defaults)}"
        )
    models_value: object = defaults.get("models")
    if not isinstance(models_value, dict):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define defaults.models as a mapping"
        )
    models: dict[str, object] = cast(dict[str, object], models_value)
    unknown_models: tuple[str, ...] = tuple(sorted(set(models) - MODEL_DEFAULT_KEYS))
    if unknown_models:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported defaults.models keys: "
            f"{', '.join(unknown_models)}"
        )
    try:
        return parse_model_retention(
            value=models.get("retention"),
            field_path=f"Pipeline config '{file_path}' defaults.models.retention",
        )
    except ValueError as error:
        raise PipelineDiscoveryError(str(error)) from error


def _load_pipeline_execution_settings(*, value: object, file_path: Path) -> ExecutionSettings:
    if value is None:
        return ExecutionSettings()
    if not isinstance(value, dict):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define execution as a mapping"
        )
    execution: dict[str, object] = cast(dict[str, object], value)
    unknown_keys: tuple[str, ...] = tuple(sorted(set(execution) - PIPELINE_EXECUTION_KEYS))
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported execution keys: "
            f"{', '.join(unknown_keys)}"
        )
    replay: object = execution.get("replay")
    if replay is None:
        return ExecutionSettings()
    if not isinstance(replay, dict):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define execution.replay as a mapping"
        )
    replay_mapping: dict[str, object] = cast(dict[str, object], replay)
    unknown_replay_keys: tuple[str, ...] = tuple(
        sorted(set(replay_mapping) - PIPELINE_REPLAY_EXECUTION_KEYS)
    )
    if unknown_replay_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported execution.replay keys: "
            f"{', '.join(unknown_replay_keys)}"
        )
    settings: object = replay_mapping.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define execution.replay.settings as a "
            "non-empty mapping"
        )
    invalid_names: tuple[str, ...] = tuple(
        sorted(
            str(name)
            for name in settings
            if not isinstance(name, str) or EXECUTION_SETTING_NAME_PATTERN.fullmatch(name) is None
        )
    )
    if invalid_names:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains invalid execution.replay.settings names: "
            f"{', '.join(invalid_names)}"
        )
    setting_mapping: dict[str, object] = cast(dict[str, object], settings)
    return ExecutionSettings(
        replay={name: str(setting) for name, setting in setting_mapping.items()}
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
    unknown_keys: list[str] = [key for key in pipeline_values if key not in PIPELINE_CONFIG_KEYS]
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{config_file.file_path}' contains unsupported keys: "
            f"{', '.join(sorted(unknown_keys))}"
        )
    return pipeline_values


def _load_pipeline_mode(*, value: object, default: PipelineMode, file_path: Path) -> PipelineMode:
    if value is None:
        return default
    if not isinstance(value, str):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define mode as 'direct' or 'virtual'"
        )
    try:
        return PipelineMode(value)
    except ValueError as error:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' has unsupported mode '{value}'; "
            "expected 'direct' or 'virtual'"
        ) from error


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


def _load_replay_on_change(*, value: object, file_path: Path) -> ReplayOnChangePolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define replay_on_change as a mapping"
        )
    typed_value: dict[str, object] = cast(dict[str, object], value)
    unknown_keys: list[str] = [key for key in typed_value if key not in SCHEMA_CHANGE_RULE_KEYS]
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported replay_on_change keys: "
            f"{', '.join(sorted(unknown_keys))}"
        )
    return ReplayOnChangePolicy(
        breaking=_load_replay_on_change_rule(
            value=typed_value.get("breaking"), key="breaking", file_path=file_path
        ),
        non_breaking=_load_replay_on_change_rule(
            value=typed_value.get("non_breaking"), key="non_breaking", file_path=file_path
        ),
    )


def _load_replay_on_change_rule(
    *, value: object, key: str, file_path: Path
) -> ReplayOnChangeRule | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define replay_on_change.{key} as a string"
        )
    if value == FULL_REPLAY_POLICY_VALUE:
        return ReplayOnChangeRule(mode=ReplayOnChangeMode.FULL)
    bounded_match: re.Match[str] | None = re.fullmatch(r"bounded-(\d+)([dhms])", value)
    if bounded_match is None:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define replay_on_change.{key} as 'full' "
            "or 'bounded-<duration>'"
        )
    return ReplayOnChangeRule(
        mode=ReplayOnChangeMode.BOUNDED,
        lookback_seconds=(
            int(bounded_match.group(1)) * SECONDS_BY_DURATION_UNIT[bounded_match.group(2)]
        ),
    )


def _load_bounded_replay_fallback(
    *, value: object, file_path: Path
) -> BoundedReplayFallback | None:
    if value is None:
        return None
    try:
        return BoundedReplayFallback(value)
    except ValueError as error:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' has unsupported bounded_replay_fallback '{value}'; "
            "expected 'full' or 'bounded_without_history'"
        ) from error


def _load_pipeline_naming(*, value: object, file_path: Path) -> PipelineNaming:
    if value is None:
        return PipelineNaming()
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define naming as a mapping"
        )
    typed_value: dict[str, object] = cast(dict[str, object], value)
    unknown_keys: list[str] = [key for key in typed_value if key not in NAMING_KEYS]
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported naming keys: "
            f"{', '.join(sorted(unknown_keys))}"
        )
    return PipelineNaming(
        table_prefix=_pipeline_prefix(
            values=typed_value,
            key="table_prefix",
            file_path=file_path,
        ),
        view_prefix=_pipeline_prefix(
            values=typed_value,
            key="view_prefix",
            file_path=file_path,
        ),
    )


def _pipeline_prefix(*, values: dict[str, object], key: str, file_path: Path) -> str | None:
    value: object = values.get(key)
    if value is None and key not in values:
        return None
    if not isinstance(value, str):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define naming.{key} as a string"
        )
    return value


def _load_pipeline_protection(
    *, value: object, pipeline_name: str, file_path: Path, mode: PipelineMode
) -> PipelineProtection | None:
    if value is None:
        return None
    if mode != PipelineMode.DIRECT:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' may define protection only for direct pipelines"
        )
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define protection as a mapping"
        )
    typed_value: dict[str, object] = cast(dict[str, object], value)
    unknown_keys: list[str] = [key for key in typed_value if key not in PROTECTION_KEYS]
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported protection keys: "
            f"{', '.join(sorted(unknown_keys))}"
        )
    default_warning: str = (
        f"Pipeline '{pipeline_name}' is protected. Confirm its operational impact before building."
    )
    warning: str = _protection_string(
        values=typed_value,
        key="warning",
        default=default_warning,
        file_path=file_path,
    )
    confirmation: str = _protection_string(
        values=typed_value,
        key="confirmation",
        default=_default_protection_confirmation(pipeline_name=pipeline_name),
        file_path=file_path,
    )
    if PROTECTION_CONFIRMATION_PATTERN.fullmatch(confirmation) is None:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' protection.confirmation must contain only letters, "
            "numbers, '.', '_', ':', or '-' and must start with a letter or number"
        )
    return PipelineProtection(warning=warning, confirmation=confirmation)


def _load_pipeline_audit_defaults(*, value: object, file_path: Path) -> AuditDefaults:
    if value is None:
        return AuditDefaults()
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define audit_defaults as a mapping"
        )
    values: dict[str, object] = cast(dict[str, object], value)
    unknown_keys: list[str] = [key for key in values if key not in AUDIT_DEFAULT_KEYS]
    if unknown_keys:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' contains unsupported audit_defaults keys: "
            f"{', '.join(sorted(unknown_keys))}"
        )
    severity_value: object | None = values.get("severity")
    if severity_value is not None and severity_value not in AUDIT_SEVERITIES:
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' audit_defaults.severity must be 'error' or 'warning'"
        )
    try:
        cadence_seconds: int | None = (
            parse_duration_seconds(
                value=values[AUDIT_DEFAULT_EVERY_KEY],
                field_path=f"Pipeline config '{file_path}' audit_defaults.every",
                allow_zero=False,
            )
            if AUDIT_DEFAULT_EVERY_KEY in values
            else None
        )
        warmup_seconds: int | None = (
            parse_duration_seconds(
                value=values[AUDIT_DEFAULT_WARMUP_KEY],
                field_path=f"Pipeline config '{file_path}' audit_defaults.warmup",
                allow_zero=True,
            )
            if AUDIT_DEFAULT_WARMUP_KEY in values
            else None
        )
    except ValueError as error:
        raise PipelineDiscoveryError(str(error)) from None
    return AuditDefaults(
        severity=str(severity_value) if severity_value is not None else None,
        cadence_seconds=cadence_seconds,
        warmup_seconds=warmup_seconds,
    )


def _default_protection_confirmation(*, pipeline_name: str) -> str:
    if PROTECTION_CONFIRMATION_PATTERN.fullmatch(pipeline_name) is not None:
        return pipeline_name
    sanitized_name: str = PROTECTION_CONFIRMATION_UNSAFE_PATTERN.sub("_", pipeline_name)
    return f"CONFIRM_{sanitized_name}"


def _protection_string(
    *, values: dict[str, object], key: str, default: str, file_path: Path
) -> str:
    value: object = values.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise PipelineDiscoveryError(
            f"Pipeline config '{file_path}' must define protection.{key} as a non-empty string"
        )
    return value


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
