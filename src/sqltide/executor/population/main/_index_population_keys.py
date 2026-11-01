"""Index object keys for native population decisions."""

from collections.abc import Iterable

from sqltide.compiler.compile.models import ObjectKey


def index_population_keys(keys: Iterable[ObjectKey]) -> dict[ObjectKey, int]:
    """Number each distinct key in first-seen order."""

    index_by_key: dict[ObjectKey, int] = {}
    key: ObjectKey
    for key in keys:
        index_by_key.setdefault(key, len(index_by_key))
    return index_by_key
