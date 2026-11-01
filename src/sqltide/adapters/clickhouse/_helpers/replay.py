"""Render neutral replay requests as ClickHouse SQL."""

import sqltide._native as _native
from sqltide.adapter.models import (
    AdapterCapturedReplayRequest,
    AdapterDeploymentReplayRequest,
    AdapterReplayCoverageRequest,
)


def render_clickhouse_replay_coverage_query(request: AdapterReplayCoverageRequest) -> str:
    """Render checkpoint coverage for the rows selected by one replay window."""

    return _native.render_clickhouse_replay_coverage_query(request)


def render_clickhouse_replay_from_capture(request: AdapterCapturedReplayRequest) -> str:
    """Render one direct replay from process-owned typed boundaries."""

    return _native.render_clickhouse_replay_from_capture(request)


def render_clickhouse_replay_from_deployment(
    request: AdapterDeploymentReplayRequest,
) -> tuple[str, ...]:
    """Render seed and replay statements that read dynamic deployment metadata."""

    return tuple(_native.render_clickhouse_replay_from_deployment(request))
