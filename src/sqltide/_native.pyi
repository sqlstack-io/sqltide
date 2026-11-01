import os
from collections.abc import Callable, Mapping
from typing import Any, Literal

from sqltide.compiler.discovery.models import (
    AuditDefaults,
    AuthoredProjectConfig,
    EffectiveProjectConfiguration,
    ExecutionSettings,
    ExternalTableSourceStep,
    KafkaLandingStep,
    KafkaRetentionPolicy,
    LocalProjectConfig,
    ModelRetentionPolicy,
    PipelineNaming,
    PipelineProtection,
    PostgresRefreshSourceStep,
    ReplayOnChangePolicy,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import BoundedReplayFallback

type _Span = tuple[int, int, int, int, int, int]

class SqlScanError(ValueError):
    args: tuple[str, str, _Span | None]

def parse_model_header(header: str) -> dict[str, object]: ...
def skip_trivia(sql: str, start: int) -> int: ...
def find_matching_parenthesis(sql: str, open_index: int, context: str) -> int: ...
def find_next_unquoted_character(sql: str, character: str, start: int) -> int | None: ...
def try_consume_keyword(sql: str, start: int, keyword: str) -> int | None: ...
def normalized_statement_sql(sql: str) -> str: ...
def extract_references(sql: str) -> list[tuple[str, str, str | None, _Span]]: ...
def split_header_blocks(sql: str, keyword: str) -> list[tuple[int, int, str, str]]: ...
def extract_top_level_ctes(
    sql: str, context: str
) -> tuple[list[tuple[str, str, _Span]], str, _Span]: ...
def outer_projection_spans(sql: str) -> list[_Span]: ...
def outer_star_span(sql: str, start: int, end: int) -> _Span | None: ...
def outer_double_colon_type(sql: str, start: int, end: int) -> str | None: ...
def expand_macro_calls(
    sql: str,
    macro_names: list[str],
    file_path: str,
    call: Callable[..., object],
    fail: Callable[..., BaseException],
    locate: Callable[..., BaseException],
) -> str: ...
def find_macro_call_names(sql: str) -> tuple[list[str], str | None]: ...
def find_next_macro_start(sql: str, start: int) -> int | None: ...
def rewrite_template_relations(
    template: str, rewrites: list[tuple[str, str, bool]], database_placeholder: str
) -> str: ...
def append_template_predicate(template: str, predicate: str) -> str: ...
def merge_template_ctes(template: str, named_queries: list[tuple[str, str]]) -> str: ...

class PolyglotError(ValueError): ...

def sql_normalize_data_type(sql: str, dialect_name: str) -> str: ...
def polyglot_analyze_query(sql: str, dialect_name: str) -> dict[str, Any]: ...

class SqlTreeError(ValueError):
    args: tuple[Any, ...]

class SqlModelTree:
    @property
    def function_names(self) -> list[str]: ...
    @property
    def has_group_by(self) -> bool: ...
    @property
    def projections(self) -> list[tuple[int, str, str, _Span]]: ...
    def uses_prewhere(self) -> bool: ...

class SqlTreeAnalyzer:
    def __init__(self, dialect: str) -> None: ...
    def analyze_model(self, sql: str) -> SqlModelTree: ...
    def resolve_model(
        self,
        model: SqlModelTree,
        authored_sql: str,
        references: list[tuple[str, str, str | None, int, int]],
        resolver: Mapping[str, str],
        database_placeholder: str,
    ) -> tuple[str, str]: ...
    def rewrite_relations(
        self,
        sql: str,
        references: list[tuple[str, str, str | None, int, int]],
        resolver: Mapping[str, str],
    ) -> dict[str, str]: ...
    def canonicalize_query(self, sql: str, database_placeholder: str) -> tuple[str, str]: ...
    def storage_column_names(self, expression: str) -> list[str]: ...

def sql_rewrite_query(
    sql: str,
    dialect_name: str,
    rewrites: list[tuple[str, str, list[str | None] | None, bool]],
    predicate: str | None,
    ctes: list[tuple[str, str]],
) -> tuple[str, list[str], bool]: ...
def sql_select_aggregate_facts(sql: str, dialect_name: str) -> tuple[list[str], bool]: ...
def sql_analyze_catalog(
    sql: str, dialect_name: str
) -> tuple[
    str,
    str | None,
    list[tuple[str | None, str]],
    tuple[str | None, str] | None,
    tuple[str | None, str] | None,
    str | None,
    str | None,
    list[tuple[str, str]],
]: ...
def sql_expression_list(sql: str, dialect_name: str) -> list[str]: ...
def sql_validate_query(sql: str, dialect_name: str) -> None: ...
def sql_validate_insert_query(sql: str, dialect_name: str) -> None: ...
def sql_canonicalize(sql: str, dialect_name: str) -> str: ...
def sql_render_string_literal(value: str, dialect_name: str) -> str: ...
def json_dumps(value: object, indent: int | None = None, sort_keys: bool = False) -> str: ...
def stage_artifact_files(
    staging_root: os.PathLike[str] | str, files: list[tuple[str, str]]
) -> None: ...
def artifact_model_query_path(pipeline: str, model: str) -> str: ...
def artifact_model_resource_path(pipeline: str, model: str, resource_kind: str) -> str: ...
def artifact_source_resource_path(source: str, resource: str) -> str: ...
def artifact_audit_path(relative_file: str, name: str | None = None) -> str: ...
def artifact_test_path(name: str, target_names: list[str]) -> str: ...
def build_project_graph(
    sources: list[str],
    models: list[tuple[str, str, str, str | None, list[tuple[str, str | None]]]],
    pipeline_modes: list[tuple[str, str]],
    pipeline_reference_scope: bool,
    allowed_cross_pipeline_references: list[tuple[str, str]],
) -> tuple[list[int], list[list[tuple[int, str]]], list[list[tuple[int, int]]], list[int]]: ...

type _NativeSpec = tuple[
    str,
    list[tuple[str, str, str | None]],
    list[str | None],
    list[str],
    list[tuple[str, str]] | None,
]

def classify_object_changes(
    comparisons: list[tuple[_NativeSpec, _NativeSpec | None, bool]],
) -> list[tuple[str, str | None, str | None]]: ...
def resolve_variable_values(
    values: Mapping[str, object],
    environment: Mapping[str, str],
    field_path_prefix: str,
    defer_missing_environment: bool,
) -> dict[str, object]: ...
def interpolate_config_value(
    value: object, variables: Mapping[str, object], environment: Mapping[str, str], field_path: str
) -> object: ...
def parse_duration_seconds(value: object, field_path: str, allow_zero: bool) -> int: ...
def parse_freshness_policy(
    payload: object, label: str, file_path: str
) -> SourceFreshnessPolicy | None: ...
def parse_pipeline_mode(
    payload: Mapping[str, object], file_path: str, default_mode: str
) -> str: ...
def parse_pipeline_settings(
    payload: Mapping[str, object], file_path: str, pipeline_name: str, mode: str
) -> tuple[
    ReplayOnChangePolicy | None,
    BoundedReplayFallback | None,
    PipelineNaming,
    PipelineProtection | None,
    AuditDefaults,
    ExecutionSettings,
    ModelRetentionPolicy | Literal[False] | None,
]: ...
def parse_source_registry(
    files: list[tuple[str, object, str | None]],
    variables: Mapping[str, object],
    environment: Mapping[str, str],
    default_ttl: str | None,
    default_retention: KafkaRetentionPolicy | Literal[False] | None,
    default_broker_list: str | None,
    default_freshness: SourceFreshnessPolicy | None,
    derive_name: Callable[..., tuple[str, str, str]],
) -> list[tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...]]: ...

