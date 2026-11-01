"""Strict SQL Rules selection and reviewed exception configuration."""

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import cast

import sqltide._native as _native
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.rules._helpers.policy import apply_rule_policy
from sqltide.compiler.rules.constants import (
    CROSS_JOIN_CODE,
    CTE_KEY,
    INVALID_PATH_CHARACTERS,
    INVALID_PATH_PARTS,
    RELATION_KEY,
    SELECT_KEY,
    SQLCLEAN_FAMILY,
    THRESHOLD_KEYS,
)
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import RuleExemption, RuleIgnore, RulesConfig


def rules_config(
    *, loaded_project: LoadedProject | None, selection: tuple[str, ...] | None = None
) -> RulesConfig:
    """Read retained project text, without rereading files or changing target resolution."""

    project: dict[str, object] = (
        {} if loaded_project is None else tomllib.loads(loaded_project.source_file.contents)
    )
    settings: dict[str, object] = _table(
        value=project.get("settings", {}), allowed={"sql_analysis"}
    )
    enabled: object = settings.get("sql_analysis", True)
    if type(enabled) is not bool:
        raise SqlRulesError("settings.sql_analysis must be a boolean")
    payload: dict[str, object] = _table(
        value=project.get("rules", {}),
        allowed={"select", "ignore", "rule_exceptions", "rule_ignores", "thresholds"},
    )
    select: tuple[str, ...] | None = (
        _selectors(value=list(selection))
        if selection is not None
        else None
        if SELECT_KEY not in payload
        else _selectors(value=payload[SELECT_KEY])
    )
    ignore: tuple[str, ...] = _selectors(value=payload.get("ignore", []))
    exceptions: object = payload.get("rule_exceptions", [])
    if not isinstance(exceptions, list):
        raise SqlRulesError("rules.rule_exceptions must be an array of tables")
    ignores: object = payload.get("rule_ignores", [])
    if not isinstance(ignores, list):
        raise SqlRulesError("rules.rule_ignores must be an array of tables")
    thresholds: object = payload.get("thresholds", {})
    if not isinstance(thresholds, dict) or any(
        key not in THRESHOLD_KEYS or type(value) is not int or value < 1
        for key, value in thresholds.items()
    ):
        raise SqlRulesError(
            "rules.thresholds requires known thresholds with positive integer values"
        )
    project_dir: Path = (
        loaded_project.source_file.file_path.parent if loaded_project is not None else Path.cwd()
    )
    config: RulesConfig = RulesConfig(
        enabled=enabled,
        select=select,
        ignore=ignore,
        rule_exceptions=tuple(
            _exception(payload=item, project_dir=project_dir) for item in exceptions
        ),
        rule_ignores=tuple(_ignore(payload=item, project_dir=project_dir) for item in ignores),
        thresholds=cast(dict[str, int], thresholds),
    )
    _ = apply_rule_policy(
        findings=(),
        config=replace(
            config, rule_ignores=tuple(entry for entry in config.rule_ignores if entry.paths)
        ),
        selected=(),
        project_dir=project_dir,
    )
    return config


def selected_rules(*, config: RulesConfig) -> tuple[str, ...]:
    """An explicit SQLCLEAN family selects opt-in Rules as well as defaults."""

    return tuple(
        code
        for code, _, _, default in _native.sqlclean_rules()
        if config.enabled
        and (default if config.select is None else code.startswith(config.select))
        and not code.startswith(config.ignore)
    )


def _selectors(*, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SqlRulesError("Rule selectors must be a list of exact codes or SQLCLEAN")
    known: set[str] = {code for code, _, _, _ in _native.sqlclean_rules()} | {SQLCLEAN_FAMILY}
    if any(item not in known for item in value):
        raise SqlRulesError("Unknown Rule selector; use an exact SQLCLEAN code or SQLCLEAN")
    return tuple(item for item in value if isinstance(item, str))


def _table(*, value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SqlRulesError("Rules configuration must be a table")
    payload: dict[str, object] = cast(dict[str, object], value)
    unknown: set[str] = payload.keys() - allowed
    if unknown:
        raise SqlRulesError(f"Unsupported Rules configuration keys: {', '.join(sorted(unknown))}")
    return payload


def _exception(*, payload: object, project_dir: Path) -> RuleExemption:
    table: dict[str, object] = _table(
        value=payload, allowed={"rule", "path", "reason", "relation", "cte"}
    )
    rule: str = _text(value=table.get("rule"))
    path: str = _text(value=table.get("path"))
    reason: str = _text(value=table.get("reason"))
    relation: str | None = _text(value=table[RELATION_KEY]) if RELATION_KEY in table else None
    cte: str | None = _text(value=table[CTE_KEY]) if CTE_KEY in table else None
    codes: tuple[str, ...] = _selectors(value=[rule])
    if codes == (SQLCLEAN_FAMILY,):
        raise SqlRulesError("Rule exceptions require an exact Rule code")
    if any(part in INVALID_PATH_PARTS for part in path.split("/")) or any(
        character in path for character in INVALID_PATH_CHARACTERS
    ):
        raise SqlRulesError("Rule exception paths must be exact normalized project-relative files")
    if not (project_dir / path).is_file():
        raise SqlRulesError(f"Rule exception path does not exist: {path}")
    if cte is not None and relation is None:
        raise SqlRulesError("Rule exception cte requires relation")
    if relation is not None and rule != CROSS_JOIN_CODE:
        raise SqlRulesError("Only SQLCLEAN038 supports relation exceptions")
    return RuleExemption(rule=rule, path=path, reason=reason, relation=relation, cte=cte)


def _text(*, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SqlRulesError("Rule exception fields must be non-empty strings")
    return value


def _ignore(*, payload: object, project_dir: Path) -> RuleIgnore:
    table: dict[str, object] = _table(
        value=payload, allowed={"rules", "paths", "selectors", "reason"}
    )
    rules: tuple[str, ...] = _selectors(value=table.get("rules", []))
    paths: object = table.get("paths", [])
    selectors: object = table.get("selectors", [])
    if not isinstance(selectors, list):
        raise SqlRulesError("Rule ignore selectors must be a list of non-empty strings")
    scopes: tuple[str, ...] = tuple(_text(value=selector) for selector in selectors)
    if not rules or not isinstance(paths, list) or not (paths or scopes):
        raise SqlRulesError("Rule ignores require rules, paths or selectors, and reason")
    normalized: tuple[str, ...] = tuple(_text(value=path) for path in paths)
    for path in normalized:
        if not any(character in path for character in "*?[{") and not (project_dir / path).exists():
            raise SqlRulesError(f"Rule ignore path does not exist: {path}")
    return RuleIgnore(
        rules=rules, paths=normalized, selectors=scopes, reason=_text(value=table.get("reason"))
    )
