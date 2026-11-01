"""Resolve ignore scopes from compiler-owned models, never execution closure."""

from dataclasses import replace
from pathlib import Path

from sqltide.compiler.compile.models import (
    CompiledProject,
    CompiledTableModel,
    CompiledViewModel,
    LogicalResourceKey,
)
from sqltide.compiler.graph.main.resolve_named_model_keys import resolve_named_model_keys
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import RuleIgnore, RulesConfig


def resolve_rule_ignores(
    *, config: RulesConfig, compiled_project: CompiledProject, project_dir: Path
) -> RulesConfig:
    """Named pipelines own their models; downstream resources retain their checks."""

    if not any(entry.selectors for entry in config.rule_ignores):
        return config
    paths_by_key: dict[LogicalResourceKey, str] = {}
    for model in compiled_project.models:
        path: Path | None = (
            model.transform.source_file_path
            if isinstance(model, CompiledTableModel)
            else model.view.source_file_path
            if isinstance(model, CompiledViewModel)
            else None
        )
        if path is not None:
            paths_by_key[model.key] = path.relative_to(project_dir).as_posix()
    resolved: list[RuleIgnore] = []
    for entry in config.rule_ignores:
        if not entry.selectors:
            resolved.append(entry)
            continue
        keys: frozenset[LogicalResourceKey] = resolve_named_model_keys(
            compiled_pipelines=compiled_project.pipelines, selectors=entry.selectors
        )
        paths: set[str] = {paths_by_key[key] for key in keys if key in paths_by_key}
        if not paths:
            raise SqlRulesError("Rule ignore selectors must resolve to authored model files")
        resolved.append(replace(entry, paths=tuple(sorted(paths | set(entry.paths))), selectors=()))
    return replace(config, rule_ignores=tuple(resolved))
