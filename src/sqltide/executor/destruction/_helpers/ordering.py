"""Deterministic dependency ordering for destructive relation groups."""

import sqltide._native as _native
from sqltide.executor.destruction.constants import DUPLICATE_RELATION_ORDERING_ERROR
from sqltide.executor.destruction.exceptions import DestructionConsistencyError
from sqltide.executor.destruction.models import DestructionRelationEvidence


def reverse_topologically_order_relations(
    relations: tuple[DestructionRelationEvidence, ...],
) -> tuple[DestructionRelationEvidence, ...]:
    """Order dependants before prerequisites with deterministic lexical ties."""

    try:
        positions: list[int] = _native.order_relations_for_drop(
            [(relation.name, list(relation.dependency_relation_names)) for relation in relations]
        )
    except ValueError as error:
        if error.args[0] == DUPLICATE_RELATION_ORDERING_ERROR:
            duplicate: DestructionRelationEvidence = relations[error.args[1]]
            raise DestructionConsistencyError(
                f"Destruction evidence contains duplicate relation {duplicate.name!r}"
            ) from None
        unresolved: tuple[str, ...] = tuple(error.args[1])
        raise DestructionConsistencyError(
            f"Destruction relation dependency cycle blocks ordering: {unresolved!r}"
        ) from None
    return tuple(relations[position] for position in positions)
