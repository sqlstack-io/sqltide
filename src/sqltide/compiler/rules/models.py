"""A formatting candidate the compiler hands to `sqt sql`."""

from dataclasses import dataclass
from pathlib import Path

from sqltide.diagnostics.models import CompilerDiagnostic


@dataclass(frozen=True)
class FormattedSource:
    """A source candidate and remaining diagnostics, or a refusal that preserves its original."""

    path: Path
    original: str
    formatted: str
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    error: str | None = None
