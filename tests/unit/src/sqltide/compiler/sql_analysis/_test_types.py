from dataclasses import dataclass

from sqltide.compiler.sql_analysis.models import SqlNamedQuery, SqlRelationRewrite


@dataclass(frozen=True)
class TemplateRewriteTestCase:
    description: str
    template: str
    relation_rewrites: tuple[SqlRelationRewrite, ...]
    predicate: str | None
    prepend_ctes: tuple[SqlNamedQuery, ...]
    expected_query: str
    expected_aggregate_semantics: bool


@dataclass(frozen=True)
class TemplateRewriteErrorTestCase:
    description: str
    template: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ReferenceExtractionTestCase:
    description: str
    sql: str
    expected_references: tuple[tuple[str, str, str | None], ...]
    expected_source_slices: tuple[str, ...]
    expected_coordinates: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class ReferenceErrorTestCase:
    description: str
    sql: str
    expected_error_fragment: str
    expected_line: int
    expected_column: int


@dataclass(frozen=True)
class ReferenceParityTestCase:
    description: str
    sql: str
    expected_reference_count: int


@dataclass(frozen=True)
class CompilerReferenceDiagnosticTestCase:
    description: str
    sql: str
    source_path: str
    source_line: int
    source_column: int
    expected_location: tuple[str, int, int, int, int]
    expected_phase: str


@dataclass(frozen=True)
class RepositoryReferenceFixtureTestCase:
    description: str
    fixture_root: str
    expected_relative_paths: tuple[str, ...]
    expected_reference_name: str
    expected_replacement: str


@dataclass(frozen=True)
class ReferenceRewriteTestCase:
    description: str
    sql: str
    resolver: dict[str, str]
    expected_sql: str


@dataclass(frozen=True)
class ReferenceRewriteErrorTestCase:
    description: str
    sql: str
    resolver: dict[str, str]
    expected_error_fragment: str


@dataclass(frozen=True)
class SqlAnalysisBoundaryTestCase:
    description: str
    forbidden_import: str
    removed_dependency_name: str
    expected_outside_import_paths: tuple[str, ...]
    expected_removed_source_paths: tuple[str, ...]
    expected_retired_path_exists: bool
    retired_dependency_name: str


@dataclass(frozen=True)
class ModelTypeAnalysisTestCase:
    description: str
    sql: str
    expected_columns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ModelAggregateAnalysisTestCase:
    description: str
    sql: str
    engine: str
    expected_order_by: str
    expected_function_names: tuple[str, ...]
    expected_has_group_by: bool
    expected_engine_has_aggregate_semantics: bool
    expected_has_semantics: bool


@dataclass(frozen=True)
class ModelStorageAnalysisTestCase:
    description: str
    sql: str
    order_by: tuple[str, ...]
    partition_by: str | None
    ttl: str | None
    expected_storage_facts: tuple[tuple[str, str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class ModelRepeatedResolutionTestCase:
    description: str
    sql: str
    resolver: dict[str, str]
    expected_template_fragment: str


@dataclass(frozen=True)
class ModelResolutionTestCase:
    description: str
    sql: str
    resolver: dict[str, str]
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class ModelReservedPlaceholderTestCase:
    description: str
    sql: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ModelRawRelationTestCase:
    description: str
    sql: str
    resolver: dict[str, str]
    expected_error_fragment: str


@dataclass(frozen=True)
class ModelAnalysisOrderingTestCase:
    description: str
    sql_by_model: tuple[str, ...]
    expected_output_names: tuple[str, ...]


@dataclass(frozen=True)
class QueryRelationRewriteTestCase:
    description: str
    sql: str
    rewrites: tuple[SqlRelationRewrite, ...]
    expected_fragments: tuple[str, ...]
    expected_absent_fragments: tuple[str, ...]


@dataclass(frozen=True)
class QueryPredicateRewriteTestCase:
    description: str
    sql: str
    predicate: str
    expected_query: str


@dataclass(frozen=True)
class QueryRewriteErrorTestCase:
    description: str
    sql: str
    named_queries: tuple[SqlNamedQuery, ...]
    expected_error_fragment: str


@dataclass(frozen=True)
class CatalogSqlAnalysisTestCase:
    description: str
    sql: str
    expected_query_fragment: str
    expected_first_source: tuple[str | None, str | None]
    expected_direct_source: tuple[str | None, str | None]
    expected_target_relation: tuple[str | None, str | None]
    expected_ttl: str | None
    expected_settings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CatalogPhysicalSourcesTestCase:
    description: str
    sql: str
    expected_sources: tuple[tuple[str | None, str], ...]


@dataclass(frozen=True)
class SqlExpressionListTestCase:
    description: str
    sql: str
    expected_expressions: tuple[str, ...]


@dataclass(frozen=True)
class SqlStringLiteralTestCase:
    description: str
    value: str
    expected_literal: str
