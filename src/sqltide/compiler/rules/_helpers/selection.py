"""Resolve ignore scopes from compiler-owned models, never execution closure."""

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.compile.models import (
    CompiledProject,
    CompiledTableModel,
    CompiledViewModel,
    LogicalResourceKey,
)
from sqltide.compiler.graph.main.resolve_named_model_keys import resolve_named_model_keys
from sqltide.compiler.rules.exceptions import SqlRulesError


def resolve_rule_ignores(
    *, config: _native.RulesConfig, compiled_project: CompiledProject, project_dir: Path
) -> _native.RulesConfig:
    """Named pipelines own their models; downstream resources retain their checks."""

    scopes: list[list[str]] = config.ignore_selectors()
    if not any(scopes):
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
    resolved: list[list[str]] = []
    for selectors in scopes:
        if not selectors:
            resolved.append([])
            continue
        keys: frozenset[LogicalResourceKey] = resolve_named_model_keys(
            compiled_pipelines=compiled_project.pipelines, selectors=tuple(selectors)
        )
        paths: list[str] = [paths_by_key[key] for key in keys if key in paths_by_key]
        if not paths:
            raise SqlRulesError("Rule ignore selectors must resolve to authored model files")
        resolved.append(paths)
    return config.with_selector_paths(resolved)
