import os
from collections.abc import Callable, Mapping
from typing import Any, Literal

from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterManifestSnapshot,
    AdapterReplayRequest,
    CatalogSnapshot,
)
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
def render_clickhouse_resource(resource: object, database: str, if_not_exists: bool) -> str: ...
def render_clickhouse_set_difference_comparison(targets: tuple[object, ...]) -> str: ...
def render_clickhouse_ensure_database(database: str) -> str: ...
def render_clickhouse_replay_coverage_query(request: object) -> str: ...
def render_clickhouse_replay_from_capture(request: object) -> str: ...
def render_clickhouse_replay_from_deployment(request: object) -> list[str]: ...
def database_scoped_consumer_group(consumer_group: str, database: str) -> str: ...
def render_clickhouse_metadata_migration_statements(database: str) -> list[str]: ...
def render_clickhouse_metadata_migration_workflow(database: str) -> list[str]: ...
def render_clickhouse_metadata_state(database: str, state: object) -> list[str]: ...
def render_clickhouse_latest_fingerprints_query(
    database: str, logical_model_identities: list[str]
) -> str: ...
def render_clickhouse_direct_fingerprint_observations(
    database: str, fingerprints: tuple[object, ...]
) -> list[str]: ...
def render_clickhouse_manifest_history_query(
    database: str, project_identity: str, target_name: str, target_database: str
) -> str: ...
def render_clickhouse_manifest_publication(database: str, manifest: object) -> str: ...
def render_clickhouse_sensor_state_inserts(
    database: str, state: object, include_migration: bool
) -> list[str]: ...
def render_clickhouse_sensor_retention_cleanup(database: str, retention_days: int) -> list[str]: ...
def render_clickhouse_run_event_inserts(
    database: str, events: tuple[object, ...], include_migration: bool
) -> list[str]: ...
def render_clickhouse_run_statement_inserts(
    database: str, statements: tuple[object, ...], include_migration: bool
) -> list[str]: ...
def render_clickhouse_latest_node_status_query(
    database: str, project_identity: str, target_identity: str, nodes: tuple[object, ...]
) -> str: ...
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
    declarations: Declarations | None,
) -> str: ...

class Declarations:
    def render_constant(self, name: str, file_path: str) -> str: ...
    def render_enum_member(self, name: str, member: str, file_path: str) -> str: ...

