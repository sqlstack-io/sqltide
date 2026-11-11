"""Dev server type declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqltide._native import ActivityTone as ActivityTone
from sqltide._native import AuditScheduleState as AuditScheduleState
from sqltide._native import CompileStateKind as CompileStateKind
from sqltide._native import Freshness as Freshness
from sqltide._native import ReplayAnchorReason as ReplayAnchorReason
from sqltide._native import RunPresentationStatus as RunPresentationStatus
from sqltide._native import SensorSchedulerState as SensorSchedulerState

if TYPE_CHECKING:
    from pathlib import Path

    from sqltide.compiler.pipeline.models import CompileAnalysis
    from sqltide.dev_server.models import CompileOutcome

type KafkaClientKey = tuple[str, tuple[tuple[str, str], ...]]
type KafkaLagOffsets = tuple[
    tuple[tuple[str, int, int | None], ...], tuple[int, ...], tuple[tuple[int, int], ...]
]


class CompileAuthorizationGuard(Protocol):
    """Authorize a reload against the held successful analysis."""

    def __call__(self, *, analysis: CompileAnalysis | None) -> object:
        """Raise before compilation when the operation is denied."""


class DevServerReporter(Protocol):
    """Terminal narrator for the long-running dev server; implemented by the CLI."""

    def report_startup(
        self,
        *,
        outcome: CompileOutcome,
        project_dir: Path,
        database: str | None,
        warehouse_connected: bool,
        host: str,
        port: int,
    ) -> None:
        """Print the startup banner for one compiled (or failing) project."""

    def report_reload(self, *, outcome: CompileOutcome) -> None:
        """Print one activity line for a project reload outcome."""

    def report_activity(
        self, *, category: str, status: str, tone: ActivityTone, detail: str
    ) -> None:
        """Print one timestamped activity line."""

    def report_shutdown(self) -> None:
        """Print the shutdown line after the server stops."""
