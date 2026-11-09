"""CLI plan models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlanCommandOptions:
    """Every operator-supplied option for one `sqt plan` invocation."""

    pipelines_root: Path
    database: str | None
    selectors: tuple[str, ...]
    deployment_id: str | None
    full_refresh: bool
    start_time: str | None
    json_output: bool
    verbose: bool
    changed: bool = False
    include_missing_upstream: bool = False
