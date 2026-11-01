"""Build the stable identity of one compiled SQL audit."""

import sqltide._native as _native
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.compiler.quality.types import QualityNodeKind
from sqltide.compiler.sql_analysis.main._canonicalize_sql import canonicalize_sql


def build_audit_quality_identity(
    *, audit: LoadedSqlAudit, resolved_query: str, dialect: str
) -> QualityNodeIdentity:
    """Build target-neutral and target-resolved identity for one compiled audit."""

    node_name: str = audit.name or audit.file_path.stem
    binding_key, definition_fingerprint, execution_fingerprint = _native.quality_audit_identity(
        node_name,
        audit.attached_column,
        audit.attached_model,
        str(audit.attachment_kind),
        list(audit.referenced_model_names),
        audit.severity,
        canonicalize_sql(sql=audit.query, dialect=dialect),
        canonicalize_sql(sql=resolved_query, dialect=dialect),
    )
    return QualityNodeIdentity(
        node_kind=QualityNodeKind.AUDIT,
        node_name=node_name,
        binding_key=binding_key,
        definition_fingerprint=definition_fingerprint,
        execution_fingerprint=execution_fingerprint,
    )
