"""Planner runtime domain types."""

from sqltide._native import DeploymentAction as DeploymentAction
from sqltide._native import DeploymentPhase as DeploymentPhase
from sqltide._native import DirectPlanReason as DirectPlanReason
from sqltide._native import DirectRelationAction as DirectRelationAction
from sqltide._native import DirectResourceKind as DirectResourceKind
from sqltide._native import DirectSelectionMode as DirectSelectionMode
from sqltide._native import DirectSqlBaselineStatus as DirectSqlBaselineStatus
from sqltide._native import PlannedChangeType as PlannedChangeType
from sqltide._native import RebuildExecutionMode as RebuildExecutionMode
from sqltide._native import RebuildStrategy as RebuildStrategy
from sqltide._native import RootDeploymentStateKind as RootDeploymentStateKind
from sqltide._native import TableSchemaChangeKind as TableSchemaChangeKind
from sqltide._native import TableSchemaSeedCompatibility as TableSchemaSeedCompatibility
from sqltide.compiler.compile.models import (
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    DesiredView,
)
from sqltide.compiler.planner.models import (
    ActualKafkaTable,
    ActualMaterializedView,
    ActualTable,
    ActualView,
)

type DesiredObject = DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView
type ActualObject = ActualKafkaTable | ActualTable | ActualMaterializedView | ActualView
