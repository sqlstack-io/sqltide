"""Apache-2.0: SQLBuild compiler/compile/main/_assemble_project.py@7e3b2f854f05."""

from dataclasses import replace

from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile._helpers.audit_policy import resolve_audit_policies
from sqltide.compiler.compile._helpers.naming import validate_compiled_project_relation_names
from sqltide.compiler.compile._helpers.replay_policies import (
    resolve_source_replay_lineage_mode,
)
from sqltide.compiler.compile.main._compile_pipeline import compile_pipeline
from sqltide.compiler.compile.main.replace_refs import replace_refs
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledProject,
    CompiledSource,
    CompileProjectInputs,
    LogicalResourceKey,
)
from sqltide.compiler.compile.types import LogicalResourceType
from sqltide.compiler.discovery.models import AuditDefaults, LoadedProject
from sqltide.compiler.quality.main._build_audit_quality_identity import (
    build_audit_quality_identity,
)
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.compiler.testing.main._build_sql_test_cases import build_sql_test_cases
from sqltide.compiler.testing.models import SqlTestCase


def assemble_project(
    *,
    inputs: CompileProjectInputs,
    reference_rewriter: SqlReferenceRewriter,
    sql_analyzer: SqlModelAnalyzer,
) -> CompiledProject:
    """Compile and validate every attached project resource exactly once."""

    pipelines: tuple[CompiledPipeline, ...] = tuple(
        compile_pipeline(loaded_pipeline=loaded_pipeline, sql_analyzer=sql_analyzer)
        for loaded_pipeline in inputs.pipelines
    )
    sources_by_name: dict[str, CompiledSource] = {
        source.name: CompiledSource(
            key=LogicalResourceKey(
                resource_type=LogicalResourceType.SOURCE,
                name=source.name,
            ),
            source=source,
            effective_replay_lineage_mode=resolve_source_replay_lineage_mode(source=source),
        )
        for source in inputs.sources
    }
    models: list[CompiledModel] = []
    pipeline: CompiledPipeline
    for pipeline in pipelines:
        if pipeline.source is not None:
            sources_by_name.setdefault(pipeline.source.key.name, pipeline.source)
        models.extend(pipeline.models)
    test_cases: tuple[SqlTestCase, ...] = build_sql_test_cases(
        loaded_tests=inputs.tests,
        compiled_pipelines=pipelines,
        reference_rewriter=reference_rewriter,
        comparison_renderer=inputs.adapter_profile.render_set_difference_comparison,
        dialect=inputs.adapter_profile.sql_analysis_dialect,
    )
    audits: tuple[LoadedSqlAudit, ...] = _compiled_audits(
        audits=resolve_audit_policies(
            audits=inputs.audits,
            compiled_pipelines=pipelines,
            project_defaults=_project_audit_defaults(inputs),
        ),
        models=tuple(models),
        database=inputs.effective_target.default_database,
        reference_rewriter=reference_rewriter,
        dialect=inputs.adapter_profile.sql_analysis_dialect,
    )
    project: CompiledProject = CompiledProject(
        sources=tuple(sources_by_name.values()),
        models=tuple(models),
        pipelines=pipelines,
        tests=inputs.tests,
        test_cases=test_cases,
        audits=audits,
        macro_registry=inputs.macro_registry,
        macro_context=inputs.macro_context,
        project_name=inputs.project_name,
        target_name=inputs.target_name,
        production_target=inputs.effective_target.production_target,
        destruction_relation_drop_size_limit=(
            inputs.effective_target.destruction_relation_drop_size_limit
        ),
        model_reference_scope=inputs.model_reference_scope,
        allowed_cross_pipeline_references=inputs.allowed_cross_pipeline_references,
    )
    validate_compiled_project_relation_names(project=project)
    return project


def _compiled_audits(
    *,
    audits: tuple[LoadedSqlAudit, ...],
    models: tuple[CompiledModel, ...],
    database: str | None,
    reference_rewriter: SqlReferenceRewriter,
    dialect: str,
) -> tuple[LoadedSqlAudit, ...]:
    resolver: dict[str, str] = {
        model.key.name: (
            model.relation_name if database is None else f"{database}.{model.relation_name}"
        )
        for model in models
    }
    return tuple(
        replace(
            audit,
            quality_identity=build_audit_quality_identity(
                audit=audit,
                resolved_query=replace_refs(
                    sql=audit.query,
                    resolver=resolver,
                    rewriter=reference_rewriter,
                ),
                dialect=dialect,
            ),
        )
        for audit in audits
    )


def _project_audit_defaults(inputs: CompileProjectInputs) -> AuditDefaults:
    loaded_project: LoadedProject | None = inputs.discovered_inputs.loaded_project
    return AuditDefaults() if loaded_project is None else loaded_project.project.audit_defaults
