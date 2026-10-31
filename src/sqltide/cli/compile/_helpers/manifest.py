"""SQLTide-native deterministic manifest construction."""

from pathlib import Path
from typing import cast

import sqltide._native as _native
from sqltide.adapter.models import AdapterManagedSource, AdapterTable, AdapterView
from sqltide.cli.compile._helpers.paths import (
    audit_path,
    model_ordinary_view_path,
    model_query_path,
    model_table_path,
    model_view_path,
    source_resource_path,
    static_test_path,
)
from sqltide.cli.compile.models import StaticArtifactFile
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.models import (
    CompiledModel,
    CompiledPipeline,
    CompiledSource,
    CompiledTableModel,
    CompiledViewModel,
    LogicalResourceKey,
)
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    KafkaRetentionPolicy,
    ModelRetentionPolicy,
)
from sqltide.compiler.discovery.types import KafkaRetentionOrigin
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.pipeline.types import AdapterResource
from sqltide.compiler.testing.models import SqlTestCase


def build_manifest_json(
    *, analysis: CompileAnalysis, compiled_files: tuple[StaticArtifactFile, ...]
) -> str:
    """Build the complete deterministic SQLTide-native manifest."""

    payload: dict[str, object] = {
        "artifacts": (
            "manifest.json",
            "sqltide_dag.json",
            *(file.relative_path.as_posix() for file in compiled_files),
        ),
        "audits": {
            _audit_identity(audit): _audit_entry(audit=audit, analysis=analysis)
            for audit in analysis.compiled_project.audits
        },
        "metadata": {"manifest_version": 1, "tool": "sqltide"},
        "dependencies": {
            "model_reference_scope": str(analysis.compiled_project.model_reference_scope),
            "allowed_cross_pipeline_references": tuple(
                {
                    "upstream_pipeline": reference.upstream_pipeline,
                    "downstream_pipeline": reference.downstream_pipeline,
                }
                for reference in analysis.compiled_project.allowed_cross_pipeline_references
            ),
        },
        "macros": {
            name: {
                "file": macro.relative_path.as_posix(),
                "source": macro.source,
            }
            for name, macro in analysis.compile_inputs.macro_registry.macros.items()
        },
        "models": {
            model.key.name: _model_entry(model=model, analysis=analysis)
            for model in analysis.compiled_project.models
        },
        "pipelines": {
            pipeline.pipeline.name: _pipeline_entry(pipeline=pipeline, analysis=analysis)
            for pipeline in analysis.compiled_project.pipelines
        },
        "sources": {
            source.key.name: _source_entry(source=source, analysis=analysis)
            for source in analysis.compiled_project.sources
        },
        "tests": {
            _test_identity(test_case): _test_entry(test_case=test_case, analysis=analysis)
            for test_case in analysis.compiled_project.test_cases
        },
    }
    return _native.json_dumps(payload, 2, True) + "\n"


def _pipeline_entry(*, pipeline: CompiledPipeline, analysis: CompileAnalysis) -> dict[str, object]:
    source_keys: tuple[LogicalResourceKey, ...] = (
        () if pipeline.source is None else (pipeline.source.key,)
    )
    logical_keys: tuple[LogicalResourceKey, ...] = (
        *source_keys,
        *(model.key for model in pipeline.models),
    )
    return {
        "file": _project_relative_path(path=pipeline.file_path, analysis=analysis),
        "models": tuple(model.key.name for model in pipeline.models),
        "name": pipeline.pipeline.name,
        "replay_lineage_mode": pipeline.effective_replay_lineage_mode,
        "relations": {
            key.name: analysis.realized_project.relation_name_by_logical_key[key]
            for key in logical_keys
        },
        "resolved_database": _pipeline_database(pipeline=pipeline, analysis=analysis),
        "source_name": None if pipeline.source is None else pipeline.source.key.name,
    }


def _source_entry(*, source: CompiledSource, analysis: CompileAnalysis) -> dict[str, object]:
    naming_macro: str | None = getattr(source.source, "naming_macro", None)
    resources: tuple[AdapterResource, ...] = analysis.realized_project.resources_by_logical_key[
        source.key
    ]
    landing_table: AdapterTable | None = next(
        (resource for resource in resources if isinstance(resource, AdapterTable)),
        None,
    )
    return {
        "logical_key": f"source:{source.key.name}",
        "name": source.key.name,
        "name_origin": {
            "kind": source.source.name_origin,
            "macro": naming_macro,
            "macro_fingerprint": getattr(source.source, "naming_macro_fingerprint", None),
        },
        "relation_name": analysis.realized_project.relation_name_by_logical_key[source.key],
        "replay_lineage_mode": source.effective_replay_lineage_mode,
        "retention": _source_retention_entry(source),
        "ttl": None if landing_table is None else landing_table.ttl,
        "resources": tuple(
            {
                "kind": _resource_kind(resource),
                "name": resource.name,
                "path": source_resource_path(
                    source_name=source.key.name,
                    resource_name=resource.name,
                ).as_posix(),
            }
            for resource in resources
        ),
    }


