"""Verify each edited model to a fixed point, without writing unverified candidates."""

from dataclasses import replace
from pathlib import Path

from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import DiscoveredProjectFile, LoadedProject
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.rules._helpers.bodies import lint_bodies
from sqltide.compiler.rules._helpers.configuration import rules_config
from sqltide.compiler.rules._helpers.evaluation import evaluate_rules
from sqltide.compiler.rules._helpers.formatting import formatted_file
from sqltide.compiler.rules._helpers.selection import resolve_rule_ignores
from sqltide.compiler.rules._helpers.verification import verify_fix
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import RuleFinding, RulesConfig
from sqltide.diagnostics.models import CompilerDiagnostic


def fixed_file(
    *,
    source: DiscoveredProjectFile,
    keyword: str,
    root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    selection: tuple[str, ...] | None,
) -> tuple[str, tuple[CompilerDiagnostic, ...]]:
    """Only the allowlisted authored model edits reach compiler verification and then disk."""

    path: Path = source.file_path
    candidate: str = source.contents
    config: RulesConfig = rules_config(loaded_project=loaded_project, selection=selection)
    baseline: CompileAnalysis = analyze_project(
        pipelines_root=root,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
        enforce_rules=False,
        rule_selection=selection,
    )
    config = resolve_rule_ignores(
        config=config, compiled_project=baseline.compiled_project, project_dir=root.parent
    )
    seen: set[str] = set()
    for _ in range(128):
        if candidate in seen:
            raise SqlRulesError(f"{path}: Rule fixes cycle; original source preserved")
        seen.add(candidate)
        current: CompileAnalysis = analyze_project(
            pipelines_root=root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
            enforce_rules=False,
            source_overrides={path: candidate},
            rule_selection=selection,
        )
        verify_fix(before=baseline, after=current, edited=path)
        findings: tuple[RuleFinding, ...] = (
            evaluate_rules(
                bodies=tuple(
                    body for body in lint_bodies(inputs=current.compile_inputs) if body.path == path
                ),
                config=config,
                dialect=adapter_profile.sql_analysis_dialect,
                project_dir=root.parent,
                check_stale=False,
            )
            if adapter_profile.run_sql_rules
            else ()
        )
        edits: list[RuleFinding] = sorted(
            (finding for finding in findings if finding.replacement is not None),
            key=lambda finding: finding.fix_start or 0,
        )
        if not edits:
            formatted: str = formatted_file(
                source=replace(source, contents=candidate),
                keyword=keyword,
                dialect=adapter_profile.sql_analysis_dialect,
            )
            final: CompileAnalysis = analyze_project(
                pipelines_root=root,
                loaded_project=loaded_project,
                adapter_profile=adapter_profile,
                enforce_rules=False,
                source_overrides={path: formatted},
                rule_selection=selection,
            )
            verify_fix(before=baseline, after=final, edited=path)
            return formatted, tuple(
                diagnostic
                for diagnostic in final.diagnostics
                if diagnostic.location is not None and diagnostic.location.path == path
            )
        edit: RuleFinding = edits[0]
        if edit.fix_start is None or edit.fix_end is None or edit.replacement is None:
            raise SqlRulesError(f"{path}: Rule edit is incomplete; original source preserved")
        candidate = candidate[: edit.fix_start] + edit.replacement + candidate[edit.fix_end :]
    raise SqlRulesError(f"{path}: Rule fixes exceeded 128 passes; original source preserved")
