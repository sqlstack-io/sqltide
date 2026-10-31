"""Analyze one query or ClickHouse catalog statement."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis._helpers.polyglot import native_sql
from sqltide.compiler.sql_analysis.models import SqlCatalogAnalysis, SqlRelationIdentity


def analyze_catalog_sql(*, sql: str, dialect: str) -> SqlCatalogAnalysis:
    """Return canonical catalog and relation facts through Polyglot."""

    (
        canonical_sql,
        query_sql,
        source_relations,
        direct_source,
        target_relation,
        engine,
        ttl,
        settings,
    ) = native_sql(lambda: _native.sql_analyze_catalog(sql, dialect))
    return SqlCatalogAnalysis(
        canonical_sql=canonical_sql,
        query_sql=query_sql,
        source_relations=tuple(_identity(relation) for relation in source_relations),
        direct_source=None if direct_source is None else _identity(direct_source),
        target_relation=None if target_relation is None else _identity(target_relation),
        engine=engine,
        ttl=ttl,
        settings=tuple(settings),
    )


def _identity(relation: tuple[str | None, str]) -> SqlRelationIdentity:
    return SqlRelationIdentity(database=relation[0], name=relation[1])
