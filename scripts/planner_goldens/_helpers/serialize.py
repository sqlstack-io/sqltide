"""Turn planner results into deterministic JSON-ready values."""

import dataclasses
from collections.abc import Mapping
from enum import Enum


def jsonable(value: object) -> object:
    """Return `value` as nested dicts, lists, and scalars, with sets sorted."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(jsonable(key)): jsonable(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted((jsonable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)
