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
_NATIVE_INTERCHANGE: str = (
    "Internal shape exchanged with native code that the native core now owns; not documented, "
    "exported or in a signature."
)
REMOVED: dict[str, str] = {
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
    "sqltide.executor.deployment.types:NativeDeploymentSummary": _NATIVE_INTERCHANGE,
    "sqltide.executor.deployment.types:NativeInventoryRecord": _NATIVE_INTERCHANGE,
    "sqltide.executor.destruction.models:RelationalStoredDestructionPlan": _NATIVE_INTERCHANGE,
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
    "sqltide.sensors.constants:DEFAULT_DISPATCH_LEASE_TTL_SECONDS": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:DEFAULT_EVENT_BATCH_LIMIT": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:DISPATCH_LEASE_NAME": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:INVOCATIONS_EVENT_SOURCE": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:NODE_RESULTS_EVENT_SOURCE": _NATIVE_CONSTANT,
    "sqltide.sensors.constants:SENSORS_DIRECTORY_NAME": _NATIVE_CONSTANT,
}
