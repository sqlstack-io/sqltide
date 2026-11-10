"""Identify logical models whose realized relations currently exist."""

from collections.abc import Mapping

from sqltide.adapter.classes.adapter_connection import AdapterConnection


def load_materialized_model_names(
    *,
    client: AdapterConnection,
    database: str,
    relation_name_by_model: Mapping[str, str],
) -> frozenset[str]:
    """Return logical model names backed by a relation in the target catalog."""

    relation_names: frozenset[str] = client.load_catalog(database).relation_names()
    return frozenset(
        model_name
        for model_name, relation_name in relation_name_by_model.items()
        if relation_name in relation_names
    )
