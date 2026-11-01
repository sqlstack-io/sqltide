"""Selector helpers for SQL audits."""

from __future__ import annotations

from collections import defaultdict

from sqltide.cli.entry.exceptions import CliUserError
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.main.compiled_models import compiled_models
from sqltide.compiler.compile.models import CompiledModel, CompiledPipeline, ParsedRef
from sqltide.compiler.graph.constants import (
    PIPELINE_SELECTOR_NAMESPACE,
    SELECTOR_NAMESPACE_SEPARATOR,
    UPSTREAM_SELECTOR_PREFIX,
)


def select_loaded_sql_audits(
    *,
    loaded_audits: tuple[LoadedSqlAudit, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selectors: tuple[str, ...],
) -> tuple[LoadedSqlAudit, ...]:
    """Filter discovered SQL audits by referenced-model selectors."""

    if not selectors:
        return loaded_audits
    selected_model_names: frozenset[str] = _resolve_selected_model_names(
        compiled_pipelines=compiled_pipelines,
        selectors=selectors,
    )
    selected_audits: tuple[LoadedSqlAudit, ...] = tuple(
        loaded_audit
        for loaded_audit in loaded_audits
        if set(loaded_audit.referenced_model_names) & selected_model_names
    )
    if selected_audits:
        return selected_audits
    raise CliUserError("No SQL audits matched the requested selectors.")


def _resolve_selected_model_names(
    *,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    selectors: tuple[str, ...],
) -> frozenset[str]:
    pipeline_model_names: dict[str, tuple[str, ...]] = _model_names_by_pipeline(
        compiled_pipelines=compiled_pipelines
    )
    upstream_names_by_model: dict[str, tuple[str, ...]] = _build_upstream_model_graph(
        compiled_pipelines
    )
    known_model_names: frozenset[str] = frozenset(upstream_names_by_model)
    selected_model_names: set[str] = set()
    raw_selector: str
    for raw_selector in selectors:
        include_upstream: bool = raw_selector.startswith(UPSTREAM_SELECTOR_PREFIX)
        selector: str = raw_selector.removeprefix(UPSTREAM_SELECTOR_PREFIX)
        if not selector or UPSTREAM_SELECTOR_PREFIX in selector:
            raise CliUserError(
                f"Unsupported audit selector syntax '{raw_selector}'. Use bare model names, "
                "pipeline:<name>, or optional leading '+'."
            )
        base_model_names: tuple[str, ...] = _resolve_selector_target_names(
            selector=selector,
            pipeline_model_names=pipeline_model_names,
            known_model_names=known_model_names,
        )
        selected_model_names.update(base_model_names)
        if include_upstream:
            base_model_name: str
            for base_model_name in base_model_names:
                selected_model_names.update(
                    _expand_graph_neighbors(
                        start_name=base_model_name, neighbors_by_name=upstream_names_by_model
                    )
                )
    return frozenset(selected_model_names)


def _resolve_selector_target_names(
    *,
    selector: str,
    pipeline_model_names: dict[str, tuple[str, ...]],
    known_model_names: frozenset[str],
) -> tuple[str, ...]:
    if SELECTOR_NAMESPACE_SEPARATOR not in selector:
        if selector not in known_model_names:
            raise CliUserError(f"Unknown SQL audit selector model '{selector}'")
        return (selector,)
    selector_kind, selector_value = selector.split(SELECTOR_NAMESPACE_SEPARATOR, 1)
    if selector_kind != PIPELINE_SELECTOR_NAMESPACE:
        raise CliUserError(
            f"Unsupported audit selector namespace '{selector_kind}' in '{selector}'"
        )
    pipeline_targets: tuple[str, ...] | None = pipeline_model_names.get(selector_value)
    if pipeline_targets is None:
        raise CliUserError(f"Unknown SQL audit selector pipeline '{selector_value}'")
    if not pipeline_targets:
        raise CliUserError(
            f"SQL audit selector pipeline '{selector_value}' does not define any models"
        )
    return pipeline_targets


def _build_upstream_model_graph(
    compiled_pipelines: tuple[CompiledPipeline, ...],
) -> dict[str, tuple[str, ...]]:
    upstream_names_by_model: dict[str, set[str]] = defaultdict(set)
    known_model_names: set[str] = {
        compiled_model.key.name
        for compiled_model in compiled_models(compiled_pipelines=compiled_pipelines)
    }
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in compiled_pipelines:
        compiled_model: CompiledModel
        for compiled_model in compiled_pipeline.models:
            model_name: str = compiled_model.key.name
            upstream_names_by_model.setdefault(model_name, set())
            parsed_ref: ParsedRef
            for parsed_ref in compiled_model.parsed_refs:
                if parsed_ref.name not in known_model_names:
                    continue
                upstream_names_by_model[model_name].add(parsed_ref.name)
    return {name: tuple(sorted(neighbors)) for name, neighbors in upstream_names_by_model.items()}


def _expand_graph_neighbors(
    *,
    start_name: str,
    neighbors_by_name: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    visited_names: set[str] = set()
    stack: list[str] = [start_name]
    while stack:
        current_name: str = stack.pop()
        neighbor_name: str
        for neighbor_name in neighbors_by_name.get(current_name, ()):
            if neighbor_name in visited_names:
                continue
            visited_names.add(neighbor_name)
            stack.append(neighbor_name)
    return frozenset(visited_names)


def _model_names_by_pipeline(
    *, compiled_pipelines: tuple[CompiledPipeline, ...]
) -> dict[str, tuple[str, ...]]:
    names_by_pipeline: dict[str, tuple[str, ...]] = {}
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in compiled_pipelines:
        names_by_pipeline[compiled_pipeline.pipeline.name] = tuple(
            compiled_model.key.name for compiled_model in compiled_pipeline.models
        )
    return names_by_pipeline
