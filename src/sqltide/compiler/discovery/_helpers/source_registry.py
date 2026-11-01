"""Standalone streaming source registry discovery and parsing."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Literal

import yaml
from yaml import YAMLError

import sqltide._native as _native
from sqltide.compiler.discovery.constants import KAFKA_NAMING_MACRO_TOPIC_PARAMETER
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    DiscoveredProjectFile,
    DiscoveredSourceFile,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaRetentionPolicy,
    PostgresRefreshSourceStep,
    SourceFreshnessPolicy,
)
from sqltide.compiler.macros.models import LoadedMacro, MacroRegistry


def discover_source_registry(
    *,
    project_dir: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    default_managed_source_ttl: str | None = None,
    default_managed_source_retention: KafkaRetentionPolicy | Literal[False] | None = None,
    default_kafka_broker_list: str | None = None,
    default_freshness: SourceFreshnessPolicy | None = None,
    default_kafka_naming_macro: str | None = None,
    macro_registry: MacroRegistry | None = None,
) -> tuple[DiscoveredSourceFile, ...]:
    """Load direct sources/*.yml files once in stable order and validate uniqueness."""

    sources_root: Path = project_dir / "sources"
    if not sources_root.is_dir():
        return ()
    source_files: tuple[DiscoveredProjectFile, ...] = tuple(
        DiscoveredProjectFile(
            file_path=file_path,
            relative_path=file_path.relative_to(project_dir),
            contents=file_path.read_text(encoding="utf-8"),
        )
        for file_path in sorted(sources_root.glob("*.yml"))
    )
    parsed: list[
        tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...]
    ] = _native.parse_source_registry(
        [_decoded_source_file(source_file=source_file) for source_file in source_files],
        variables,
        environment,
        default_managed_source_ttl,
        default_managed_source_retention,
        default_kafka_broker_list,
        default_freshness,
        partial(
            _derive_kafka_source_name,
            macro_name=default_kafka_naming_macro,
            macro_registry=macro_registry,
        ),
    )
    return tuple(
        DiscoveredSourceFile(source_file=source_file, sources=sources)
        for source_file, sources in zip(source_files, parsed, strict=True)
    )


def parse_freshness_policy(
    *,
    payload: object,
    label: str,
    file_path: Path,
) -> SourceFreshnessPolicy | None:
    """Parse one optional freshness mapping of duration-literal thresholds."""

    return _native.parse_freshness_policy(payload, label, str(file_path))


def source_registry_by_name(
    source_files: tuple[DiscoveredSourceFile, ...],
) -> dict[str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep]:
    """Index one validated source registry by project-wide source name."""

    sources_by_name: dict[
        str, KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
    ] = {}
    source_file: DiscoveredSourceFile
    for source_file in source_files:
        source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
        for source in source_file.sources:
            sources_by_name[source.name] = source
    return sources_by_name


def _decoded_source_file(*, source_file: DiscoveredProjectFile) -> tuple[str, object, str | None]:
    try:
        payload: object = yaml.safe_load(source_file.contents)
    except YAMLError as error:
        return (str(source_file.file_path), None, str(error))
    return (str(source_file.file_path), payload, None)


def _derive_kafka_source_name(
    *,
    topic: str,
    macro_name: str | None,
    macro_registry: MacroRegistry | None,
    label: str,
    file_path: str,
) -> tuple[str, str, str]:
    if macro_name is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.name must be a non-empty string or "
            "defaults.sources.kafka.naming_macro must be configured"
        )
    loaded_macro: LoadedMacro | None = (
        None if macro_registry is None else macro_registry.macros.get(macro_name)
    )
    if loaded_macro is None:
        available: str = (
            "none" if macro_registry is None else ", ".join(sorted(macro_registry.macros)) or "none"
        )
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.name cannot use unknown Kafka naming macro "
            f"'{macro_name}'. Available macros: {available}"
        )
    parameters: tuple[inspect.Parameter, ...] = tuple(
        inspect.signature(loaded_macro.function).parameters.values()
    )
    if (
        len(parameters) != 1
        or parameters[0].name != KAFKA_NAMING_MACRO_TOPIC_PARAMETER
        or parameters[0].kind
        not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ):
        raise PipelineDiscoveryError(
            f"Kafka source naming macro '{macro_name}' in '{loaded_macro.file_path}' must have "
            "the signature def <name>(topic: str) -> str"
        )
    try:
        result: object = loaded_macro.function(topic)
    except TypeError as error:
        raise PipelineDiscoveryError(
            f"Kafka source naming macro '{macro_name}' in '{loaded_macro.file_path}' must accept "
            f"exactly one topic string: {error}"
        ) from error
    except Exception as error:
        raise PipelineDiscoveryError(
            f"Kafka source naming macro '{macro_name}' in '{loaded_macro.file_path}' failed for "
            f"topic '{topic}': {error}"
        ) from error
    if not isinstance(result, str):
        raise PipelineDiscoveryError(
            f"Kafka source naming macro '{macro_name}' in '{loaded_macro.file_path}' must return "
            "a string"
        )
    return result, loaded_macro.name, loaded_macro.identity_fingerprint
