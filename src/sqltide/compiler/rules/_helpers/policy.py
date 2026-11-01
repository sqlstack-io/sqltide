"""SQLBuild-compatible exact exceptions and reasoned path ignores via published policy."""

import json
from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.rules.models import RuleExemption, RuleFinding, RulesConfig


def apply_rule_policy(
    *,
    findings: tuple[RuleFinding, ...],
    config: RulesConfig,
    selected: tuple[str, ...],
    project_dir: Path,
) -> tuple[RuleFinding, ...]:
    """Preserve host fix ownership while the shared engine validates suppression lifecycle."""

    if not config.rule_exceptions and not config.rule_ignores:
        return findings
    kept: list[int] = _native.sqlclean_apply_policy(
        json.dumps(
            {
                "findings": [
                    {
                        "code": finding.diagnostic.code,
                        "path": finding.path.relative_to(project_dir).as_posix(),
                        "line": None,
                        "column": None,
                        "symbol": _covering_symbol(
                            finding=finding, config=config, project_dir=project_dir
                        ),
                        "message": finding.diagnostic.message,
                        "remediation": None,
                        "severity": "blocking",
                    }
                    for finding in findings
                ],
                "evaluated_codes": selected,
                "suppressions": [
                    {
                        "code": entry.rule,
                        "path": entry.path,
                        "symbol": _symbol(entry=entry),
                        "reason": entry.reason,
                    }
                    for entry in config.rule_exceptions
                ],
                "scoped_ignores": [
                    {"selectors": entry.rules, "paths": entry.paths, "reason": entry.reason}
                    for entry in config.rule_ignores
                ],
            }
        )
    )
    return tuple(findings[index] for index in kept)


def _symbol(*, entry: RuleExemption) -> str | None:
    if entry.relation is None:
        return None
    return f"relation {entry.relation.lower()}" + (
        "" if entry.cte is None else f" in CTE {entry.cte.lower()}"
    )


def _covering_symbol(*, finding: RuleFinding, config: RulesConfig, project_dir: Path) -> str | None:
    subject: tuple[str, str | None] | None = _native.sqlclean_cross_join_subject(
        finding.diagnostic.message
    )
    if subject is None:
        return None
    matching: list[RuleExemption] = [
        entry
        for entry in config.rule_exceptions
        if entry.rule == finding.diagnostic.code
        and finding.path.relative_to(project_dir).as_posix() == entry.path
        and entry.relation is not None
        and entry.relation.encode().lower() == subject[0].encode().lower()
        and (
            entry.cte is None
            or subject[1] is not None
            and entry.cte.encode().lower() == subject[1].encode().lower()
        )
    ]
    matching.sort(key=lambda entry: entry.cte is None)
    return None if not matching else _symbol(entry=matching[0])
