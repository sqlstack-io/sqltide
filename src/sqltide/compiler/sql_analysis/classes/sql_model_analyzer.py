"""Invocation-scoped model SQL analysis and canonical generation."""

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.adapter.constants import ADAPTER_DATABASE_PLACEHOLDER
from sqltide.compiler.sql_analysis._helpers.model_analysis import analyze_model_sql_impl
from sqltide.compiler.sql_analysis._helpers.polyglot import native_references, sql_tree_error
from sqltide.compiler.sql_analysis._helpers.storage import analyze_storage_expressions
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import (
    SqlModelAnalysis,
    SqlOutputColumn,
    SqlResolvedQuery,
    SqlStorageExpression,
)


class SqlModelAnalyzer:
    """Own private parsed model state for one complete compiler invocation."""

    def __init__(self, *, dialect: str) -> None:
        self._dialect: str = dialect
        self._analyzer: _native.SqlTreeAnalyzer = _native.SqlTreeAnalyzer(dialect)
        self._tree_by_analysis: dict[SqlModelAnalysis, _native.SqlModelTree] = {}

    def analyze(
        self,
        *,
        sql: str,
        engine: str,
        order_by: tuple[str, ...],
        partition_by: str | None,
        ttl: str | None,
    ) -> SqlModelAnalysis:
        """Analyze one authored model and retain only private rewrite state."""

        analysis: SqlModelAnalysis
        tree: _native.SqlModelTree
        analysis, tree = analyze_model_sql_impl(
            sql=sql,
            engine=engine,
            order_by=order_by,
            partition_by=partition_by,
            ttl=ttl,
            analyzer=self._analyzer,
        )
        self._tree_by_analysis[analysis] = tree
        return analysis

    def resolve(
        self,
        *,
        analysis: SqlModelAnalysis,
        resolver: Mapping[str, str],
    ) -> SqlResolvedQuery:
        """Resolve and qualify a previously analyzed model without reparsing it."""

        try:
            canonical_sql, database_template = self._analyzer.resolve_model(
                self._tree(analysis),
                analysis.authored_sql,
                native_references(analysis.references),
                resolver,
                ADAPTER_DATABASE_PLACEHOLDER,
            )
        except _native.SqlTreeError as error:
            raise sql_tree_error(error) from None
        return SqlResolvedQuery(canonical_sql=canonical_sql, database_template=database_template)

    def uses_prewhere(self, *, analysis: SqlModelAnalysis) -> bool:
        """Return whether an analyzed query contains a PREWHERE clause."""

        return self._tree(analysis).uses_prewhere()

    def canonicalize_query(self, *, sql: str) -> SqlResolvedQuery:
        """Generate canonical and adapter-template forms of a generated query."""

        try:
            canonical_sql, database_template = self._analyzer.canonicalize_query(
                sql, ADAPTER_DATABASE_PLACEHOLDER
            )
        except _native.SqlTreeError as error:
            raise sql_tree_error(error) from None
        return SqlResolvedQuery(canonical_sql=canonical_sql, database_template=database_template)

    def analyze_storage(
        self,
        *,
        order_by: tuple[str, ...],
        partition_by: str | None,
        ttl: str | None,
        output_columns: tuple[SqlOutputColumn, ...],
    ) -> tuple[SqlStorageExpression, ...]:
        """Analyze standalone storage clauses through this invocation's dialect."""

        return analyze_storage_expressions(
            order_by=order_by,
            partition_by=partition_by,
            ttl=ttl,
            output_columns=output_columns,
            analyzer=self._analyzer,
        )

    def is_row_level(self, *, analysis: SqlModelAnalysis) -> bool:
        """Return whether the outer SELECT produces one row per input row."""

        return _native.sql_select_is_row_level(analysis.authored_sql, self._dialect)

    def _tree(self, analysis: SqlModelAnalysis) -> _native.SqlModelTree:
        tree: _native.SqlModelTree | None = self._tree_by_analysis.get(analysis)
        if tree is None:
            raise SqlAnalysisError("Model analysis does not belong to this compiler invocation")
        return tree