def _source_retention_entry(source: CompiledSource) -> dict[str, object] | None:
    if not isinstance(source.source, KafkaLandingStep):
        return None
    value: KafkaRetentionPolicy | bool | None = source.source.kafka.retention
    origin: KafkaRetentionOrigin | str | None = source.source.kafka.retention_origin
    if origin is None:
        return None
    if value is False:
        return {"origin": str(origin), "status": "disabled"}
    if not isinstance(value, KafkaRetentionPolicy):
        return None
    return {
        "origin": str(origin),
        "status": "applied",
        "duration_seconds": value.duration_seconds,
        "timestamp": str(value.timestamp),
        "fallback": str(value.fallback),
        "cap_at": None if value.cap_at is None else str(value.cap_at),
    }


def _model_entry(*, model: CompiledModel, analysis: CompileAnalysis) -> dict[str, object]:
    resources: tuple[AdapterResource, ...] = analysis.realized_project.resources_by_logical_key[
        model.key
    ]
    entry: dict[str, object] = {
        "logical_key": f"model:{model.key.name}",
        "name": model.key.name,
        "pipeline": model.pipeline_name,
        "query_path": model_query_path(
            pipeline_name=model.pipeline_name, model_name=model.key.name
        ).as_posix(),
        "refs": tuple(model.refs),
        "relation_name": analysis.realized_project.relation_name_by_logical_key[model.key],
        "resources": tuple(
            {
                "kind": _resource_kind(resource),
                "name": resource.name,
                "path": _model_resource_path(model=model, resource=resource).as_posix(),
            }
            for resource in resources
        ),
    }
    if isinstance(model, CompiledViewModel):
        entry.update({"source": None, "spec": None})
        return entry
    table_model: CompiledTableModel = cast(CompiledTableModel, model)
    table: AdapterTable = next(
        resource for resource in resources if isinstance(resource, AdapterTable)
    )
    entry.update(
        {
            "source": table_model.transform.source,
            "retention": _model_retention_entry(table_model),
            "spec": {
                "columns": tuple(
                    {
                        "default": column.default_expression,
                        "name": column.name,
                        "type": column.type,
                    }
                    for column in table.columns
                ),
                "engine": table.engine,
                "order_by": tuple(table.order_by),
                "partition_by": table.partition_by,
                "settings": None if not table.settings else dict(table.settings),
                "ttl": table.ttl,
            },
        }
    )
    return entry


def _model_retention_entry(model: CompiledTableModel) -> dict[str, object] | None:
    value: ModelRetentionPolicy | bool | None = model.retention.value
    if model.retention.origin is None:
        return None
    if value is False:
        return {"origin": str(model.retention.origin), "status": "disabled"}
    if not isinstance(value, ModelRetentionPolicy):
        return None
    return {
        "origin": str(model.retention.origin),
        "status": "applied" if model.retention_applied else "skipped",
        "duration_seconds": value.duration_seconds,
        "timestamp_column": value.timestamp_column,
        "cap_at_column": value.cap_at_column,
        "when_missing": str(value.when_missing),
    }


def _audit_entry(*, audit: LoadedSqlAudit, analysis: CompileAnalysis) -> dict[str, object]:
    return {
        "file": _project_relative_path(path=audit.file_path, analysis=analysis),
        "name": _audit_identity(audit),
        "path": audit_path(
            audit=audit,
            project_dir=analysis.discovered_inputs.project_dir,
        ).as_posix(),
        "referenced_models": tuple(audit.referenced_model_names),
        "severity": audit.severity,
    }


def _test_entry(*, test_case: SqlTestCase, analysis: CompileAnalysis) -> dict[str, object]:
    return {
        "file": _project_relative_path(path=test_case.file_path, analysis=analysis),
        "name": _test_identity(test_case),
        "path": static_test_path(test_case=test_case).as_posix(),
        "targets": tuple(target.target_model_name for target in test_case.target_cases),
    }


def _model_resource_path(*, model: CompiledModel, resource: AdapterResource) -> Path:
    if isinstance(resource, AdapterTable):
        return model_table_path(pipeline_name=model.pipeline_name, model_name=model.key.name)
    if isinstance(resource, AdapterView):
        return model_ordinary_view_path(
            pipeline_name=model.pipeline_name, model_name=model.key.name
        )
    return model_view_path(pipeline_name=model.pipeline_name, model_name=model.key.name)


def _resource_kind(resource: AdapterResource) -> str:
    if isinstance(resource, AdapterManagedSource):
        return "managed_source"
    if isinstance(resource, AdapterTable):
        return "table"
    if isinstance(resource, AdapterView):
        return "view"
    return "materialized_view"


def _project_relative_path(*, path: Path, analysis: CompileAnalysis) -> str:
    try:
        return (
            path.resolve().relative_to(analysis.discovered_inputs.project_dir.resolve()).as_posix()
        )
    except ValueError:
        return path.as_posix()


def _pipeline_database(*, pipeline: CompiledPipeline, analysis: CompileAnalysis) -> str:
    if pipeline.project is not None and pipeline.project.default_database is not None:
        return pipeline.project.default_database
    return analysis.compile_inputs.effective_target.default_database or "default"


def _audit_identity(audit: LoadedSqlAudit) -> str:
    return audit.name or audit.file_path.stem


def _test_identity(test_case: SqlTestCase) -> str:
    return test_case.name or test_case.file_path.stem
