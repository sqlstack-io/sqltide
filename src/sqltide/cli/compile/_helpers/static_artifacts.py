"""Build one complete deterministic static compile target in memory."""

from sqltide.adapter.models import AdapterManagedSource
from sqltide.cli.compile._helpers.content import (
    normalized_sql,
    static_test_sql,
)
from sqltide.cli.compile._helpers.manifest import build_manifest_json
from sqltide.cli.compile._helpers.paths import (
    audit_path,
    model_query_path,
    model_resource_path,
    source_resource_path,
    static_test_path,
)
from sqltide.cli.compile._helpers.redaction import redacted_managed_source
from sqltide.cli.compile.exceptions import CompileArtifactError
from sqltide.cli.compile.models import StaticArtifactFile, StaticCompileArtifacts
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledSource,
    LogicalResourceKey,
)
from sqltide.compiler.dag.main.build_dag_json import build_dag_json
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.pipeline.types import AdapterResource
from sqltide.compiler.testing.models import SqlTestCase


def build_static_compile_artifacts(*, analysis: CompileAnalysis) -> StaticCompileArtifacts:
    """Build all static files before any target path is changed."""

    rendered_by_key: dict[LogicalResourceKey, tuple[tuple[AdapterResource, str], ...]] = (
        _rendered_resources_by_logical_key(analysis=analysis)
    )
    compiled_files: list[StaticArtifactFile] = []
    compiled_files.extend(
        _source_resource_files(analysis=analysis, rendered_by_key=rendered_by_key)
    )
    compiled_files.extend(_model_files(analysis=analysis, rendered_by_key=rendered_by_key))
    compiled_files.extend(_audit_and_test_files(analysis=analysis))
    ordered_files: tuple[StaticArtifactFile, ...] = tuple(
        sorted(compiled_files, key=lambda file: file.relative_path)
    )
    _validate_unique_paths(files=ordered_files)
    return StaticCompileArtifacts(
        compiled_files=ordered_files,
        manifest_json=build_manifest_json(analysis=analysis, compiled_files=ordered_files),
        dag_json=build_dag_json(
            graph=analysis.graph,
            macro_registry=analysis.compile_inputs.macro_registry,
        ),
    )


def _rendered_resources_by_logical_key(
    *, analysis: CompileAnalysis
) -> dict[LogicalResourceKey, tuple[tuple[AdapterResource, str], ...]]:
    database_by_key: dict[LogicalResourceKey, str] = {}
    pipeline: CompiledPipeline
    for pipeline in analysis.compiled_project.pipelines:
        database: str = _pipeline_database(analysis=analysis, pipeline=pipeline)
        if pipeline.source is not None:
            database_by_key.setdefault(pipeline.source.key, database)
        model: CompiledModel
        for model in pipeline.models:
            database_by_key[model.key] = database
    rendered_by_key: dict[LogicalResourceKey, tuple[tuple[AdapterResource, str], ...]] = {}
    logical_key: LogicalResourceKey
    resources: tuple[AdapterResource, ...]
    for logical_key, resources in analysis.realized_project.resources_by_logical_key.items():
        database = database_by_key[logical_key]
        rendered_by_key[logical_key] = tuple(
            (
                resource,
                normalized_sql(
                    analysis.adapter_profile.render_resource(
                        resource=(
                            redacted_managed_source(resource)
                            if isinstance(resource, AdapterManagedSource)
                            else resource
                        ),
                        database=database,
                    )
                ),
            )
            for resource in resources
        )
    return rendered_by_key


def _source_resource_files(
    *,
    analysis: CompileAnalysis,
    rendered_by_key: dict[LogicalResourceKey, tuple[tuple[AdapterResource, str], ...]],
) -> list[StaticArtifactFile]:
    files: list[StaticArtifactFile] = []
    source: CompiledSource
    for source in analysis.compiled_project.sources:
        resource: AdapterResource
        sql: str
        for resource, sql in rendered_by_key[source.key]:
            files.append(
                StaticArtifactFile(
                    relative_path=source_resource_path(
                        source_name=source.key.name,
                        resource_name=resource.name,
                    ),
                    contents=sql,
                )
            )
    return files


def _model_files(
    *,
    analysis: CompileAnalysis,
    rendered_by_key: dict[LogicalResourceKey, tuple[tuple[AdapterResource, str], ...]],
) -> list[StaticArtifactFile]:
    files: list[StaticArtifactFile] = []
    model: CompiledModel
    for model in analysis.compiled_project.models:
        files.append(
            StaticArtifactFile(
                relative_path=model_query_path(
                    pipeline_name=model.pipeline_name,
                    model_name=model.key.name,
                ),
                contents=normalized_sql(
                    analysis.realized_project.resolved_query_by_model_key[model.key]
                ),
            )
        )
        resource: AdapterResource
        sql: str
        for resource, sql in rendered_by_key[model.key]:
            files.append(
                StaticArtifactFile(
                    relative_path=model_resource_path(
                        pipeline_name=model.pipeline_name,
                        model_name=model.key.name,
                        resource=resource,
                    ),
                    contents=sql,
                )
            )
    return files


def _audit_and_test_files(*, analysis: CompileAnalysis) -> list[StaticArtifactFile]:
    files: list[StaticArtifactFile] = []
    audit: LoadedSqlAudit
    for audit in analysis.compiled_project.audits:
        files.append(
            StaticArtifactFile(
                relative_path=audit_path(
                    audit=audit,
                    project_dir=analysis.discovered_inputs.project_dir,
                ),
                contents=normalized_sql(audit.query),
            )
        )
    test_case: SqlTestCase
    for test_case in analysis.compiled_project.test_cases:
        files.append(
            StaticArtifactFile(
                relative_path=static_test_path(test_case=test_case),
                contents=static_test_sql(test_case=test_case),
            )
        )
    return files


def _pipeline_database(*, analysis: CompileAnalysis, pipeline: CompiledPipeline) -> str:
    if pipeline.project is not None and pipeline.project.default_database is not None:
        return pipeline.project.default_database
    return analysis.compile_inputs.effective_target.default_database or "default"


def _validate_unique_paths(*, files: tuple[StaticArtifactFile, ...]) -> None:
    seen_paths: set[str] = set()
    file: StaticArtifactFile
    for file in files:
        if file.relative_path in seen_paths:
            raise CompileArtifactError(
                f"Multiple compile artifacts resolve to '{file.relative_path}'"
            )
        seen_paths.add(file.relative_path)
