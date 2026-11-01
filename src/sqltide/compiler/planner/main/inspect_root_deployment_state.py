"""Classify one managed root from neutral inspected warehouse state."""

import sqltide._native as _native
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.compiler.compile.models import ObjectKey
from sqltide.compiler.planner.models import RootDeploymentInspection
from sqltide.compiler.planner.types import RootDeploymentStateKind


def inspect_root_deployment_state(
    *,
    inspected_state: InspectedManagedTableState,
    root_key: ObjectKey,
) -> RootDeploymentInspection:
    """Classify one managed root as active, greenfield, or damaged."""

    state_kind, active_deployment_id = _native.root_deployment_state(
        [
            (binding.database, binding.logical_name, binding.physical_name)
            for binding in inspected_state.active_bindings
        ],
        [
            (candidate.database, candidate.logical_name, candidate.physical_name)
            for candidate in inspected_state.physical_candidates
        ],
        root_key.database,
        root_key.name,
    )
    return RootDeploymentInspection(
        root_key=root_key,
        state_kind=RootDeploymentStateKind(state_kind),
        active_deployment_id=active_deployment_id,
    )
