"""Validate SQL audit targets and resolve effective audit policy over the native rules."""

from dataclasses import replace

import sqltide._native as _native
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.audit_discovery.types import AuditAttachmentKind
from sqltide.compiler.compile.exceptions import AuditCompileError
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.models import AuditDefaults


def resolve_audit_policies(
    *,
    audits: tuple[LoadedSqlAudit, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    project_defaults: AuditDefaults,
) -> tuple[LoadedSqlAudit, ...]:
    """Validate every audit's targets, then apply deterministic policy precedence."""

    try:
        policies: list[tuple[str, int | None, int, bool]] = _native.resolve_audit_policies(
            [
                (
                    str(audit.file_path),
                    audit.name or audit.file_path.stem,
                    audit.name is not None,
                    audit.attachment_kind == AuditAttachmentKind.STANDALONE,
                    list(audit.referenced_model_names),
                    audit.severity,
                    audit.severity_is_explicit,
                    audit.cadence_seconds_override,
                    audit.warmup_seconds_override,
                    audit.scheduled_override,
                )
                for audit in audits
            ],
            [_pipeline_scope(pipeline) for pipeline in compiled_pipelines],
            _defaults(project_defaults),
        )
    except ValueError as error:
        raise AuditCompileError(str(error)) from None
    return tuple(
        replace(
            audit,
            severity=severity,
            cadence_seconds=cadence_seconds,
            warmup_seconds=warmup_seconds,
            scheduled=scheduled,
        )
        for audit, (severity, cadence_seconds, warmup_seconds, scheduled) in zip(
            audits, policies, strict=True
        )
    )


def _defaults(defaults: AuditDefaults) -> tuple[str | None, int | None, int | None]:
    return (defaults.severity, defaults.cadence_seconds, defaults.warmup_seconds)


def _pipeline_scope(
    pipeline: CompiledPipeline,
) -> tuple[tuple[str | None, int | None, int | None], list[str]]:
    model_names: list[str] = [model.key.name for model in pipeline.models]
    return (_defaults(pipeline.pipeline.audit_defaults), model_names)
