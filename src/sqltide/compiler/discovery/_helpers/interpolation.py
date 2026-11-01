"""Invocation-local project variable and environment interpolation."""

from __future__ import annotations

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import ProjectConfigError


def resolve_variable_values(
    *,
    values: Mapping[str, object],
    environment: Mapping[str, str],
    field_path_prefix: str,
    defer_missing_environment: bool = False,
) -> dict[str, object]:
    """Resolve all effective variable values with cycle detection."""

    try:
        return _native.resolve_variable_values(
            values, environment, field_path_prefix, defer_missing_environment
        )
    except ValueError as error:
        raise ProjectConfigError(str(error)) from None


def interpolate_config_value(
    *,
    value: object,
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    field_path: str,
) -> object:
    """Interpolate one effective non-connection configuration value."""

    try:
        return _native.interpolate_config_value(value, variables, environment, field_path)
    except ValueError as error:
        raise ProjectConfigError(str(error)) from None
