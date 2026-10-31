"""Traversal over realized desired-state resources, backed by one native graph per state."""

import sqltide._native as _native
from sqltide.compiler.compile.constants import DESIRED_OBJECT_TYPE_TABLE
from sqltide.compiler.compile.models import DesiredMaterializedView, DesiredState, ObjectKey
from sqltide.compiler.graph.exceptions import GraphInputError
from sqltide.compiler.graph.models import NativeDesiredGraph
from sqltide.compiler.pipeline.types import DesiredObject


def build_desired_reverse_deps(
    *, desired_state: DesiredState
) -> dict[ObjectKey, tuple[ObjectKey, ...]]:
    """Build downstream desired-object keys by upstream key."""

    graph: NativeDesiredGraph = _graph(desired_state)
    return {
        graph.keys[upstream]: _keys(graph=graph, indices=downstream)
        for upstream, downstream in graph.native.reverse_deps()
    }


def order_desired_keys(
    *, desired_state: DesiredState, included_keys: set[ObjectKey]
) -> tuple[ObjectKey, ...]:
    """Order included desired-object keys stably or reject a cycle."""

    graph: NativeDesiredGraph = _graph(desired_state)
    included: set[int] = {
        graph.index_by_key[key] for key in included_keys if key in graph.index_by_key
    }
    try:
        ordered: list[int] = graph.native.order(included)
    except ValueError as error:
        raise GraphInputError(str(error)) from None
    return _keys(graph=graph, indices=ordered)


def collect_desired_descendant_keys(
    *, desired_state: DesiredState, root_key: ObjectKey
) -> tuple[ObjectKey, ...]:
    """Collect one desired-object downstream closure in dependency order."""

    graph: NativeDesiredGraph = _graph(desired_state)
    root: int | None = graph.index_by_key.get(root_key)
    if root is None:
        return ()
    try:
        descendants: list[int] = graph.native.descendants(root)
    except ValueError as error:
        raise GraphInputError(str(error)) from None
    return _keys(graph=graph, indices=descendants)


def find_nearest_replay_anchor_key(
    *,
    desired_state: DesiredState,
    root_key: ObjectKey,
    allow_root_key: bool,
) -> ObjectKey:
    """Find the nearest eligible table through realized driving dependencies only."""

    graph: NativeDesiredGraph = _graph(desired_state)
    root: int | None = graph.index_by_key.get(root_key)
    if root is None:
        return root_key
    try:
        anchor: int = graph.native.nearest_replay_anchor(root, allow_root_key, root_key.name)
    except ValueError as error:
        raise GraphInputError(str(error)) from None
    return graph.keys[anchor]


def _graph(desired_state: DesiredState) -> NativeDesiredGraph:
    keys: tuple[ObjectKey, ...] = tuple(dict.fromkeys(_named_keys(desired_state)))
    index_by_key: dict[ObjectKey, int] = {key: index for index, key in enumerate(keys)}
    return NativeDesiredGraph(
        keys=keys,
        index_by_key=index_by_key,
        native=_native.DesiredGraph(
            [(str(key.object_type), key.name) for key in keys],
            [
                _native_object(object_=object_, index_by_key=index_by_key)
                for object_ in desired_state.objects
            ],
            [index_by_key[key] for key in desired_state.replay_anchor_keys],
        ),
    )


def _named_keys(desired_state: DesiredState) -> list[ObjectKey]:
    named: list[ObjectKey] = []
    object_: DesiredObject
    for object_ in desired_state.objects:
        named.append(object_.key)
        named.extend(object_.deps)
        if isinstance(object_, DesiredMaterializedView):
            named.append(_table_key(object_.source_table_name))
    named.extend(desired_state.replay_anchor_keys)
    return named


def _native_object(
    *, object_: DesiredObject, index_by_key: dict[ObjectKey, int]
) -> tuple[int, list[int], int | None]:
    source: int | None = (
        index_by_key[_table_key(object_.source_table_name)]
        if isinstance(object_, DesiredMaterializedView)
        else None
    )
    return (index_by_key[object_.key], [index_by_key[key] for key in object_.deps], source)


def _keys(*, graph: NativeDesiredGraph, indices: list[int]) -> tuple[ObjectKey, ...]:
    return tuple(graph.keys[index] for index in indices)


def _table_key(name: str) -> ObjectKey:
    return ObjectKey(database=None, object_type=DESIRED_OBJECT_TYPE_TABLE, name=name)
