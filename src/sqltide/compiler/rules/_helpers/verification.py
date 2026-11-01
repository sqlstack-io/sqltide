"""Compiler proof for the existing conservative SQLBuild Rule-fix allowlist."""

from collections import Counter
from pathlib import Path

from sqltide.compiler.compile.models import CompiledModel, CompiledTableModel, CompiledViewModel
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.sql_analysis.main._canonicalize_sql import canonicalize_sql


def verify_fix(*, before: CompileAnalysis, after: CompileAnalysis, edited: Path) -> None:
    """Reject changed columns, dependencies, replay lineage or other owners' diagnostics."""

    previous: dict[object, CompiledModel] = {
        model.key: model for model in before.compiled_project.models
    }
    current: dict[object, CompiledModel] = {
        model.key: model for model in after.compiled_project.models
    }
    if (
        previous.keys() != current.keys()
        or before.graph.upstream_edges_by_key != after.graph.upstream_edges_by_key
    ):
        raise SqlRulesError("Rule fix changed project models or dependencies")
    for key, model in previous.items():
        other: CompiledModel = current[key]
        if model.output_columns != other.output_columns or model.kind != other.kind:
            raise SqlRulesError("Rule fix changed output names, types or nullability")
        if model.sql_analysis.storage_expressions != other.sql_analysis.storage_expressions or (
            model.sql_analysis.aggregate_facts.has_semantics
            != other.sql_analysis.aggregate_facts.has_semantics
        ):
            raise SqlRulesError("Rule fix changed storage or aggregation semantics")
        if isinstance(model, CompiledTableModel) and isinstance(other, CompiledTableModel):
            if (
                model.preserves_required_lineage != other.preserves_required_lineage
                or model.effective_bounded_replay_fallback
                != other.effective_bounded_replay_fallback
                or model.replay_anchor_eligible != other.replay_anchor_eligible
            ):
                raise SqlRulesError("Rule fix changed replay lineage")
        owner: Path | None = (
            model.transform.source_file_path
            if isinstance(model, CompiledTableModel)
            else model.view.source_file_path
            if isinstance(model, CompiledViewModel)
            else None
        )
        if owner != edited:
            dialect: str = before.adapter_profile.sql_analysis_dialect
            old_sql: str = canonicalize_sql(sql=model.query, dialect=dialect)
            new_sql: str = canonicalize_sql(sql=other.query, dialect=dialect)
            if old_sql != new_sql:
                raise SqlRulesError("Rule fix changed another model's SQL")
    previous_diagnostics: Counter[tuple[Path | None, str, str]] = Counter(
        (item.location.path if item.location else None, item.code, item.message)
        for item in before.diagnostics
    )
    current_diagnostics: Counter[tuple[Path | None, str, str]] = Counter(
        (item.location.path if item.location else None, item.code, item.message)
        for item in after.diagnostics
    )
    if current_diagnostics - previous_diagnostics or any(
        count != current_diagnostics[key]
        for key, count in previous_diagnostics.items()
        if key[0] != edited
    ):
        raise SqlRulesError("Rule fix introduced diagnostics or changed another file's diagnostics")
