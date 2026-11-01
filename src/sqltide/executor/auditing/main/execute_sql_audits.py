"""Entry point for executing SQL audits against ClickHouse."""

from __future__ import annotations

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterQueryResult
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.main.replace_refs import replace_refs
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.executor.auditing.constants import AUDIT_SAMPLE_LIMIT
from sqltide.executor.auditing.exceptions import AuditExecutionError
from sqltide.executor.auditing.models import SqlAuditResult, SqlAuditRunResult


def execute_sql_audits(
    *,
    loaded_audits: tuple[LoadedSqlAudit, ...],
    resolver: Mapping[str, str],
    client: AdapterConnection,
    dialect: str,
) -> SqlAuditRunResult:
    """Execute discovered SQL audits with resolved model refs."""

    reference_rewriter: SqlReferenceRewriter = SqlReferenceRewriter(dialect=dialect)
    return SqlAuditRunResult(
        audit_results=tuple(
            _execute_single_sql_audit(
                loaded_audit=loaded_audit,
                resolver=resolver,
                client=client,
                reference_rewriter=reference_rewriter,
            )
            for loaded_audit in loaded_audits
        )
    )


def _execute_single_sql_audit(
    *,
    loaded_audit: LoadedSqlAudit,
    resolver: Mapping[str, str],
    client: AdapterConnection,
    reference_rewriter: SqlReferenceRewriter,
) -> SqlAuditResult:
    resolved_query: str = replace_refs(
        sql=loaded_audit.query,
        resolver=dict(resolver),
        rewriter=reference_rewriter,
    )
    failing_row_count = 0
    sample_column_names: tuple[str, ...] = ()
    sample_rows: tuple[tuple[object, ...], ...] = ()
    try:
        failing_row_count = _query_failing_row_count(query=resolved_query, client=client)
        if failing_row_count:
            sample_result: AdapterQueryResult = client.query(
                _native.render_clickhouse_audit_sample_query(resolved_query, AUDIT_SAMPLE_LIMIT)
            )
            sample_column_names = sample_result.column_names
            sample_rows = sample_result.rows
    except (AdapterError, AuditExecutionError) as error:
        return SqlAuditResult(
            file_path=loaded_audit.file_path,
            referenced_model_names=loaded_audit.referenced_model_names,
            severity=loaded_audit.severity,
            passed=False,
            failing_row_count=max(failing_row_count, 1),
            sample_column_names=sample_column_names,
            sample_rows=sample_rows,
            description=loaded_audit.description,
            name=loaded_audit.name,
            error_message=str(error),
        )
    return SqlAuditResult(
        file_path=loaded_audit.file_path,
        referenced_model_names=loaded_audit.referenced_model_names,
        severity=loaded_audit.severity,
        passed=failing_row_count == 0,
        failing_row_count=failing_row_count,
        sample_column_names=sample_column_names,
        sample_rows=sample_rows,
        description=loaded_audit.description,
        name=loaded_audit.name,
        error_message=None,
    )


def _query_failing_row_count(*, query: str, client: AdapterConnection) -> int:
    result: AdapterQueryResult = client.query(_native.render_clickhouse_audit_count_query(query))
    if not result.rows:
        raise AuditExecutionError("Expected a count row from SQL audit execution")
    count_value: object = result.rows[0][0]
    if not isinstance(count_value, (int, float, str)):
        raise AuditExecutionError("SQL audit count query returned a non-numeric row count")
    try:
        return int(count_value)
    except (OverflowError, ValueError) as error:
        raise AuditExecutionError(
            "SQL audit count query returned a non-numeric row count"
        ) from error