DEFAULT_ADAPTER_NAME: str
DEFAULT_PIPELINE_PREFIX: str
DEFAULT_TABLE_PREFIX: str
DEFAULT_VIEW_PREFIX: str
RUN_UNRESPONSIVE_AFTER_SECONDS: int
DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS: int
DEFAULT_POSTGRES_PORT: int
DEFAULT_KAFKA_FORMAT: str
DEFAULT_SQL_MODEL_ENGINE: str

def check_model_header_keys(header: Mapping[str, object], file_path: str) -> None: ...
def parse_model_spec(
    header: Mapping[str, object], file_path: str, check_query: Callable[..., str | None]
) -> tuple[str, dict[str, Any]]: ...
def resolve_effective_configuration(
    project_payload: Mapping[str, object],
    project_path: str,
    local_payload: Mapping[str, object] | None,
    local_path: str,
    selected_target: str | None,
    cli_variables: Mapping[str, object],
    environment: Mapping[str, str],
    timezone_exists: Callable[[str], bool],
) -> EffectiveProjectConfiguration: ...
def parse_project_config(
    payload: Mapping[str, object], file_path: str, timezone_exists: Callable[[str], bool]
) -> AuthoredProjectConfig: ...
def parse_local_config(payload: Mapping[str, object], file_path: str) -> LocalProjectConfig: ...
def infer_pipeline_sources(
    pipelines: list[tuple[str, list[tuple[str, str]]]],
    inputs_by_transform: dict[str, str],
    sources: set[str],
) -> tuple[list[str | None], str | None]: ...
def audit_readiness(
    observation: tuple[
        str,
        bool,
        bool,
        int | None,
        int | None,
        bool,
        tuple[int, int, int, int, bool, float | None] | None,
        float | None,
        str | None,
        int | None,
    ],
    thresholds: tuple[float, float],
) -> tuple[str, int | None, float | None, list[str]]: ...
def select_rollback_publications(
    publications: list[tuple[str, tuple[int, str] | None, list[tuple[str, str, str]]]],
    active: list[tuple[str, str, str]],
    deployment_id: str | None,
) -> tuple[int, int]: ...

