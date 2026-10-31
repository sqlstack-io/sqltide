"""Build the native graph over one realized desired state."""

from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.graph._helpers.desired_state import (
    build_native_desired_graph as _build_native_desired_graph,
)
from sqltide.compiler.graph.models import NativeDesiredGraph


def build_native_desired_graph(*, desired_state: DesiredState) -> NativeDesiredGraph:
    """Return interned desired-state keys and the native graph over their indices."""

    return _build_native_desired_graph(desired_state)
