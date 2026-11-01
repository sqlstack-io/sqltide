"""SQLClean commands with explicit, race-checked source writes."""

from pathlib import Path

from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.models import ResolvedCliInvocation
from sqltide.cli.entry.types import CliCommand
from sqltide.cli.sql._helpers.output import report_format_results, report_lint_results
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.rules.main.format_project_sql import format_project_sql
from sqltide.compiler.rules.models import FormattedSource
from sqltide.diagnostics.models import CompilerDiagnostic


def run_sql(*, invocation: ResolvedCliInvocation, adapter_profile: CompilerAdapterProfile) -> int:
    """Lint read-only or format explicitly; check and diff modes never write."""

    root: Path = invocation.pipelines_root or (invocation.project_dir or Path.cwd()) / "pipelines"
    selection: tuple[str, ...] | None = (
        None if invocation.args.select is None else tuple(invocation.args.select)
    )
    paths: tuple[Path, ...] = tuple(
        (path if path.is_absolute() else root.parent / path).resolve()
        for path in invocation.args.paths
    )
    if any(not path.is_relative_to(root.parent.resolve()) or not path.exists() for path in paths):
        raise CliUserError("SQL paths must exist inside the project")
    if invocation.args.command == CliCommand.LINT:
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=root,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
            enforce_rules=False,
            rule_selection=selection,
        )
        diagnostics: tuple[CompilerDiagnostic, ...] = tuple(
            item
            for item in analysis.diagnostics
            if item.location is not None and _selected(path=item.location.path, paths=paths)
        )
        report_lint_results(diagnostics=diagnostics, json_output=invocation.args.json)
        return int(bool(diagnostics))
    results: tuple[FormattedSource, ...] = format_project_sql(
        root=root,
        loaded_project=invocation.loaded_project,
        adapter_profile=adapter_profile,
        selection=selection,
        paths=paths,
        fix=invocation.args.fix,
    )
    return report_format_results(results=results, args=invocation.args)


def _selected(*, path: Path, paths: tuple[Path, ...]) -> bool:
    return not paths or any(
        path.resolve() == selected or path.resolve().is_relative_to(selected) for selected in paths
    )
