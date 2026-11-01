"""Evaluate published SQLClean Rules and preserve authored diagnostics and fix ownership."""

import json
from pathlib import Path
from typing import Any

import sqltide._native as _native
from sqltide.compiler.rules._helpers.bodies import source_range
from sqltide.compiler.rules._helpers.configuration import selected_rules
from sqltide.compiler.rules._helpers.policy import apply_rule_policy
from sqltide.compiler.rules.constants import (
    ERROR_KEY,
    QUALIFY_CODE,
    SAFE_FIX_CODES,
    SNOWFLAKE_DIALECT,
    SUPPRESSION_CODE,
)
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import LintBody, RuleFinding, RulesConfig
from sqltide.compiler.test_discovery.constants import (
    ASSERT_CTE_PREFIX,
    EXPECTED_CTE_PREFIX,
    MACRO_ACTUAL_CTE_NAME,
    MACRO_EXPECTED_CTE_NAME,
    REF_CTE_PREFIX,
    SOURCE_CTE_PREFIX,
)
from sqltide.diagnostics.models import CompilerDiagnostic, SourceLocation
from sqltide.diagnostics.types import DiagnosticPhase, DiagnosticSeverity


def evaluate_rules(
    *,
    bodies: tuple[LintBody, ...],
    config: RulesConfig,
    dialect: str,
    project_dir: Path,
    check_stale: bool = True,
) -> tuple[RuleFinding, ...]:
    """Return all findings; a disabled selection avoids the native parser entirely."""

    selected: tuple[str, ...] = selected_rules(config=config)
    active: tuple[LintBody, ...] = tuple(body for body in bodies if body.enabled)
    findings: list[RuleFinding] = []
    if selected:
        requests: list[dict[str, Any]] = [
            _request(body=body, selected=selected, config=config, dialect=dialect)
            for body in active
        ]
        responses: list[dict[str, Any]] = json.loads(
            _native.sqlclean_lint_batch(json.dumps(requests))
        )
        for body, response in zip(active, responses, strict=True):
            if ERROR_KEY in response:
                raise SqlRulesError(
                    f"{body.path}: SQLClean could not analyze SQL: {response[ERROR_KEY]}"
                )
            for diagnostic in response["Ok"]["diagnostics"]:
                finding: RuleFinding = _finding(body=body, raw=diagnostic, dialect=dialect)
                findings.append(finding)
    findings = list(
        apply_rule_policy(
            findings=tuple(findings),
            config=config,
            selected=selected if check_stale else (),
            project_dir=project_dir,
        )
    )
    if selected:
        seen: set[Path] = set()
        for body in active:
            if body.path in seen:
                continue
            seen.add(body.path)
            for start, end in _native.sqlclean_suppressions(body.contents):
                diagnostic: CompilerDiagnostic = _diagnostic(
                    body=body,
                    code=SUPPRESSION_CODE,
                    message="Inline Rule suppressions are not supported",
                    remediation=(
                        "Declare a reviewed [[rules.rule_exceptions]] entry with rule, path "
                        "and reason in sqltide_project.toml."
                    ),
                    start=start,
                    end=end,
                )
                findings.append(
                    RuleFinding(diagnostic=diagnostic, path=body.path, start=start, end=end)
                )
    return tuple(
        sorted(findings, key=lambda item: (str(item.path), item.start, item.diagnostic.code))
    )


def _request(
    *, body: LintBody, selected: tuple[str, ...], config: RulesConfig, dialect: str
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "version": 1,
        "sql": body.sql,
        "dialect": dialect,
        "enabled_rules": selected,
        "dependency_identifiers": body.dependencies,
        "external_identifiers": body.external_identifiers,
        "relation_names": body.relation_names,
        "sql_macro_outputs": [{"start": start, "end": end} for start, end in body.macro_outputs],
        "config_file": "sqltide_project.toml",
        **config.thresholds,
    }
    if body.fixture:
        request["fixture_harness"] = {
            "cte_prefixes": [
                REF_CTE_PREFIX,
                SOURCE_CTE_PREFIX,
                EXPECTED_CTE_PREFIX,
                ASSERT_CTE_PREFIX,
                MACRO_ACTUAL_CTE_NAME,
                MACRO_EXPECTED_CTE_NAME,
            ],
            "input_cte_prefixes": [REF_CTE_PREFIX, SOURCE_CTE_PREFIX],
        }
    return request


def _finding(*, body: LintBody, raw: dict[str, Any], dialect: str) -> RuleFinding:
    mapped: tuple[int, int] | None = source_range(body=body, start=raw["start"], end=raw["end"])
    if mapped is None:
        raise SqlRulesError("SQLClean diagnostic could not be mapped to authored SQL")
    start, end = mapped
    fix: dict[str, Any] | None = raw.get("fix")
    replacement: str | None = None
    fix_start: int | None = None
    fix_end: int | None = None
    if (
        fix is not None
        and body.model
        and raw["code"] in SAFE_FIX_CODES
        and (raw["code"] != QUALIFY_CODE or dialect == SNOWFLAKE_DIALECT)
    ):
        mapped_fix: tuple[int, int] | None = source_range(
            body=body, start=fix["start"], end=fix["end"], edit=True
        )
        if mapped_fix is not None:
            fix_start, fix_end = mapped_fix
            replacement = fix["replacement"]
    return RuleFinding(
        diagnostic=_diagnostic(
            body=body,
            code=raw["code"],
            message=raw["message"],
            remediation=raw["remediation"],
            start=start,
            end=end,
        ),
        path=body.path,
        start=start,
        end=end,
        replacement=replacement,
        fix_start=fix_start,
        fix_end=fix_end,
    )


def _diagnostic(
    *, body: LintBody, code: str, message: str, remediation: str, start: int, end: int
) -> CompilerDiagnostic:
    line: int = body.contents.count("\n", 0, start) + 1
    column: int = start - body.contents.rfind("\n", 0, start)
    end_line: int = body.contents.count("\n", 0, end) + 1
    end_column: int = end - body.contents.rfind("\n", 0, end)
    return CompilerDiagnostic(
        phase=DiagnosticPhase.COMPILATION,
        severity=DiagnosticSeverity.ERROR,
        code=code,
        message=message,
        resource_name=body.path.stem,
        location=SourceLocation(
            path=body.path, line=line, column=column, end_line=end_line, end_column=end_column
        ),
        help=remediation,
    )
