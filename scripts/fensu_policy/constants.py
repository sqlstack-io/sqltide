"""Path and module constants for SQLTide's Fensu policy."""

PRODUCT_SCOPE_NAME: str = "root"
COMPILER_PATH_PREFIX: tuple[str, ...] = ("src", "sqltide", "compiler")
SQL_ANALYSIS_PATH_PREFIX: tuple[str, ...] = (
    "src",
    "sqltide",
    "compiler",
    "sql_analysis",
)
CLICKHOUSE_ADAPTER_PATH_PREFIX: tuple[str, ...] = (
    "src",
    "sqltide",
    "adapters",
    "clickhouse",
)
ADAPTER_IMPLEMENTATION_MODULE_PREFIX: tuple[str, ...] = ("sqltide", "adapters")
CLICKHOUSE_DRIVER_ROOT_MODULE: str = "clickhouse_connect"
POLYGLOT_ROOT_MODULE: str = "polyglot_sql"
SQLGLOT_ROOT_MODULE: str = "sqlglot"
DYNAMIC_IMPORT_CALL_NAMES: tuple[str, ...] = ("__import__", "importlib.import_module")
RETIRED_CLICKHOUSE_MODULE_PREFIX: tuple[str, ...] = ("sqltide", "clickhouse")
RETIRED_CLICKHOUSE_INTEGRATION_MODULE_PREFIX: tuple[str, ...] = (
    "sqltide",
    "integrations",
    "clickhouse",
)
WORKFLOW_GATEWAY_PATH: tuple[str, ...] = (
    "src",
    "sqltide",
    "executor",
    "workflow",
    "main",
    "_execute_warehouse_workflow.py",
)
ADAPTER_CONNECTION_MODULE_PARTS: tuple[str, ...] = (
    "sqltide",
    "adapter",
    "classes",
    "adapter_connection",
)
ADAPTER_CONNECTION_SYMBOL: str = "AdapterConnection"
CLICKHOUSE_CONNECTION_MODULE_PARTS: tuple[str, ...] = (
    "sqltide",
    "adapters",
    "clickhouse",
    "classes",
    "clickhouse_connection",
)
CLICKHOUSE_CONNECTION_PATH: tuple[str, ...] = (
    "src",
    "sqltide",
    "adapters",
    "clickhouse",
    "classes",
    "clickhouse_connection.py",
)
CLICKHOUSE_CONNECTION_SYMBOL: str = "ClickHouseConnection"
WORKFLOW_MODELS_MODULE_PARTS: tuple[str, ...] = (
    "sqltide",
    "executor",
    "workflow",
    "models",
)
BUILD_WORKFLOW_EXECUTION_PATH: tuple[str, ...] = (
    "src",
    "sqltide",
    "executor",
    "workflow",
    "main",
    "execute_build_workflow.py",
)
BUILD_WORKFLOW_PUBLICATION_PATH: tuple[str, ...] = (
    "src",
    "sqltide",
    "cli",
    "workflow_artifacts",
    "main",
    "_publish_build_workflow.py",
)
WORKFLOW_CONSUMER_PATHS: tuple[tuple[str, ...], ...] = (
    WORKFLOW_GATEWAY_PATH,
    BUILD_WORKFLOW_EXECUTION_PATH,
    BUILD_WORKFLOW_PUBLICATION_PATH,
    (
        "src",
        "sqltide",
        "cli",
        "workflow_artifacts",
        "_helpers",
        "build_publication.py",
    ),
)
WORKFLOW_ASSEMBLER_PATHS: tuple[tuple[str, ...], ...] = (
    ("src", "sqltide", "executor", "direct", "_helpers", "workflow.py"),
    ("src", "sqltide", "executor", "backfill", "_helpers", "workflow.py"),
    ("src", "sqltide", "executor", "population", "_helpers", "workflow.py"),
    ("src", "sqltide", "executor", "repair", "_helpers", "workflow.py"),
    ("src", "sqltide", "executor", "destruction", "_helpers", "workflow.py"),
    ("src", "sqltide", "executor", "workflow", "main", "target_mutation_lock.py"),
    ("src", "sqltide", "executor", "auditing", "_helpers", "schedule_claim_workflow.py"),
)
RETIRED_ADAPTER_MUTATION_METHODS: tuple[str, ...] = (
    "command",
    "insert_rows",
    "ensure_database",
    "realize_resource",
    "migrate_metadata_state",
    "persist_metadata_state",
    "execute_replay",
    "replace_stable_bindings",
    "cleanup_relations",
)
WORKFLOW_CONSUMER_PROHIBITED_CALLS: tuple[str, ...] = (
    "WarehouseStatement",
    "render_resource",
    "sorted",
    "reversed",
)
WORKFLOW_GATEWAY_CALL_NAME: str = "execute_workflow_sql"
BUILD_WORKFLOW_EXECUTION_FUNCTION: str = "execute_build_workflow"
BUILD_WORKFLOW_EXECUTION_PARAMETERS: tuple[str, ...] = (
    "published_workflow",
    "connection",
    "emitter",
)
PUBLISHED_WORKFLOW_CONSTRUCTOR_NAME: str = "PublishedBuildWorkflow"
WORKFLOW_STATEMENT_CONSTRUCTOR_NAME: str = "WarehouseStatement"
WORKFLOW_PLAN_CALL_PREFIX: str = "plan_"
WORKFLOW_RENDER_CALL_PREFIX: str = "render_"
WORKFLOW_TOPOLOGICAL_CALL_FRAGMENT: str = "topological"
ANNOTATED_ASSIGNMENT_CHILD_COUNT: int = 2
METHOD_REFERENCE_MINIMUM_PARTS: int = 2
FUNCTION_DEFINITION_SYNTAX_KINDS: tuple[str, ...] = (
    "FunctionDef",
    "AsyncFunctionDef",
)
ADAPTER_CLASS_RECEIVER_NAMES: tuple[str, ...] = ("self", "cls")
OBSERVABILITY_AUTHORITY_ALLOWED_PATH_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("src", "sqltide", "adapter"),
    ("src", "sqltide", "adapters"),
    ("src", "sqltide", "executor", "observability"),
    ("src", "sqltide", "executor", "workflow", "main", "_execute_observation_workflow.py"),
    ("src", "sqltide", "quality"),
    ("src", "sqltide", "dev_server"),
    ("src", "sqltide", "sensors"),
    ("src", "sqltide", "events"),
)
QUALITY_MODULE_PREFIX: tuple[str, ...] = ("sqltide", "quality")
OBSERVABILITY_QUERY_CALL_NAMES: frozenset[str] = frozenset({"render_latest_node_status_query"})
OBSERVABILITY_TABLE_CONSTANT_NAMES: frozenset[str] = frozenset(
    {
        "METADATA_DEAD_LETTERS_TABLE_NAME",
        "METADATA_INVOCATIONS_TABLE_NAME",
        "METADATA_NODE_RESULTS_TABLE_NAME",
        "METADATA_RUN_EVENTS_TABLE_NAME",
        "METADATA_SENSOR_CHECKPOINTS_TABLE_NAME",
        "METADATA_SENSOR_TICKS_TABLE_NAME",
        "METADATA_SENSOR_STEPS_TABLE_NAME",
        "METADATA_SENSOR_OVERRIDES_TABLE_NAME",
        "METADATA_SENSOR_LEASES_TABLE_NAME",
    }
)
OBSERVABILITY_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "_sqltide_dead_letters",
        "_sqltide_invocations",
        "_sqltide_node_results",
        "_sqltide_run_events",
        "_sqltide_sensor_checkpoints",
        "_sqltide_sensor_ticks",
        "_sqltide_sensor_steps",
        "_sqltide_sensor_overrides",
        "_sqltide_sensor_leases",
    }
)
EVENT_CATALOG_PATH_PREFIX: tuple[str, ...] = ("src", "sqltide", "events")
EVENT_CONSTRUCTION_CALL_NAMES: frozenset[str] = frozenset({"AuditCompleted(", "RunCompleted("})
