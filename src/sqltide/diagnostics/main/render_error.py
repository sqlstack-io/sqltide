"""Render an exception through its structured diagnostic when present."""

from sqltide.diagnostics.main.render_diagnostic import render_diagnostic
from sqltide.diagnostics.models import CompilerDiagnostic


def render_error(error: BaseException) -> str:
    """Return structured diagnostic text or the exception's ordinary message."""

    diagnostic: object = getattr(error, "diagnostic", None)
    if isinstance(diagnostic, CompilerDiagnostic):
        return render_diagnostic(diagnostic=diagnostic)
    return str(error)
