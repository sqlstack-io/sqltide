"""Parse ClickHouse catalog details through the SQL-analysis boundary."""

from sqltide._native import analyze_catalog_sql, parse_expression_list
from sqltide.adapter.exceptions import AdapterResultError
from sqltide.adapters.clickhouse.constants import CLICKHOUSE_VIEW_ENGINE, EMPTY_KEY_EXPRESSIONS
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import SqlCatalogAnalysis, SqlRelationIdentity


def parse_catalog_ddl_details(
    create_table_query: str,
) -> tuple[str | None, tuple[tuple[str, str], ...], str | None]:
    """Return TTL, settings, and materialized-view target from one catalog DDL."""

    analysis: SqlCatalogAnalysis = _analyze(create_table_query)
    target_name: str | None = (
        None if analysis.target_relation is None else analysis.target_relation.name
    )
    return analysis.ttl, analysis.settings, target_name


def parse_catalog_engine(create_table_query: str) -> str | None:
    """Return the storage engine expression from one catalog DDL statement."""

    return _analyze(create_table_query).engine


def parse_sorting_key(value: str) -> tuple[str, ...]:
    """Parse the current ClickHouse sorting-key projection."""

    normalized: str = value.strip()
    if not normalized:
        return ()
    try:
        return parse_expression_list(sql=normalized, dialect="clickhouse")
    except SqlAnalysisError as error:
        raise AdapterResultError(f"ClickHouse sorting key could not be parsed: {error}") from None


def normalize_partition_key(value: str) -> str | None:
    """Normalize ClickHouse's empty partition-key sentinels."""

    return None if value in EMPTY_KEY_EXPRESSIONS else value


def normalize_catalog_query(value: str) -> str | None:
    """Normalize a relation query using the mandatory SQL-analysis boundary."""

    normalized: str = value.strip()
    return None if not normalized else _analyze(normalized).canonical_sql


def parse_catalog_query_details(
    *, engine: str, value: str
) -> tuple[str | None, tuple[str, ...], str | None]:
    """Return canonical SQL, physical sources, and a valid direct stable binding."""

    normalized: str = value.strip()
    if not normalized:
        return None, (), None
    analysis: SqlCatalogAnalysis = _analyze(normalized)
    source_names: tuple[str, ...] = tuple(
        dict.fromkeys(source.name for source in analysis.source_relations)
    )
    stable_binding_name: str | None = (
        analysis.direct_source.name
        if engine == CLICKHOUSE_VIEW_ENGINE and analysis.direct_source is not None
        else None
    )
    return analysis.canonical_sql, source_names, stable_binding_name


def parse_catalog_relation_identities(value: str) -> tuple[SqlRelationIdentity, ...]:
    """Return every database-aware physical source from catalog SQL."""

    normalized: str = value.strip()
    return () if not normalized else _analyze(normalized).source_relations


def parse_catalog_target_identity(value: str) -> SqlRelationIdentity | None:
    """Return a database-aware materialized target from catalog DDL."""

    normalized: str = value.strip()
    return None if not normalized else _analyze(normalized).target_relation


def extract_source_relation_name(value: str) -> str | None:
    """Return the first physical source relation named by a catalog query."""

    normalized: str = value.strip()
    if not normalized:
        return None
    analysis: SqlCatalogAnalysis = _analyze(normalized)
    return None if analysis.first_source is None else analysis.first_source.name


def extract_stable_binding(*, engine: str, as_select: str) -> str | None:
    """Return a direct stable-view target relation when the query has the expected shape."""

    if engine != CLICKHOUSE_VIEW_ENGINE or not as_select.strip():
        return None
    analysis: SqlCatalogAnalysis = _analyze(as_select)
    return None if analysis.direct_source is None else analysis.direct_source.name


def extract_create_query_source(create_table_query: str) -> str | None:
    """Return the first physical source from one CREATE VIEW catalog statement."""

    analysis: SqlCatalogAnalysis = _analyze(create_table_query)
    return None if analysis.first_source is None else analysis.first_source.name


def _analyze(sql: str) -> SqlCatalogAnalysis:
    try:
        return analyze_catalog_sql(sql=sql, dialect="clickhouse")
    except SqlAnalysisError as error:
        raise AdapterResultError(f"ClickHouse catalog SQL could not be parsed: {error}") from None
