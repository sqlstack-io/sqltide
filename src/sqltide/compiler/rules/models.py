"""Invocation-scoped Rules configuration and authored diagnostic ownership."""

from dataclasses import dataclass, field
from pathlib import Path

from sqltide.diagnostics.models import CompilerDiagnostic


@dataclass(frozen=True)
class RuleExemption:
    """A reviewed deviation restricted to a path and an exact Rule."""

    rule: str
    path: str
    reason: str
    relation: str | None = None
    cte: str | None = None


@dataclass(frozen=True)
class RuleIgnore:
    """Explicit resource/path policy, distinct from stale-checked exact exceptions."""

    rules: tuple[str, ...]
    paths: tuple[str, ...]
    reason: str
    selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RulesConfig:
    """Omitted selection enables SQLClean's defaults; empty selection disables Rules."""

    enabled: bool = True
    select: tuple[str, ...] | None = None
    ignore: tuple[str, ...] = ()
    rule_exceptions: tuple[RuleExemption, ...] = ()
    rule_ignores: tuple[RuleIgnore, ...] = ()
    thresholds: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LintBody:
    """SQL body and reversible expansion passes, all in Unicode character offsets."""

    path: Path
    contents: str
    start: int
    end: int
    sql: str
    model: bool
    enabled: bool
    passes: tuple[tuple[tuple[int, int, int, int], ...], ...] = ()
    dependencies: tuple[str, ...] = ()
    relation_names: dict[str, str] = field(default_factory=dict)
    external_identifiers: tuple[str, ...] = ()
    macro_outputs: tuple[tuple[int, int], ...] = ()
    fixture: bool = False


@dataclass(frozen=True)
class RuleFinding:
    """Diagnostic and an optional edit proven to touch only authored model SQL."""

    diagnostic: CompilerDiagnostic
    path: Path
    start: int
    end: int
    replacement: str | None = None
    fix_start: int | None = None
    fix_end: int | None = None


@dataclass(frozen=True)
class FormattedSource:
    """A source candidate and remaining diagnostics, or a refusal that preserves its original."""

    path: Path
    original: str
    formatted: str
    diagnostics: tuple[CompilerDiagnostic, ...] = ()
    error: str | None = None
