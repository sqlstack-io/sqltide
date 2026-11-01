"""Run one project compile and reduce failures to structured error info."""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from dataclasses import asdict
from importlib.metadata import version

from sqltide.compiler.pipeline.models import CompilationTimings, CompileAnalysis
from sqltide.dev_server.models import CompileErrorInfo, CompileOutcome
from sqltide.dev_server.types import CompileStateKind
from sqltide.diagnostics.models import CompilerDiagnostic, SourceLocation


def build_compile_outcome(
    *,
    run_compile: Callable[[], CompileAnalysis],
) -> CompileOutcome:
    """Execute one injected compile callable and capture success or failure."""

    compiled_at: str = _utc_now_iso()
    version_key: str = f"{time.monotonic_ns():x}"
    try:
        analysis: CompileAnalysis = run_compile()
    except Exception as error:
        return CompileOutcome(
            state=CompileStateKind.FAILING,
            version_key=version_key,
            compiled_at=compiled_at,
            error=describe_compile_error(error=error),
        )
    return CompileOutcome(
        state=CompileStateKind.OK,
        version_key=version_key,
        compiled_at=compiled_at,
        analysis=analysis,
        timings=analysis.timings,
    )


def describe_compile_error(*, error: Exception) -> CompileErrorInfo:
    """Reduce one compile exception to message plus source coordinates."""

    diagnostic: object = getattr(error, "diagnostic", None)
    if not isinstance(diagnostic, CompilerDiagnostic):
        return CompileErrorInfo(message=str(error))
    location: SourceLocation | None = diagnostic.location
    if location is None:
        return CompileErrorInfo(message=diagnostic.message)
    return CompileErrorInfo(
        message=diagnostic.message,
        path=str(location.path),
        line=location.line,
        column=location.column,
        end_line=location.end_line,
        end_column=location.end_column,
    )


def _utc_now_iso() -> str:
    return datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")


def build_status_payload(
    *,
    outcome: CompileOutcome,
    warehouse_status: dict[str, object],
) -> dict[str, object]:
    """Build the cheap polled status payload."""

    return {
        "toolVersion": version("sqltide"),
        "compile": {
            "state": str(outcome.state),
            "versionKey": outcome.version_key,
            "compiledAt": outcome.compiled_at,
            "timings": _timings_payload(outcome.timings),
            "error": _error_payload(outcome.error),
        },
        "warehouse": warehouse_status,
    }


def _timings_payload(timings: CompilationTimings | None) -> dict[str, int] | None:
    if timings is None:
        return None
    return {
        "discoveryMs": timings.discovery_ms,
        "compileInputsMs": timings.compile_inputs_ms,
        "assemblyMs": timings.assembly_ms,
        "graphMs": timings.graph_ms,
        "realizationMs": timings.realization_ms,
        "rulesMs": timings.rules_ms,
    }


def _error_payload(error: CompileErrorInfo | None) -> dict[str, object] | None:
    if error is None:
        return None
    return asdict(error)
