"""Helpers for generic SQL audit definitions and model-header instances."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.audit_discovery._helpers.parsing import (
    parse_generic_sql_audit_definition,
)
from sqltide.compiler.audit_discovery.constants import (
    AUDIT_COLUMN_ARGUMENT_KEY,
    AUDIT_MODEL_ARGUMENT_KEY,
)
from sqltide.compiler.audit_discovery.exceptions import SqlAuditParseError
from sqltide.compiler.audit_discovery.models import (
    LoadedGenericSqlAuditDefinition,
    LoadedGenericSqlAuditInstance,
    LoadedSqlAudit,
)
from sqltide.compiler.audit_discovery.types import (
    AuditAttachmentKind,
    NativeGenericAuditInstance,
)
from sqltide.compiler.compile.main._extract_refs import extract_refs
from sqltide.compiler.compile.models import ParsedRef
from sqltide.compiler.discovery.models import ModelColumnSpec, TransformStep, ViewStep
from sqltide.compiler.discovery.types import SqlRelationType
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from sqltide.compiler.sql_analysis.main._render_string_literal import render_string_literal

_LITERAL_RENDER_FAILURE: str = "literal"
_LITERAL_DIALECT: str = "clickhouse"


def discover_generic_sql_audit_definitions(
    *,
    root: Path,
    contents_by_path: Mapping[Path, str] | None = None,
    macro_registry: MacroRegistry | None = None,
    macro_context: MacroContext | None = None,
) -> tuple[LoadedGenericSqlAuditDefinition, ...]:
    """Load generic SQL audit definitions from `audits/generic/`."""

    if not root.exists():
        return ()
    definitions: list[LoadedGenericSqlAuditDefinition] = []
    file_path: Path
    for file_path in sorted(root.rglob("*.sql")):
        definitions.append(
            parse_generic_sql_audit_definition(
                file_path=file_path,
                contents=None if contents_by_path is None else contents_by_path[file_path],
                macro_registry=macro_registry,
                macro_context=macro_context,
            )
        )
    duplicate_names: tuple[str, ...] = _find_duplicate_names(
        tuple(definition.name for definition in definitions)
    )
    if duplicate_names:
        raise SqlAuditParseError(
            f"Duplicate generic SQL audit definitions found: {', '.join(duplicate_names)}"
        )
    return tuple(definitions)


def build_model_header_generic_sql_audit_instances(
    *,
    models: tuple[TransformStep | ViewStep, ...],
) -> tuple[LoadedGenericSqlAuditInstance, ...]:
    """Build generic audit instances declared in MODEL(...) headers."""

    instances: list[LoadedGenericSqlAuditInstance] = []
    model: TransformStep | ViewStep
    for model in models:
        file_path: Path = (
            model.source_file_path
            if model.source_file_path is not None
            else Path(f"{model.name}.sql")
        )
        instances.extend(
            _build_generic_sql_audit_instances(
                file_path=file_path,
                raw_audits=list(model.audits),
                implicit_arguments={"model": model.name},
                default_name_prefix=model.name,
            )
        )
        column: ModelColumnSpec
        for column in model.columns:
            instances.extend(
                _build_generic_sql_audit_instances(
                    file_path=file_path,
                    raw_audits=list(column.audits),
                    implicit_arguments={"model": model.name, "column": column.name},
                    default_name_prefix=f"{model.name}.{column.name}",
                )
            )
    return tuple(instances)


def render_generic_sql_audits(
    *,
    definitions: tuple[LoadedGenericSqlAuditDefinition, ...],
    instances: tuple[LoadedGenericSqlAuditInstance, ...],
) -> tuple[LoadedSqlAudit, ...]:
    """Render concrete audits with per-file sequential indices for distinct node identities."""

    definitions_by_name: dict[str, LoadedGenericSqlAuditDefinition] = {
        definition.name: definition for definition in definitions
    }
    rendered_audits: list[LoadedSqlAudit] = []
    audit_index_by_file: dict[Path, int] = {}
    instance: LoadedGenericSqlAuditInstance
    for instance in instances:
        definition: LoadedGenericSqlAuditDefinition | None = definitions_by_name.get(
            instance.definition_name
        )
        if definition is None:
            raise SqlAuditParseError(
                f"Generic SQL audit instance '{instance.file_path}' references unknown definition "
                f"'{instance.definition_name}'"
            )
        rendered_query: str = _render_generic_sql_audit_query(
            definition=definition,
            arguments=instance.arguments,
            file_path=instance.file_path,
        )
        parsed_refs: tuple[ParsedRef, ...] = tuple(
            extract_refs(sql=rendered_query, source_path=instance.file_path)
        )
        if not parsed_refs:
            raise SqlAuditParseError(
                f"Rendered generic SQL audit '{instance.name}' must reference at least one model"
            )
        parsed_ref: ParsedRef
        for parsed_ref in parsed_refs:
            if parsed_ref.relation_type != SqlRelationType.REF:
                raise SqlAuditParseError(
                    f"Rendered generic SQL audit '{instance.name}' may only use __ref(...)"
                )
        audit_index_by_file[instance.file_path] = audit_index_by_file.get(instance.file_path, 0) + 1
        rendered_audits.append(
            LoadedSqlAudit(
                file_path=instance.file_path,
                query=rendered_query,
                referenced_model_names=tuple(dict.fromkeys(ref.name for ref in parsed_refs)),
                severity=instance.severity,
                description=instance.description,
                name=instance.name,
                audit_index=audit_index_by_file[instance.file_path],
                generic_definition_name=instance.definition_name,
                attachment_kind=(
                    AuditAttachmentKind.COLUMN
                    if AUDIT_COLUMN_ARGUMENT_KEY in instance.arguments
                    else AuditAttachmentKind.MODEL
                ),
                attached_model=str(instance.arguments[AUDIT_MODEL_ARGUMENT_KEY]),
                attached_column=(
                    str(instance.arguments[AUDIT_COLUMN_ARGUMENT_KEY])
                    if AUDIT_COLUMN_ARGUMENT_KEY in instance.arguments
                    else None
                ),
                severity_is_explicit=instance.severity_is_explicit,
                cadence_seconds_override=instance.cadence_seconds_override,
                warmup_seconds_override=instance.warmup_seconds_override,
                scheduled_override=instance.scheduled_override,
            )
        )
    return tuple(rendered_audits)


def _build_generic_sql_audit_instances(
    *,
    file_path: Path,
    raw_audits: object,
    implicit_arguments: dict[str, object],
    default_name_prefix: str,
) -> list[LoadedGenericSqlAuditInstance]:
    try:
        instances: list[NativeGenericAuditInstance] = _native.generic_audit_instances(
            str(file_path),
            raw_audits,
            list(implicit_arguments.items()),
            default_name_prefix,
        )
    except ValueError as error:
        raise SqlAuditParseError(str(error)) from None
    return [
        LoadedGenericSqlAuditInstance(
            file_path=file_path,
            definition_name=definition_name,
            arguments=dict(arguments),
            name=name,
            severity=severity,
            description=description,
            severity_is_explicit=severity_is_explicit,
            cadence_seconds_override=cadence_seconds,
            warmup_seconds_override=warmup_seconds,
            scheduled_override=scheduled,
        )
        for (
            definition_name,
            arguments,
            name,
            severity,
            description,
            severity_is_explicit,
            cadence_seconds,
            warmup_seconds,
            scheduled,
        ) in instances
    ]


def _render_generic_sql_audit_query(
    *,
    definition: LoadedGenericSqlAuditDefinition,
    arguments: dict[str, object],
    file_path: Path,
) -> str:
    try:
        return _native.render_generic_audit(
            str(file_path),
            definition.name,
            definition.query,
            list(definition.raw_parameter_names),
            list(definition.quoted_parameter_names),
            list(arguments.items()),
        )
    except ValueError as error:
        reason, detail = error.args
        if reason == _LITERAL_RENDER_FAILURE:
            render_string_literal(value=detail, dialect=_LITERAL_DIALECT)
        raise SqlAuditParseError(detail) from None


def _find_duplicate_names(names: tuple[str, ...]) -> tuple[str, ...]:
    seen_names: set[str] = set()
    duplicate_names: list[str] = []
    name: str
    for name in names:
        if name in seen_names and name not in duplicate_names:
            duplicate_names.append(name)
        seen_names.add(name)
    return tuple(duplicate_names)