def load_declarations(
    constant_files: list[tuple[str, str]], enum_files: list[tuple[str, str]]
) -> Declarations: ...
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
def destruction_available_pipelines(
    current: list[str], snapshot: AdapterManifestSnapshot | None
) -> list[str]: ...
def resolve_destruction_selection(
    project_argument: tuple[
        list[tuple[str, bool, str | None, list[tuple[str, str]]]],
        list[list[int]],
        list[list[int]],
        list[tuple[str, int | None]],
        list[int],
        list[int],
    ],
    request_argument: tuple[str, str, list[str], list[str], bool],
    available: list[str],
) -> tuple[list[str], list[str], list[str]]: ...
def plan_destruction_scope(
    project_argument: tuple[
        list[tuple[str, bool, str | None, list[tuple[str, str]]]],
        list[list[int]],
        list[list[int]],
        list[tuple[str, int | None]],
        list[int],
        list[int],
    ],
    request_argument: tuple[str, str, list[str], list[str], bool],
    affected: list[str],
    current: list[str],
    snapshot: AdapterManifestSnapshot | None,
    catalog_snapshot: CatalogSnapshot,
    deployment_inventory: AdapterDeploymentInventory,
) -> tuple[list[str], list[str], list[tuple[str, str, list[str], list[str], list[str]]]]: ...
def build_destruction_evidence(
    database: str,
    owned: list[tuple[str, str, list[str], list[str], list[str]]],
    catalog_snapshot: CatalogSnapshot,
    stats: dict[str, tuple[int, int]],
    limit: int | None,
) -> list[
    tuple[
        str,
        str,
        str,
        bool,
        int | None,
        int | None,
        str | None,
        list[str],
        list[str],
        list[str],
        list[str],
    ]
]: ...
def build_destruction_challenges(
    pipeline_names: list[str], production_reset: bool
) -> list[str]: ...
def destruction_plan_fingerprint(
    *,
    operation: str,
    target: str,
    database: str,
    metadata_database: str,
    requested_pipeline_names: list[str],
    included_dependent_pipeline_names: list[str],
    affected_pipeline_names: list[str],
    affected_model_names: list[str],
    affected_source_names: list[str],
    relations: list[
        tuple[
            str,
            str,
            str,
            bool,
            int | None,
            int | None,
            str | None,
            list[str],
            list[str],
            list[str],
            list[str],
        ]
    ],
    challenges: list[str],
    manifest_fingerprint: str,
    relation_drop_size_limit: int | None,
    relation_drop_size_server_limit: int | None,
    relation_drop_size_override: int | None,
    relation_drop_size_policy_observed: bool,
    include_orphans: bool,
) -> str: ...
def list_destruction_inactive_pipelines(
    current: list[str], snapshot_object: AdapterManifestSnapshot
) -> list[tuple[str, int, int, str]]: ...
def plan_population_replays(
    *,
    roots: list[tuple[int, int, str, str, str | None, int | None]],
    tables: list[tuple[int, str, str | None, list[str], list[tuple[str, str]]]],
    views: list[tuple[str, str]],
    key_names: list[str],
    physical_names: list[tuple[int, str]],
    external_columns: list[
        tuple[int, tuple[str | None, str | None, str | None, str | None, str | None]]
    ],
    replay_anchor_keys: set[int],
    default_database: str,
    boundary_time: str,
    watermarks: list[tuple[int, str, str]] | None,
) -> tuple[
    list[tuple[str, str]],
    list[
        tuple[
            int,
            str,
            str,
            tuple[str, str, str, str],
            int,
            list[tuple[str, str, str | None]],
            list[str],
            tuple[str, str, str | None, int | None],
            bool,
            list[str],
            list[tuple[str, str]],
            bool,
        ]
    ],
]: ...
def bounded_replay_outcome(
    mode: str | None,
    forced_start_time: bool,
    live_columns: set[str] | None,
    fallback_without_history: bool,
) -> str: ...
def unified_diff(a: str, b: str, from_file: str, to_file: str, keep_ends: bool) -> list[str]: ...
def stage_deployment(
    keys: list[tuple[str, str, bool]],
    subtrees: list[tuple[int, list[int]]],
    deployment_id: str | None,
) -> tuple[
    list[tuple[str, str, str, int, int | None, str | None]],
    list[tuple[int, str]],
    list[tuple[int, int]],
]: ...
def plan_direct_build(
    *,
    nodes: list[
        tuple[
            str,
            tuple[bool, bool, str, list[tuple[str, str]]] | None,
            bool,
            str,
            list[tuple[str, str | None]],
            str | None,
            bool,
            str | None,
            list[tuple[int, bool]],
            list[int],
        ]
    ],
    database: str,
    selection_mode: str,
    selected: set[int],
    source_drift: set[int],
    include_missing_upstream: bool,
    has_start_time: bool,
    catalog: list[tuple[str, bool, list[str], list[str]]],
    external_columns: list[
        tuple[str, tuple[str | None, str | None, str | None, str | None, str | None]]
    ],
    fingerprints: tuple[bool, str | None, list[tuple[str, str, str]]],
    mutable_ref_names: list[str],
) -> tuple[
    list[int],
    list[int],
    list[int],
    list[tuple[int, str, bool, bool]],
    list[
        tuple[
            int,
            str,
            list[tuple[str, str]],
            int | None,
            bool,
            tuple[str, str, str | None, str | None, str | None, str | None],
        ]
    ],
    list[
        tuple[
            int,
            int,
            str,
            list[str],
            str,
            list[int],
            bool,
            list[tuple[str, str]],
        ]
    ],
    list[tuple[str, int, str]],
    list[tuple[str, int, str]],
    list[int],
]: ...
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
def inspected_publish_candidates(
    candidates: list[tuple[str, str]], deployment_id: str
) -> list[int]: ...
def publish_stable_bindings(
    relations: list[tuple[str | None, str, str, str]],
    existing: set[str],
    default_database: str,
    deployment_id: str,
) -> list[tuple[str, str, str]]: ...
def obsolete_binding_removals(
    current: list[tuple[str | None, bool, str, str, str]],
    deployments_argument: list[_RetainedDeployment],
    publications: list[tuple[str, list[str]]],
    active: list[tuple[str, str, str]],
    default_database: str,
) -> list[tuple[str, str]]: ...
def binding_replacement_preview(
    bindings: list[tuple[str, str, str]],
    removals: list[tuple[str, str]],
    active: list[tuple[str, str, str]],
) -> tuple[
    bool, list[int], list[tuple[int, str]], list[tuple[int, str]], list[tuple[str, str]]
]: ...

