"""Central path contract for static and runtime target artifacts."""

from collections.abc import Callable
from pathlib import Path

import sqltide._native as _native
from sqltide.adapter.models import AdapterManagedSource, AdapterTable, AdapterView
from sqltide.cli.compile.exceptions import CompileArtifactError
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.pipeline.types import AdapterResource
from sqltide.compiler.testing.models import SqlTestCase


def model_query_path(*, pipeline_name: str, model_name: str) -> str:
    return _artifact_path(lambda: _native.artifact_model_query_path(pipeline_name, model_name))


def model_resource_path(*, pipeline_name: str, model_name: str, resource: AdapterResource) -> str:
    return _artifact_path(
        lambda: _native.artifact_model_resource_path(
            pipeline_name, model_name, resource_kind(resource)
        )
    )


def source_resource_path(*, source_name: str, resource_name: str) -> str:
    return _artifact_path(lambda: _native.artifact_source_resource_path(source_name, resource_name))


def audit_path(*, audit: LoadedSqlAudit, project_dir: Path) -> str:
    relative_file: str = _audit_relative_path(
        file_path=audit.file_path,
        project_dir=project_dir,
    ).as_posix()
    return _artifact_path(lambda: _native.artifact_audit_path(relative_file, audit.name))


def static_test_path(*, test_case: SqlTestCase) -> str:
    return _artifact_path(
        lambda: _native.artifact_test_path(
            test_case.name or test_case.file_path.stem,
            [target.target_model_name for target in test_case.target_cases],
        )
    )


def runtime_test_path(*, test_case: SqlTestCase) -> Path:
    return Path("run", *Path(static_test_path(test_case=test_case)).parts[1:])


def resource_kind(resource: AdapterResource) -> str:
    if isinstance(resource, AdapterManagedSource):
        return "managed_source"
    if isinstance(resource, AdapterTable):
        return "table"
    if isinstance(resource, AdapterView):
        return "view"
    return "materialized_view"


def _artifact_path(build: Callable[[], str]) -> str:
    try:
        return build()
    except ValueError as error:
        raise CompileArtifactError(str(error)) from None


def _audit_relative_path(*, file_path: Path, project_dir: Path) -> Path:
    resolved_file_path: Path = file_path.resolve()
    resolved_project_dir: Path = project_dir.resolve()
    audits_root: Path = resolved_project_dir / "audits"
    try:
        return resolved_file_path.relative_to(audits_root)
    except ValueError:
        try:
            return resolved_file_path.relative_to(resolved_project_dir)
        except ValueError as error:
            raise CompileArtifactError(
                f"Audit file '{file_path}' is outside project root '{project_dir}'"
            ) from error
