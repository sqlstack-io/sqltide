"""Project and local configuration precedence resolution."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

import sqltide._native as _native
from sqltide.compiler.discovery._helpers.configuration import timezone_exists
from sqltide.compiler.discovery.models import (
    EffectiveProjectConfiguration,
    LoadedProjectConfiguration,
)


def resolve_effective_project_configuration(
    *,
    loaded: LoadedProjectConfiguration,
    selected_target: str | None,
    cli_variables: Mapping[str, object],
    environment: Mapping[str, str],
) -> EffectiveProjectConfiguration:
    """Resolve one connection-lazy effective project configuration."""

    return _native.resolve_effective_configuration(
        tomllib.loads(loaded.project_source.contents),
        str(loaded.project_source.file_path),
        None if loaded.local_source is None else tomllib.loads(loaded.local_source.contents),
        "" if loaded.local_source is None else str(loaded.local_source.file_path),
        selected_target,
        cli_variables,
        environment,
        timezone_exists,
    )
