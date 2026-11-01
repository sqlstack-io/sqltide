"""Run compiler-owned configurable SQL Rules after mandatory compiler checks."""

import time
from dataclasses import replace

from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.rules._helpers.bodies import lint_bodies
from sqltide.compiler.rules._helpers.configuration import rules_config, selected_rules
from sqltide.compiler.rules._helpers.evaluation import evaluate_rules
from sqltide.compiler.rules._helpers.selection import resolve_rule_ignores
from sqltide.compiler.rules.exceptions import RulesError
from sqltide.compiler.rules.models import RuleFinding, RulesConfig


def evaluate_project_rules(
    *,
    analysis: CompileAnalysis,
    enforce: bool,
    selection: tuple[str, ...] | None,
) -> CompileAnalysis:
    """Attach timed authored findings, or fail before any artifacts are written."""

    start: int = time.monotonic_ns()
    config: RulesConfig = resolve_rule_ignores(
        config=rules_config(
            loaded_project=analysis.discovered_inputs.loaded_project, selection=selection
        ),
        compiled_project=analysis.compiled_project,
        project_dir=analysis.discovered_inputs.project_dir,
    )
    findings: tuple[RuleFinding, ...] = (
        evaluate_rules(
            bodies=lint_bodies(inputs=analysis.compile_inputs),
            config=config,
            dialect=analysis.adapter_profile.sql_analysis_dialect,
            project_dir=analysis.discovered_inputs.project_dir,
        )
        if analysis.adapter_profile.run_sql_rules and selected_rules(config=config)
        else ()
    )
    if enforce and findings:
        raise RulesError(findings=findings, defaults=config.select is None)
    return replace(
        analysis,
        diagnostics=tuple(finding.diagnostic for finding in findings),
        timings=replace(analysis.timings, rules_ms=(time.monotonic_ns() - start) // 1_000_000),
    )
