"""Planner constants."""

from typing import Final

from sqltide.compiler.planner.types import (
    DeploymentAction,
    DeploymentPhase,
    PlannedChangeType,
    RebuildExecutionMode,
    RebuildStrategy,
    TableSchemaChangeKind,
    TableSchemaSeedCompatibility,
)

PLANNED_CHANGE_TYPE_REBUILD: Final[PlannedChangeType] = PlannedChangeType(PlannedChangeType.REBUILD)
PLANNED_CHANGE_TYPE_CREATE: Final[PlannedChangeType] = PlannedChangeType(PlannedChangeType.CREATE)
PLANNED_CHANGE_TYPE_NO_OP: Final[PlannedChangeType] = PlannedChangeType(PlannedChangeType.NO_OP)

TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING: Final[TableSchemaChangeKind] = TableSchemaChangeKind(
    TableSchemaChangeKind.NON_BREAKING
)
TABLE_SCHEMA_CHANGE_KIND_BREAKING: Final[TableSchemaChangeKind] = TableSchemaChangeKind(
    TableSchemaChangeKind.BREAKING
)

TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE: Final[TableSchemaSeedCompatibility] = (
    TableSchemaSeedCompatibility(TableSchemaSeedCompatibility.SEEDABLE)
)
TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE: Final[TableSchemaSeedCompatibility] = (
    TableSchemaSeedCompatibility(TableSchemaSeedCompatibility.NON_SEEDABLE)
)

REBUILD_STRATEGY_SHADOW: Final[RebuildStrategy] = RebuildStrategy(RebuildStrategy.SHADOW_REBUILD)

REBUILD_EXECUTION_MODE_FULL: Final[RebuildExecutionMode] = RebuildExecutionMode(
    RebuildExecutionMode.FULL_REBUILD
)
REBUILD_EXECUTION_MODE_SEEDED_BOUNDED: Final[RebuildExecutionMode] = RebuildExecutionMode(
    RebuildExecutionMode.SEEDED_BOUNDED_REBUILD
)
REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED: Final[RebuildExecutionMode] = RebuildExecutionMode(
    RebuildExecutionMode.UNSEEDED_BOUNDED_REBUILD
)

DEPLOYMENT_PHASE_PLAN: Final[DeploymentPhase] = DeploymentPhase(DeploymentPhase.PLAN)

DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.PLAN_SHADOW_TABLE
)

BLANK_VALUES: tuple[object, ...] = (None, "")
DIRECT_DEAD_LETTERS_VIEW_SUFFIX: str = "__dead_letters"
