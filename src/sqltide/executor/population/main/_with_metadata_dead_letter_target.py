"""Place dead-letter materialized views against the run's metadata database."""

from dataclasses import replace

from sqltide.adapter.models import AdapterMaterializedView


def with_metadata_dead_letter_target[ResourceT](
    *, resource: ResourceT, database: str, metadata_database: str
) -> ResourceT | AdapterMaterializedView:
    """Return a dead-letter view targeting the metadata database when it differs."""

    if (
        not isinstance(resource, AdapterMaterializedView)
        or not resource.dead_letters
        or metadata_database == database
    ):
        return resource
    return replace(
        resource,
        target_relation_name=f"{metadata_database}.{resource.target_relation_name}",
    )
