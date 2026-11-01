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
PLANNED_CHANGE_TYPE_REPLACE: Final[PlannedChangeType] = PlannedChangeType(PlannedChangeType.REPLACE)
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
REBUILD_STRATEGY_OFFLINE: Final[RebuildStrategy] = RebuildStrategy(RebuildStrategy.OFFLINE_REBUILD)

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
DEPLOYMENT_PHASE_BACKFILL: Final[DeploymentPhase] = DeploymentPhase(DeploymentPhase.BACKFILL)
DEPLOYMENT_PHASE_AUDIT: Final[DeploymentPhase] = DeploymentPhase(DeploymentPhase.AUDIT)
DEPLOYMENT_PHASE_PUBLISH: Final[DeploymentPhase] = DeploymentPhase(DeploymentPhase.PUBLISH)

DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.PLAN_SHADOW_TABLE
)
DEPLOYMENT_ACTION_PLAN_SHADOW_MATERIALIZED_VIEW: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.PLAN_SHADOW_MATERIALIZED_VIEW
)
DEPLOYMENT_ACTION_PLAN_SHADOW_VIEW: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.PLAN_SHADOW_VIEW
)
DEPLOYMENT_ACTION_BACKFILL_SUBTREE: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.BACKFILL_SUBTREE
)
DEPLOYMENT_ACTION_AUDIT_SUBTREE: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.AUDIT_SUBTREE
)
DEPLOYMENT_ACTION_PUBLISH_SUBTREE: Final[DeploymentAction] = DeploymentAction(
    DeploymentAction.PUBLISH_SUBTREE
)

VIEW_RELATION_ENGINE: str = "View"
ENGINE_ARGUMENT_OPEN: str = "("
EMPTY_TUPLE_EXPRESSION: str = "tuple()"
BLANK_VALUES: tuple[object, ...] = (None, "")
CATALOG_MATERIALIZED_VIEW_ENGINE: str = "MaterializedView"

MUTABLE_REF_WARNING_CODE: str = "mutable_ref_replay_not_guaranteed"
MUTABLE_REF_WARNING_MESSAGE: str = (
    "Transform uses mutable side refs; exact historical replay equivalence cannot be guaranteed "
    "because side-table state may differ from the original processing time."
)

DESIRED_KAFKA_TABLE_KIND: str = "kafka_table"
DESIRED_TABLE_KIND: str = "table"
DESIRED_MATERIALIZED_VIEW_KIND: str = "materialized_view"
DESIRED_VIEW_KIND: str = "view"
ACTUAL_ACTIVE_TABLE_SOURCE: str = "active_table"
ACTUAL_PHYSICAL_NAME_LOOKUP: str = "physical_name"
