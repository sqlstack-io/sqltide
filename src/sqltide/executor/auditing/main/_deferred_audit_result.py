"""Build an explicit deferred audit result."""

from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.executor.auditing._helpers.warmup import deferred_audit_result_impl
from sqltide.executor.auditing.models import AuditWarmupState, SqlAuditResult


def deferred_audit_result(*, audit: LoadedSqlAudit, state: AuditWarmupState) -> SqlAuditResult:
    """Represent a warming audit without fabricating a pass or failure."""

    return deferred_audit_result_impl(audit=audit, state=state)
