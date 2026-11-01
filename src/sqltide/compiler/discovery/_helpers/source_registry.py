"""Standalone streaming source registry discovery and parsing."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from yaml import YAMLError

import sqltide._native as _native
from sqltide.compiler.discovery._helpers.interpolation import interpolate_config_value
from sqltide.compiler.discovery._helpers.model_header import parse_kafka_retention
from sqltide.compiler.discovery.constants import (
    BOOLEAN_FALSE_LITERALS,
    BOOLEAN_TRUE_LITERALS,
    DEFAULT_POSTGRES_PORT,
    INTERPOLATION_TOKEN_START,
    KAFKA_NAMING_MACRO_TOPIC_PARAMETER,
    POSTGRES_FORBIDDEN_SOURCE_KEYS,
    POSTGRES_SOURCE_REQUIRED_KEYS,
    REFRESH_INTERVAL_PATTERN,
    REPLAY_BOUNDARY_COLUMN_KEYS,
    REPLAY_BOUNDARY_KEYS,
    SOURCE_FILE_KEYS,
    SOURCE_KEYS,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    DiscoveredProjectFile,
    DiscoveredSourceFile,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaRetentionPolicy,
    KafkaSettings,
    PostgresRefreshSourceStep,
    ReplayBoundary,
    ReplayBoundaryColumns,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import (
    KafkaRetentionOrigin,
    ReplayBoundaryMode,
    SourceKind,
    SourceNameOrigin,
)
from sqltide.compiler.macros.models import LoadedMacro, MacroRegistry

_SOURCE_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class _ResolvedKafkaSourceName:
    name: str
    topic: str
    origin: SourceNameOrigin
    macro_name: str | None = None
    macro_fingerprint: str | None = None


@dataclass(frozen=True)
class _ManagedSourceRetentionDefaults:
    ttl: str | None
    retention: KafkaRetentionPolicy | Literal[False] | None


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
    retention_defaults: _ManagedSourceRetentionDefaults = _ManagedSourceRetentionDefaults(
        ttl=default_managed_source_ttl,
        retention=default_managed_source_retention,
    )
    discovered_files: tuple[DiscoveredSourceFile, ...] = tuple(
        _load_source_file(
            file_path=file_path,
            project_dir=project_dir,
            variables=variables,
            environment=environment,
            retention_defaults=retention_defaults,
            default_kafka_broker_list=default_kafka_broker_list,
            default_freshness=default_freshness,
            default_kafka_naming_macro=default_kafka_naming_macro,
            macro_registry=macro_registry,
        )
        for file_path in sorted(sources_root.glob("*.yml"))
    )
    _validate_unique_source_names(discovered_files)
    _validate_consistent_adopted_table_mappings(discovered_files)
    return discovered_files


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


def _validate_consistent_adopted_table_mappings(
    source_files: tuple[DiscoveredSourceFile, ...],
) -> None:
    boundary_by_table_name: dict[str, ReplayBoundary] = {}
    source_file: DiscoveredSourceFile
    for source_file in source_files:
        source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
        for source in source_file.sources:
            if not isinstance(source, ExternalTableSourceStep):
                continue
            existing_boundary: ReplayBoundary | None = boundary_by_table_name.get(source.table_name)
            if existing_boundary is not None and existing_boundary != source.replay_boundary:
                raise PipelineDiscoveryError(
                    f"Adopted source table '{source.table_name}' has conflicting replay mappings"
                )
            boundary_by_table_name[source.table_name] = source.replay_boundary


def _load_source_file(
    *,
    file_path: Path,
    project_dir: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    retention_defaults: _ManagedSourceRetentionDefaults,
    default_kafka_broker_list: str | None,
    default_freshness: SourceFreshnessPolicy | None,
    default_kafka_naming_macro: str | None,
    macro_registry: MacroRegistry | None,
) -> DiscoveredSourceFile:
    contents: str = file_path.read_text(encoding="utf-8")
    source_file: DiscoveredProjectFile = DiscoveredProjectFile(
        file_path=file_path,
        relative_path=file_path.relative_to(project_dir),
        contents=contents,
    )
    return DiscoveredSourceFile(
        source_file=source_file,
        sources=_parse_source_file(
            source_file=source_file,
            variables=variables,
            environment=environment,
            retention_defaults=retention_defaults,
            default_kafka_broker_list=default_kafka_broker_list,
            default_freshness=default_freshness,
            default_kafka_naming_macro=default_kafka_naming_macro,
            macro_registry=macro_registry,
        ),
    )


def _parse_source_file(
    *,
    source_file: DiscoveredProjectFile,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    retention_defaults: _ManagedSourceRetentionDefaults,
    default_kafka_broker_list: str | None,
    default_freshness: SourceFreshnessPolicy | None,
    default_kafka_naming_macro: str | None,
    macro_registry: MacroRegistry | None,
) -> tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...]:
    try:
        raw_payload: object = yaml.safe_load(source_file.contents)
    except YAMLError as error:
        raise PipelineDiscoveryError(
            f"Source file '{source_file.file_path}' contains invalid YAML: {error}"
        ) from error
    payload: dict[str, object] = _mapping(
        value=raw_payload,
        field_path="source file",
        file_path=source_file.file_path,
    )
    _validate_keys(
        mapping=payload,
        allowed=SOURCE_FILE_KEYS,
        field_path="source file",
        file_path=source_file.file_path,
    )
    raw_sources: object = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise PipelineDiscoveryError(
            f"Source file '{source_file.file_path}' must define sources as a list"
        )
    return tuple(
        _parse_source(
            value=raw_source,
            index=index,
            file_path=source_file.file_path,
            variables=variables,
            environment=environment,
            retention_defaults=retention_defaults,
            default_kafka_broker_list=default_kafka_broker_list,
            default_freshness=default_freshness,
            default_kafka_naming_macro=default_kafka_naming_macro,
            macro_registry=macro_registry,
        )
        for index, raw_source in enumerate(raw_sources)
    )


def _parse_source(
    *,
    value: object,
    index: int,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    retention_defaults: _ManagedSourceRetentionDefaults,
    default_kafka_broker_list: str | None,
    default_freshness: SourceFreshnessPolicy | None,
    default_kafka_naming_macro: str | None,
    macro_registry: MacroRegistry | None,
) -> KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep:
    label: str = f"sources[{index}]"
    mapping: dict[str, object] = _mapping(
        value=value,
        field_path=label,
        file_path=file_path,
    )
    _validate_keys(mapping=mapping, allowed=SOURCE_KEYS, field_path=label, file_path=file_path)
    raw_kind: str = _required_string(
        mapping=mapping,
        key="kind",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    try:
        kind: SourceKind = SourceKind(raw_kind)
    except ValueError as error:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.kind must be one of "
            f"{', '.join(repr(member.value) for member in SourceKind)}"
        ) from error
    freshness: SourceFreshnessPolicy | None = (
        parse_freshness_policy(
            payload=mapping.get("freshness"),
            label=f"{label}.freshness",
            file_path=file_path,
        )
        or default_freshness
    )
    if kind == SourceKind.KAFKA:
        topic: str = _required_string(
            mapping=mapping,
            key="topic",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        )
        explicit_name: str | None = _optional_string(
            mapping=mapping,
            key="name",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        )
        name: str
        name_origin: SourceNameOrigin = SourceNameOrigin.EXPLICIT
        naming_macro: str | None = None
        naming_macro_fingerprint: str | None = None
        if explicit_name is None:
            name, loaded_naming_macro = _derive_kafka_source_name(
                topic=topic,
                macro_name=default_kafka_naming_macro,
                macro_registry=macro_registry,
                label=label,
                file_path=file_path,
            )
            name_origin = SourceNameOrigin.DERIVED
            naming_macro = loaded_naming_macro.name
            naming_macro_fingerprint = loaded_naming_macro.identity_fingerprint
        else:
            name = explicit_name
        _validate_source_name(name=name, label=label, file_path=file_path)
        return _parse_managed_kafka_source(
            mapping=mapping,
            source_name=_ResolvedKafkaSourceName(
                name=name,
                topic=topic,
                origin=name_origin,
                macro_name=naming_macro,
                macro_fingerprint=naming_macro_fingerprint,
            ),
            label=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
            retention_defaults=retention_defaults,
            default_kafka_broker_list=default_kafka_broker_list,
            freshness=freshness,
        )
    name = _required_string(
        mapping=mapping,
        key="name",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    _validate_source_name(name=name, label=label, file_path=file_path)
    if kind == SourceKind.POSTGRES:
        return _parse_postgres_source(
            mapping=mapping,
            name=name,
            label=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
            freshness=freshness,
        )
    return _parse_adopted_source(
        mapping=mapping,
        name=name,
        label=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
        freshness=freshness,
    )


def _parse_managed_kafka_source(
    *,
    mapping: dict[str, object],
    source_name: _ResolvedKafkaSourceName,
    label: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    retention_defaults: _ManagedSourceRetentionDefaults,
    default_kafka_broker_list: str | None,
    freshness: SourceFreshnessPolicy | None,
) -> KafkaLandingStep:
    if mapping.get("table_name") is not None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} must not mix Kafka and adopted table fields"
        )
    replay_boundary: ReplayBoundary = _parse_replay_boundary(
        value=mapping.get("replay_boundary"),
        label=f"{label}.replay_boundary",
        file_path=file_path,
        allowed_modes=frozenset(
            {
                ReplayBoundaryMode.OFFSETS,
                ReplayBoundaryMode.TIMESTAMP,
                ReplayBoundaryMode.LANDED_AT,
            }
        ),
        require_columns=False,
        variables=variables,
        environment=environment,
    )
    settings_mapping: dict[str, object] = _mapping(
        value=mapping.get("settings"),
        field_path=f"{label}.settings",
        file_path=file_path,
        optional=True,
    )
    settings: dict[str, str] = {
        key: _interpolated_string(
            value=value,
            field_path=f"{label}.settings.{key}",
            file_path=file_path,
            variables=variables,
            environment=environment,
        )
        for key, value in settings_mapping.items()
    }
    broker_list: str | None = _optional_string(
        mapping=mapping,
        key="broker_list",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    if broker_list is None:
        broker_list = default_kafka_broker_list
    if broker_list is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.broker_list must be a non-empty string "
            "or defaults.kafka_broker_list must be configured"
        )
    explicit_ttl: str | None = _optional_string(
        mapping=mapping,
        key="ttl",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    try:
        explicit_retention: KafkaRetentionPolicy | Literal[False] | None = parse_kafka_retention(
            value=interpolate_config_value(
                value=mapping.get("retention"),
                variables=variables,
                environment=environment,
                field_path=f"Source file '{file_path}' {label}.retention",
            ),
            field_path=f"Source file '{file_path}' {label}.retention",
        )
    except ValueError as error:
        raise PipelineDiscoveryError(str(error)) from error
    if explicit_ttl is not None and explicit_retention is not None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} cannot combine ttl with typed retention"
        )
    return KafkaLandingStep(
        name=source_name.name,
        kafka=KafkaSettings(
            broker_list=broker_list,
            topic=source_name.topic,
            consumer_group=_optional_string(
                mapping=mapping,
                key="consumer_group",
                field_path=label,
                file_path=file_path,
                variables=variables,
                environment=environment,
            ),
            format=_optional_string(
                mapping=mapping,
                key="format",
                field_path=label,
                file_path=file_path,
                variables=variables,
                environment=environment,
            )
            or "JSONAsString",
            ttl=(
                explicit_ttl
                if explicit_ttl is not None
                else (retention_defaults.ttl if explicit_retention is None else None)
            ),
            retention=(
                explicit_retention
                if explicit_retention is not None
                else (retention_defaults.retention if explicit_ttl is None else None)
            ),
            retention_origin=(
                KafkaRetentionOrigin.SOURCE
                if explicit_retention is not None
                else (
                    KafkaRetentionOrigin.PROJECT
                    if explicit_ttl is None and retention_defaults.retention is not None
                    else None
                )
            ),
            settings=settings or None,
        ),
        replay_boundary=replay_boundary,
        freshness=freshness,
        name_origin=source_name.origin,
        naming_macro=source_name.macro_name,
        naming_macro_fingerprint=source_name.macro_fingerprint,
    )


def _parse_postgres_source(
    *,
    mapping: dict[str, object],
    name: str,
    label: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    freshness: SourceFreshnessPolicy | None,
) -> PostgresRefreshSourceStep:
    """Parse a scheduled Postgres refresh source, which carries no replay lineage."""

    present_forbidden: tuple[str, ...] = tuple(
        key for key in POSTGRES_FORBIDDEN_SOURCE_KEYS if key in mapping
    )
    if present_forbidden:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} is a Postgres source and must not declare "
            f"streaming fields: {', '.join(present_forbidden)}"
        )
    required: dict[str, str] = {
        key: _required_string(
            mapping=mapping,
            key=key,
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        )
        for key in POSTGRES_SOURCE_REQUIRED_KEYS
    }
    refresh: str = _required_string(
        mapping=mapping,
        key="refresh",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    if REFRESH_INTERVAL_PATTERN.match(refresh) is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.refresh must be an interval such as "
            f"'5 MINUTE' or '1 HOUR', not '{refresh}'"
        )
    return PostgresRefreshSourceStep(
        name=name,
        kind=SourceKind.POSTGRES,
        host=required["host"],
        database=required["database"],
        table=required["table"],
        user=required["user"],
        refresh=refresh,
        port=_postgres_port(mapping=mapping, label=label, file_path=file_path),
        password_env=_optional_string(
            mapping=mapping,
            key="password_env",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        append=_postgres_append(mapping=mapping, label=label, file_path=file_path),
        freshness=freshness,
    )


def _postgres_port(*, mapping: dict[str, object], label: str, file_path: Path) -> int:
    raw_port: object | None = mapping.get("port")
    if raw_port is None:
        return DEFAULT_POSTGRES_PORT
    if isinstance(raw_port, bool):
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.port must be a whole number, not '{raw_port}'"
        )
    if isinstance(raw_port, int):
        return raw_port
    if isinstance(raw_port, str) and raw_port.strip().isdigit():
        return int(raw_port.strip())
    raise PipelineDiscoveryError(
        f"Source file '{file_path}' {label}.port must be a whole number, not '{raw_port}'"
    )


def _postgres_append(*, mapping: dict[str, object], label: str, file_path: Path) -> bool:
    raw_append: object | None = mapping.get("append")
    if raw_append is None:
        return True
    if isinstance(raw_append, bool):
        return raw_append
    if isinstance(raw_append, str):
        normalized: str = raw_append.strip().lower()
        if normalized in BOOLEAN_TRUE_LITERALS:
            return True
        if normalized in BOOLEAN_FALSE_LITERALS:
            return False
    raise PipelineDiscoveryError(
        f"Source file '{file_path}' {label}.append must be a boolean, not '{raw_append}'"
    )


def _parse_adopted_source(
    *,
    mapping: dict[str, object],
    name: str,
    label: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    freshness: SourceFreshnessPolicy | None,
) -> ExternalTableSourceStep:
    managed_fields: tuple[str, ...] = tuple(
        field
        for field in (
            "broker_list",
            "topic",
            "consumer_group",
            "format",
            "ttl",
            "retention",
            "settings",
        )
        if mapping.get(field) is not None
    )
    if managed_fields:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} must not mix adopted and Kafka fields: "
            f"{', '.join(managed_fields)}"
        )
    replay_boundary: ReplayBoundary = _parse_replay_boundary(
        value=mapping.get("replay_boundary"),
        label=f"{label}.replay_boundary",
        file_path=file_path,
        allowed_modes=frozenset(
            {ReplayBoundaryMode.OFFSETS, ReplayBoundaryMode.TIMESTAMP, ReplayBoundaryMode.CURSOR}
        ),
        require_columns=True,
        variables=variables,
        environment=environment,
    )
    return ExternalTableSourceStep(
        name=name,
        kind=SourceKind.STREAM_TABLE,
        table_name=_required_string(
            mapping=mapping,
            key="table_name",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        replay_boundary=replay_boundary,
        freshness=freshness,
    )


def _parse_replay_boundary(
    *,
    value: object,
    label: str,
    file_path: Path,
    allowed_modes: frozenset[ReplayBoundaryMode],
    require_columns: bool,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> ReplayBoundary:
    mapping: dict[str, object] = _mapping(
        value=value,
        field_path=label,
        file_path=file_path,
    )
    _validate_keys(
        mapping=mapping,
        allowed=REPLAY_BOUNDARY_KEYS,
        field_path=label,
        file_path=file_path,
    )
    raw_mode: str = _required_string(
        mapping=mapping,
        key="mode",
        field_path=label,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    try:
        mode: ReplayBoundaryMode = ReplayBoundaryMode(raw_mode)
    except ValueError as error:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.mode is unsupported"
        ) from error
    if mode not in allowed_modes:
        supported: str = ", ".join(sorted(item.value for item in allowed_modes))
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.mode '{mode}' must be one of: {supported}"
        )
    raw_columns: object = mapping.get("columns")
    if not require_columns and raw_columns is not None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.columns is only valid for adopted sources"
        )
    columns: ReplayBoundaryColumns = _parse_boundary_columns(
        value=raw_columns,
        label=f"{label}.columns",
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    if require_columns:
        _validate_adopted_boundary_columns(
            mode=mode,
            columns=columns,
            label=label,
            file_path=file_path,
        )
    return ReplayBoundary(mode=mode, columns=columns)


def _parse_boundary_columns(
    *,
    value: object,
    label: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> ReplayBoundaryColumns:
    mapping: dict[str, object] = _mapping(
        value=value,
        field_path=label,
        file_path=file_path,
        optional=True,
    )
    _validate_keys(
        mapping=mapping,
        allowed=REPLAY_BOUNDARY_COLUMN_KEYS,
        field_path=label,
        file_path=file_path,
    )
    return ReplayBoundaryColumns(
        partition=_optional_string(
            mapping=mapping,
            key="_replay_partition",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        offset=_optional_string(
            mapping=mapping,
            key="_replay_offset",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        timestamp=_optional_string(
            mapping=mapping,
            key="_replay_timestamp",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        landed_at=_optional_string(
            mapping=mapping,
            key="_replay_landed_at",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
        cursor=_optional_string(
            mapping=mapping,
            key="_replay_cursor",
            field_path=label,
            file_path=file_path,
            variables=variables,
            environment=environment,
        ),
    )


def _validate_adopted_boundary_columns(
    *, mode: ReplayBoundaryMode, columns: ReplayBoundaryColumns, label: str, file_path: Path
) -> None:
    if mode == ReplayBoundaryMode.OFFSETS:
        if columns.partition is None or columns.offset is None or columns.timestamp is None:
            raise PipelineDiscoveryError(
                f"Source file '{file_path}' {label} offsets mode requires partition, offset, "
                "and timestamp role columns"
            )
        if columns.cursor is not None or columns.landed_at is not None:
            raise PipelineDiscoveryError(
                f"Source file '{file_path}' {label} offsets mode has incompatible role columns"
            )
        return
    if mode == ReplayBoundaryMode.TIMESTAMP:
        if columns.timestamp is None:
            raise PipelineDiscoveryError(
                f"Source file '{file_path}' {label} timestamp mode requires a timestamp role column"
            )
        if any(
            value is not None
            for value in (columns.partition, columns.offset, columns.cursor, columns.landed_at)
        ):
            raise PipelineDiscoveryError(
                f"Source file '{file_path}' {label} timestamp mode has incompatible role columns"
            )
        return
    if columns.cursor is None or columns.timestamp is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} cursor mode requires cursor and timestamp "
            "role columns"
        )
    if any(value is not None for value in (columns.partition, columns.offset, columns.landed_at)):
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label} cursor mode has incompatible role columns"
        )


def _derive_kafka_source_name(
    *,
    topic: str,
    macro_name: str | None,
    macro_registry: MacroRegistry | None,
    label: str,
    file_path: Path,
) -> tuple[str, LoadedMacro]:
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
    return result, loaded_macro


def _validate_source_name(*, name: str, label: str, file_path: Path) -> None:
    if _SOURCE_NAME_PATTERN.fullmatch(name) is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {label}.name must resolve to an unqualified identifier "
            "using only letters, numbers, and underscores"
        )


def _validate_unique_source_names(files: tuple[DiscoveredSourceFile, ...]) -> None:
    paths_by_name: dict[str, Path] = {}
    source_file: DiscoveredSourceFile
    for source_file in files:
        source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep
        for source in source_file.sources:
            existing_path: Path | None = paths_by_name.get(source.name)
            if existing_path is not None:
                raise PipelineDiscoveryError(
                    f"Duplicate source name '{source.name}' found in '{existing_path}' and "
                    f"'{source_file.source_file.file_path}'"
                )
            paths_by_name[source.name] = source_file.source_file.file_path


def _mapping(
    *, value: object, field_path: str, file_path: Path, optional: bool = False
) -> dict[str, object]:
    if value is None and optional:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineDiscoveryError(f"Source file '{file_path}' {field_path} must be a mapping")
    mapping: dict[str, object] = cast(dict[str, object], value)
    interpolated_keys: tuple[str, ...] = tuple(
        sorted(key for key in mapping if INTERPOLATION_TOKEN_START in key)
    )
    if interpolated_keys:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {field_path} must not interpolate mapping keys: "
            f"{', '.join(interpolated_keys)}"
        )
    return mapping


def _required_string(
    *,
    mapping: dict[str, object],
    key: str,
    field_path: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> str:
    value: str | None = _optional_string(
        mapping=mapping,
        key=key,
        field_path=field_path,
        file_path=file_path,
        variables=variables,
        environment=environment,
    )
    if value is None:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {field_path}.{key} must be a non-empty string"
        )
    return value


def _optional_string(
    *,
    mapping: dict[str, object],
    key: str,
    field_path: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> str | None:
    value: object | None = mapping.get(key)
    if value is None:
        return None
    return _interpolated_string(
        value=value,
        field_path=f"{field_path}.{key}",
        file_path=file_path,
        variables=variables,
        environment=environment,
    )


def _interpolated_string(
    *,
    value: object,
    field_path: str,
    file_path: Path,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> str:
    interpolated: object = interpolate_config_value(
        value=value,
        variables=variables,
        environment=environment,
        field_path=f"{file_path}:{field_path}",
    )
    if not isinstance(interpolated, str) or not interpolated:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {field_path} must resolve to a non-empty string"
        )
    return interpolated


def _validate_keys(
    *, mapping: dict[str, object], allowed: frozenset[str], field_path: str, file_path: Path
) -> None:
    unknown: tuple[str, ...] = tuple(sorted(set(mapping) - allowed))
    if unknown:
        raise PipelineDiscoveryError(
            f"Source file '{file_path}' {field_path} contains unsupported keys: "
            f"{', '.join(unknown)}"
        )
