"""Strict TOML project and local configuration loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqltide._native as _native
from sqltide.compiler.discovery.constants import (
    LEGACY_LOCAL_CONFIG_FILE_NAME,
    LEGACY_PROJECT_CONFIG_FILE_NAME,
    LOCAL_CONFIG_FILE_NAME,
    PROJECT_CONFIG_FILE_NAME,
)
from sqltide.compiler.discovery.exceptions import ProjectConfigError
from sqltide.compiler.discovery.models import (
    AuthoredProjectConfig,
    DiscoveredProjectFile,
    LoadedProjectConfiguration,
    LocalProjectConfig,
)


def load_project_configuration(*, project_dir: Path) -> LoadedProjectConfiguration:
    """Load one committed project config and optional local overrides."""

    project_path: Path = project_dir / PROJECT_CONFIG_FILE_NAME
    legacy_project_path: Path = project_dir / LEGACY_PROJECT_CONFIG_FILE_NAME
    _validate_config_paths(
        project_path=project_path,
        legacy_project_path=legacy_project_path,
        local_path=project_dir / LOCAL_CONFIG_FILE_NAME,
        legacy_local_path=project_dir / LEGACY_LOCAL_CONFIG_FILE_NAME,
    )
    project_source: DiscoveredProjectFile = _read_config_source(
        file_path=project_path,
        project_dir=project_dir,
    )
    project_payload: dict[str, object] = _parse_toml_source(project_source)
    project: AuthoredProjectConfig = _native.parse_project_config(
        project_payload,
        str(project_path),
        _timezone_exists,
    )
    local_path: Path = project_dir / LOCAL_CONFIG_FILE_NAME
    if not local_path.exists():
        return LoadedProjectConfiguration(
            project=project,
            local=LocalProjectConfig(),
            project_source=project_source,
            local_source=None,
        )
    local_source: DiscoveredProjectFile = _read_config_source(
        file_path=local_path,
        project_dir=project_dir,
    )
    return LoadedProjectConfiguration(
        project=project,
        local=_native.parse_local_config(_parse_toml_source(local_source), str(local_path)),
        project_source=project_source,
        local_source=local_source,
    )


def find_project_configuration_dir(path: Path) -> Path | None:
    """Find the nearest TOML or rejected pre-rename project configuration directory."""

    current_path: Path = path if path.is_dir() else path.parent
    candidate_dir: Path
    for candidate_dir in (current_path, *current_path.parents):
        if (candidate_dir / PROJECT_CONFIG_FILE_NAME).exists() or (
            candidate_dir / LEGACY_PROJECT_CONFIG_FILE_NAME
        ).exists():
            return candidate_dir
    return None


def _validate_config_paths(
    *,
    project_path: Path,
    legacy_project_path: Path,
    local_path: Path,
    legacy_local_path: Path,
) -> None:
    if legacy_project_path.exists():
        detail: str = "Remove the old file." if project_path.exists() else ""
        raise ProjectConfigError(
            f"{legacy_project_path} is not supported. Rename it to {PROJECT_CONFIG_FILE_NAME}. "
            f"{detail}".rstrip()
        )
    if legacy_local_path.exists():
        detail = "Remove the old file." if local_path.exists() else ""
        raise ProjectConfigError(
            f"{legacy_local_path} is not supported. Rename it to {LOCAL_CONFIG_FILE_NAME}. "
            f"{detail}".rstrip()
        )
    if not project_path.exists():
        raise ProjectConfigError(
            f"Project config not found: {project_path}. Run sqt inside a SQLTide project "
            "or pass --project-dir."
        )


def _read_config_source(*, file_path: Path, project_dir: Path) -> DiscoveredProjectFile:
    return DiscoveredProjectFile(
        file_path=file_path,
        relative_path=file_path.relative_to(project_dir),
        contents=file_path.read_text(encoding="utf-8"),
    )


def _parse_toml_source(source: DiscoveredProjectFile) -> dict[str, object]:
    try:
        payload: object = tomllib.loads(source.contents)
    except tomllib.TOMLDecodeError as error:
        raise ProjectConfigError(f"{source.file_path} contains invalid TOML: {error}") from error
    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{source.file_path} must contain a top-level mapping")
    return cast(dict[str, object], payload)


def _timezone_exists(name: str) -> bool:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True
