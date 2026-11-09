"""Planner runtime domain types."""

from enum import StrEnum
from typing import TYPE_CHECKING

from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    DesiredView,
)

if TYPE_CHECKING:
    from sqltide.compiler.planner.models import (
        ActualKafkaTable,
        ActualMaterializedView,
        ActualTable,
        ActualView,
    )

type DesiredObject = DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
type ActualObject = ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView


class PlannedChangeType(StrEnum):
    REBUILD = "rebuild"
    CREATE = "create"
    REPLACE = "replace"
    NO_OP = "no_op"


class TableSchemaChangeKind(StrEnum):
    NON_BREAKING = "non_breaking"
    BREAKING = "breaking"


class TableSchemaSeedCompatibility(StrEnum):
    SEEDABLE = "seedable"
    NON_SEEDABLE = "non_seedable"


class RebuildStrategy(StrEnum):
    SHADOW_REBUILD = "shadow_rebuild"
    OFFLINE_REBUILD = "offline_rebuild"


class RebuildExecutionMode(StrEnum):
    FULL_REBUILD = "full_rebuild"
    SEEDED_BOUNDED_REBUILD = "seeded_bounded_rebuild"
    UNSEEDED_BOUNDED_REBUILD = "unseeded_bounded_rebuild"


class DeploymentPhase(StrEnum):
    PLAN = "plan"
    BACKFILL = "backfill"
    AUDIT = "audit"
    PUBLISH = "publish"


class DeploymentAction(StrEnum):
    PLAN_SHADOW_TABLE = "plan_shadow_table"
    PLAN_SHADOW_MATERIALIZED_VIEW = "plan_shadow_materialized_view"
    PLAN_SHADOW_VIEW = "plan_shadow_view"
    BACKFILL_SUBTREE = "backfill_subtree"
    AUDIT_SUBTREE = "audit_subtree"
    PUBLISH_SUBTREE = "publish_subtree"


class RootDeploymentStateKind(StrEnum):
    """How one managed root presents itself in the warehouse."""

    ACTIVE_VIEW_PRESENT = "active_view_present"
    GREENFIELD = "greenfield"
    LOGICAL_VIEW_MISSING = "logical_view_missing"
    INVALID_ACTIVE_VIEW = "invalid_active_view"


class DirectPlanReason(StrEnum):
    """Why one logical model is in the direct execution scope."""

    SELECTED = "selected"
    CHANGED = "changed"
    SOURCE_DRIFT = "source_drift"
    DOWNSTREAM_OF_SELECTED = "downstream_of_selected"
    MISSING_UPSTREAM = "missing_upstream"
    ALL_MODELS = "all_models"


class DirectSelectionMode(StrEnum):
    """How the roots of one direct plan were selected."""

    ALL_MODELS = "all_models"
    EXPLICIT = "explicit"
    CHANGED = "changed"


class DirectSqlBaselineStatus(StrEnum):
    """Relationship between current logical SQL and its optional applied baseline."""

    FIRST_BASELINE = "first_baseline"
    QUERY_CHANGED = "query_changed"
    NO_QUERY_CHANGE = "no_query_change"
    BASELINE_UNAVAILABLE = "baseline_unavailable"


class DirectRelationAction(StrEnum):
    """One destructive or constructive relation action in a direct plan."""

    DROP = "drop"
    CREATE = "create"


class DirectResourceKind(StrEnum):
    """The physical resource kind of one direct relation."""

    TABLE = "table"
    MATERIALIZED_VIEW = "materialized_view"
    VIEW = "view"
