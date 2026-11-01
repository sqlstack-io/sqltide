"""Human and machine output for read-only lint and explicit formatting commands."""

import argparse
import json
import os
import stat
from dataclasses import asdict
from difflib import unified_diff
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqltide.compiler.rules.models import FormattedSource
from sqltide.diagnostics.main.render_diagnostic import render_diagnostic
from sqltide.diagnostics.models import CompilerDiagnostic


def report_lint_results(*, diagnostics: tuple[CompilerDiagnostic, ...], json_output: bool) -> None:
    """Keep machine output a single JSON object, without incidental progress output."""

    if json_output:
        print(json.dumps({"diagnostics": [asdict(item) for item in diagnostics]}, default=str))
    else:
        for diagnostic in diagnostics:
            print(render_diagnostic(diagnostic=diagnostic))


def report_format_results(*, results: tuple[FormattedSource, ...], args: argparse.Namespace) -> int:
    """Write only accepted candidates, preserving a nonzero exit for remaining Rule findings."""

    changed: list[str] = []
    failures: list[str] = []
    diagnostics: list[CompilerDiagnostic] = []
    for result in results:
        diagnostics.extend(result.diagnostics)
        if result.error is not None:
            failures.append(result.error)
            continue
        if result.formatted == result.original:
            continue
        changed.append(str(result.path))
        if args.diff and not args.json:
            print(
                "".join(
                    unified_diff(
                        result.original.splitlines(keepends=True),
                        result.formatted.splitlines(keepends=True),
                        fromfile=str(result.path),
                        tofile=str(result.path),
                    )
                ),
                end="",
            )
        if not args.check and not args.diff:
            failure: str | None = _write_source(result=result)
            if failure is not None:
                failures.append(failure)
    if args.json:
        print(
            json.dumps(
                {
                    "formatted_files": changed,
                    "failures": failures,
                    "diagnostics": [asdict(item) for item in diagnostics],
                },
                default=str,
            )
        )
    else:
        for failure in failures:
            print(failure)
        for diagnostic in diagnostics:
            print(render_diagnostic(diagnostic=diagnostic))
        if not args.diff:
            print(f"{'Would format' if args.check else 'Formatted'} {len(changed)} file(s)")
    return int(bool(failures or diagnostics) or args.check and bool(changed))


def _write_source(*, result: FormattedSource) -> str | None:
    """Stage and sync beside the source, then replace only the unchanged raw snapshot."""

    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=result.path.parent, prefix=f".{result.path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(stat.S_IMODE(result.path.stat().st_mode))
            _ = handle.write(result.formatted.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if result.path.read_bytes() != result.original.encode("utf-8"):
            return f"{result.path}: source changed during formatting; no write performed"
        _ = temporary.replace(result.path)
    except OSError as error:
        return f"{result.path}: {error}; original source preserved"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return None
