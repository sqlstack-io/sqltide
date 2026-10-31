"""Planner golden type declarations."""

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