type _RetainedDeployment = tuple[str, str, bool, list[tuple[str | None, bool, str, str, str]]]
type _RetainedPublication = tuple[str, tuple[int, str], int, list[str], list[tuple[str, str, str]]]

def janitor_binding_activity(
    deployments_argument: list[_RetainedDeployment],
    publications: list[_RetainedPublication],
    active: list[tuple[str, str, str]],
) -> tuple[set[str], list[tuple[str, str]]]: ...
def janitor_preview(
    deployments_argument: list[_RetainedDeployment],
    publications: list[_RetainedPublication],
    active: list[tuple[str, str, str]],
    physical_relations: set[tuple[str, str]],
    retention: tuple[str, int, int, int],
) -> list[tuple[int, bool, str]]: ...
def is_deployment_id(text: str) -> bool: ...
def is_deployment_physical_name(physical_name: str) -> bool: ...
def order_relations_for_drop(relations: list[tuple[str, list[str]]]) -> list[int]: ...

class DesiredGraph:
    def __init__(
        self,
        keys: list[tuple[str, str]],
        objects: list[
            tuple[
                int,
                list[int],
                str,
                int | None,
                str | None,
                tuple[tuple[str, int | None] | None, tuple[str, int | None] | None] | None,
            ]
        ],
        replay_anchors: list[int],
    ) -> None: ...
    def reverse_deps(self) -> list[tuple[int, list[int]]]: ...
    def order(self, included: set[int]) -> list[int]: ...
    def descendants(self, root: int) -> list[int]: ...
    def rebuild_subtrees(
        self,
        changes: list[tuple[int, str, bool, str | None, str | None, str | None]],
    ) -> list[tuple[int, list[int], int, bool, str, bool, str | None, str | None, int | None]]: ...
    def nearest_replay_anchor(self, root: int, allow_root: bool, root_name: str) -> int: ...