type _ReconcileKey = tuple[str | None, str, str]
type _ReconcileObject = tuple[
    str,
    _ReconcileKey,
    tuple[str, list[str], str | None, list[tuple[str, str, str | None]]] | None,
    tuple[str, str] | None,
]

def classify_reconcile_targets(
    desired: list[_ReconcileObject],
    actual: list[_ReconcileObject],
    selected: list[_ReconcileKey],
) -> list[tuple[_ReconcileKey, str, tuple[int, int | None] | None, list[str]]]: ...
def parse_deployment_diff(comparison: str, identifiers: list[str]) -> tuple[str, str]: ...
def deployment_diff_relations(
    from_: str,
    to: str,
    deployments_argument: list[_RetainedDeployment],
    active: list[tuple[str, str, str]],
    default_database: str,
    /,
) -> tuple[list[tuple[str, str, str | None, str | None]], list[str]]: ...
def deployment_diff_count_sql(database: str, relation: str) -> str: ...

type _DiffSide = tuple[bool, tuple[list[tuple[str, str, str | None]], int | None] | None]

def deployment_diff_status(from_: _DiffSide, to: _DiffSide, /) -> str: ...

type _SourceResource = tuple[str, str, list[str], list[tuple[str, str]]]
type _SourceProject = tuple[
    list[str],
    list[tuple[str, str, bool, int | None, list[int], list[str]]],
    list[int],
    list[list[_SourceResource]],
]
type _SourceRelation = tuple[str, list[tuple[str, str]], list[tuple[str, str]], str | None]

def plan_preserved_managed_sources(
    plan: tuple[list[int], list[int], list[tuple[str, int]]],
    project_argument: _SourceProject,
    relations_argument: list[_SourceRelation],
    database: str,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[tuple[int, int]],
    list[tuple[tuple[int, int], tuple[int, int], str, list[str]]],
    list[tuple[int, int]],
]: ...
def drifted_managed_sources(
    project_argument: _SourceProject, relations_argument: list[_SourceRelation]
) -> list[tuple[int, list[str], list[str]]]: ...

type _WorkflowStatement = tuple[str, str, str, str, bool]

def virtual_workflow_statements(
    context: tuple[str, str, str, tuple[str, str, str, str, str], str | None, bool, bool],
    rendered: tuple[
        list[tuple[str, str | None]],
        str,
        list[str],
        list[tuple[str, str]],
        list[str],
        list[tuple[str, str]],
        list[tuple[str, str]],
        float,
    ],
    roots: list[
        tuple[str, AdapterReplayRequest, bool, tuple[str | None, str, str], bool, list[str]]
    ],
) -> list[_WorkflowStatement]: ...
def direct_workflow_statements(
    preparation: tuple[
        str,
        str,
        list[tuple[str, str]],
        list[tuple[str, str, str, str, str]],
        list[str],
        list[tuple[str, str]],
        list[tuple[str, str]],
    ],
    execution: tuple[
        float,
        list[tuple[str, bool]],
        list[tuple[str, str]],
        list[tuple[str, str, str, str]],
        list[tuple[str, str]],
        int,
    ],
) -> tuple[list[_WorkflowStatement], list[_WorkflowStatement]]: ...
def workflow_step_segment(value: str) -> str: ...
def workflow_terminate_sql(sql: str) -> str: ...
def resolve_direct_replays(
    entries: list[tuple[str, list[str]]],
    replays: list[tuple[str, str, str, str, str, str]],
    forced_start_time: bool,
    desired_tables: list[tuple[str, list[tuple[str, str]]]],
    catalog: list[tuple[str, list[tuple[str, str]]]],
) -> list[tuple[str, str, str, str | None]]: ...
def realize_direct_replay_capture(
    mode: str,
    ranges: list[tuple[str | None, str, str, bool]],
) -> tuple[
    list[tuple[str, str, bool, str | None]],
    list[tuple[str, str | None]],
    list[tuple[int, int, int]],
]: ...
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
