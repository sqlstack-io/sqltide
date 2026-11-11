"""The recorded 0.65.x shape surface and the names deliberately removed from it."""

from typing import Any

BASELINE_FILE: str = "scripts/shape_surface/baseline.json"
PACKAGE: str = "sqltide"
ROLES: frozenset[str] = frozenset({"models", "types", "constants", "exceptions"})
TUPLE_ANNOTATION: str = "tuple"
INT_ANNOTATION: str = "int"
BOOL_ANNOTATION: str = "bool"
CAUSE_ANNOTATION: str = "BaseException"
PROBE_TUPLE_KEY: str = "tuple"
PROBE_ERROR_KEY: str = "error"
PROBE_OVERRIDES: dict[str, dict[str, Any]] = {
    "sqltide.compiler.rules.exceptions:RulesError": {
        "args": [],
        "kwargs": {"findings": {PROBE_TUPLE_KEY: []}},
    },
}
VOLATILE: frozenset[str] = frozenset({"sqltide.cli.build.constants:SQLTIDE_TOOL_VERSION"})
_CLI_INTERNAL: str = (
    "Internal to the CLI, which now runs natively; not documented, exported or in a signature."
)
_NATIVE_CONSTANT: str = (
    "Internal constant now owned by the native core; not documented, exported or in a signature."
)
_UNUSED_SHAPE: str = (
    "Unused internal shape with no reader or writer anywhere; not documented, exported, raised or "
    "in a signature."
)
_NATIVE_INTERCHANGE: str = (
    "Internal shape exchanged with native code that the native core now owns; not documented, "
    "exported or in a signature."
)
REMOVED: dict[str, str] = {
    "sqltide.adapter.constants:METADATA_PUBLISH_OPERATION_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.adapter.constants:METADATA_PUBLISH_PREVIOUS_DEPLOYMENT_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.adapter.constants:REFRESHABLE_VIEW_SETTING": _NATIVE_CONSTANT,
    "sqltide.adapter.constants:REPLAY_VALUE_KIND_INTEGER": _NATIVE_CONSTANT,
    "sqltide.adapter.constants:REPLAY_VALUE_KIND_TIMESTAMP": _NATIVE_CONSTANT,
    "sqltide.adapter.constants:TARGET_MUTATION_LOCK_TABLE_NAME": _NATIVE_CONSTANT,
    "sqltide.adapters.clickhouse.constants:CLICKHOUSE_DEFAULT_INDEX_GRANULARITY": _NATIVE_CONSTANT,
    "sqltide.cli.entry.models:CliConnectionOptions": _CLI_INTERNAL,
    "sqltide.cli.entry.models:ResolvedCliInvocation": _CLI_INTERNAL,
    "sqltide.cli.entry.models:ResolvedCliProjectConfig": _CLI_INTERNAL,
    "sqltide.cli.entry.models:ResolvedInvocationConnection": _CLI_INTERNAL,
    "sqltide.cli.plan.constants:DIRECT_MODE_LABEL": _NATIVE_CONSTANT,
    "sqltide.cli.presentation.constants:ANSI_BLUE": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_BOLD": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_DIM": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_GREEN": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_RED": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_RESET": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:ANSI_YELLOW": _CLI_INTERNAL,
    "sqltide.cli.presentation.constants:NOT_AVAILABLE": _NATIVE_CONSTANT,
    "sqltide.cli.presentation.constants:TRUTHY_ENV_VALUES": _NATIVE_CONSTANT,
    "sqltide.cli.presentation.constants:UTC_SUFFIX": _CLI_INTERNAL,
    "sqltide.cli.presentation.models:CliTheme": _CLI_INTERNAL,
    "sqltide.cli.presentation.models:TextStyle": _CLI_INTERNAL,
    "sqltide.cli.workflow_artifacts.types:WorkflowArtifactOwner": _CLI_INTERNAL,
    "sqltide.compiler.access.constants:PIPELINE_PERMISSIONS": _NATIVE_CONSTANT,
    "sqltide.compiler.access.constants:PROJECT_PERMISSIONS": _NATIVE_CONSTANT,
    "sqltide.compiler.access.constants:TARGET_PERMISSIONS": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:AGGREGATING_ENGINE_NAMES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:FRAMEWORK_OWNED_KAFKA_SETTING_KEYS": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:KAFKA_LANDED_AT_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:KAFKA_OFFSET_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:KAFKA_PARTITION_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:KAFKA_TABLE_NAME_PREFIX": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:KAFKA_TIMESTAMP_COLUMN_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:MANAGED_OBJECT_NAME_PREFIXES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:MODEL_REF_FUNCTION_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:RAW_LANDING_COLUMNS": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REF_FUNCTION_NAMES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REF_TYPE_KEYWORD": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REF_WITH_TYPE_ARGUMENT_COUNT": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REPLAY_ALL_COLUMN_NAMES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REPLAY_OFFSET_LINEAGE_COLUMN_NAMES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REPLAY_REQUIRED_COLUMN_NAMES_BY_MODE": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:REPLAY_TIME_LINEAGE_COLUMN_NAMES": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:SOURCE_REF_FUNCTION_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:SUPPORTED_KAFKA_TABLE_FORMAT": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:TRANSFORM_TABLE_NAME_PREFIX": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.constants:VALID_REF_ARGUMENT_COUNTS": _NATIVE_CONSTANT,
    "sqltide.compiler.compile.models:RowErrorContext": _UNUSED_SHAPE,
    "sqltide.compiler.discovery.constants:KAFKA_NAMING_MACRO_TOPIC_PARAMETER": _NATIVE_CONSTANT,
    "sqltide.compiler.discovery.constants:PIPELINE_CONFIG_FILE_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.discovery.constants:PYTHON_PACKAGE_INITIALIZER_FILE_NAME": _NATIVE_CONSTANT,
    "sqltide.compiler.discovery.types:ModelFacts": _UNUSED_SHAPE,
    "sqltide.compiler.discovery.types:PipelineFacts": _UNUSED_SHAPE,
    "sqltide.compiler.discovery.types:ProjectPolicyFacts": _UNUSED_SHAPE,
    "sqltide.compiler.test_discovery.models:SqlTestHeader": _UNUSED_SHAPE,
    "sqltide.executor.auditing.types:AuditResultStatus": _UNUSED_SHAPE,
    "sqltide.executor.deployment.types:NativeDeploymentSummary": _NATIVE_INTERCHANGE,
    "sqltide.executor.deployment.types:NativeInventoryRecord": _NATIVE_INTERCHANGE,
    "sqltide.executor.destruction.models:DestructionPlanParts": _UNUSED_SHAPE,
    "sqltide.executor.destruction.models:RelationalStoredDestructionPlan": _NATIVE_INTERCHANGE,
    "sqltide.executor.readiness.models:ColumnNameSystemRow": _UNUSED_SHAPE,
    "sqltide.executor.readiness.models:CountQueryRow": _UNUSED_SHAPE,
    "sqltide.executor.readiness.models:CreateTableQueryRow": _UNUSED_SHAPE,
    "sqltide.executor.readiness.models:OffsetSummaryQueryRow": _UNUSED_SHAPE,
    "sqltide.executor.readiness.models:ScalarSummaryQueryRow": _UNUSED_SHAPE,
    "sqltide.executor.reconcile.models:ReconcileCandidate": _UNUSED_SHAPE,
    "sqltide.executor.reconcile.types:NativeObjectKey": _NATIVE_INTERCHANGE,
    "sqltide.executor.reconcile.types:NativeReconcileObject": _NATIVE_INTERCHANGE,
    "sqltide.executor.reconcile.types:NativeReconcileOutcome": _NATIVE_INTERCHANGE,
    "sqltide.executor.reconcile.types:NativeReconcileTable": _NATIVE_INTERCHANGE,
    "sqltide.executor.testing.constants:ASSERTION_RESULT_LABEL_PREFIX": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:COMPARISON_CASE_INDEX": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:COMPARISON_DIFF_TYPE_INDEX": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:COMPARISON_MULTIPLICITY_INDEX": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:COMPARISON_ROW_LENGTH": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:COMPARISON_ROW_VALUES_INDEX": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:MISSING_DIFF_TYPE": _NATIVE_CONSTANT,
    "sqltide.executor.testing.constants:UNEXPECTED_DIFF_TYPE": _NATIVE_CONSTANT,
    "sqltide.executor.testing.models:ComparisonRows": _UNUSED_SHAPE,
    "sqltide.sensors.constants:DEFAULT_DISPATCH_LEASE_TTL_SECONDS": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:DEFAULT_EVENT_BATCH_LIMIT": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:DISPATCH_LEASE_NAME": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:INVOCATIONS_EVENT_SOURCE": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:NODE_RESULTS_EVENT_SOURCE": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:SENSORS_DIRECTORY_NAME": _NATIVE_CONSTANT,
}
