"""Provider discovery entry point for other SQLTide domains."""

from __future__ import annotations

from pathlib import Path

from sqltide.provider._helpers.discovery import (
    discover_provider_classes as _discover_provider_classes,
)
from sqltide.provider.models import DiscoveredProvider


def discover_provider_classes(*, project_dir: Path) -> tuple[DiscoveredProvider, ...]:
    """Discover provider classes under providers/."""

    return _discover_provider_classes(project_dir=project_dir)
