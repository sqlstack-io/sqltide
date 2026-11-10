"""Dev server type declarations."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from sqltide.compiler.pipeline.models import CompileAnalysis
    from sqltide.dev_server.models import CompileOutcome

type KafkaClientKey = tuple[str, tuple[tuple[str, str], ...]]
type KafkaLagOffsets = tuple[
    tuple[tuple[str, int, int | None], ...], tuple[int, ...], tuple[tuple[int, int], ...]
]


class ActivityTone(StrEnum):
    """How an activity line should read: routine, good news, bad news, or caution."""

    NEUTRAL = "neutral"
    GOOD = "good"
    BAD = "bad"
    CAUTION = "caution"


class RunPresentationStatus(StrEnum):
    """Terminal facts and reversible liveness states shown for a run."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"
    CANCELLATION_FAILED = "cancellation_failed"
    RUNNING = "running"
    UNRESPONSIVE = "unresponsive"
    PRESUMED_FAILED = "presumed_failed"


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


class CompileStateKind(StrEnum):
    """Whether the held project compile is servable or failing."""

    OK = "ok"
    FAILING = "failing"


class ReplayAnchorReason(StrEnum):
    """Why a model is or is not a replay anchor."""

    ELIGIBLE = "eligible"
    AGGREGATE = "aggregate"
    MUTABLE_REF = "mutable_ref"
    NEVER = "never"
    LINEAGE_LOSS = "lineage_loss"
    VIEW = "view"


class Freshness(StrEnum):
    """Derived source or model freshness against its authored policy."""

    FRESH = "fresh"
    LAGGING = "lagging"
    STALLED = "stalled"


class SensorSchedulerState(StrEnum):
    """Current lifecycle disposition of the hosted sensor dispatcher."""

    DISABLED = "disabled"
    IDLE = "idle"
    RUNNING = "running"
    STANDBY = "standby"
    BACKING_OFF = "backing_off"


class AuditScheduleState(StrEnum):
    """Current scheduler disposition of one compiled audit or target."""

    DISABLED = "disabled"
    IDLE = "idle"
    DUE = "due"
    SCHEDULED = "scheduled"
    WARMING_UP = "warming_up"
    NOT_MATERIALIZED = "not_materialized"
    RUNNING = "running"
    BLOCKED = "blocked"
    BACKING_OFF = "backing_off"
