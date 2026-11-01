"""Render neutral adapter resources as ClickHouse SQL."""

import os
import re

import sqltide._native as _native
from sqltide.adapter.constants import (
    ADAPTER_SECRET_PLACEHOLDER_PREFIX,
    ADAPTER_SECRET_PLACEHOLDER_SUFFIX,
)
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import (
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterSetDifferenceComparisonRequest,
    AdapterStableView,
    AdapterTable,
    AdapterView,
)

_SECRET_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(
    re.escape(ADAPTER_SECRET_PLACEHOLDER_PREFIX)
    + r"([A-Za-z_][A-Za-z0-9_]*)"
    + re.escape(ADAPTER_SECRET_PLACEHOLDER_SUFFIX)
)


def render_clickhouse_ensure_database(database: str) -> str:
    """Render one manually executable ClickHouse database creation statement."""

    return _native.render_clickhouse_ensure_database(database)


def render_clickhouse_resource(
    *,
    resource: (
        AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView
    ),
    database: str,
    if_not_exists: bool = False,
) -> str:
    """Render one neutral adapter resource as ClickHouse DDL."""

    return _resolved_secrets(
        sql=_native.render_clickhouse_resource(resource, database, if_not_exists)
    )


def _resolved_secrets(*, sql: str) -> str:
    """Substitute credential placeholders from the environment at execution time."""

    resolved: str = sql
    match: re.Match[str] | None = _SECRET_PLACEHOLDER_PATTERN.search(resolved)
    while match is not None:
        variable_name: str = match.group(1)
        secret: str | None = os.environ.get(variable_name)
        if secret is None:
            raise AdapterCapabilityError(
                f"Credential environment variable '{variable_name}' is not set"
            )
        resolved = resolved.replace(match.group(0), secret)
        match = _SECRET_PLACEHOLDER_PATTERN.search(resolved)
    return resolved


def render_clickhouse_set_difference_comparison(
    *, request: AdapterSetDifferenceComparisonRequest
) -> str:
    """Render bidirectional bag comparisons with explicit NULL-safe equality."""

    return _native.render_clickhouse_set_difference_comparison(request.targets)
