"""Resolve the effective ClickHouse Kafka consumer group."""

import sqltide._native as _native


def database_scoped_consumer_group(*, consumer_group: str, database: str) -> str:
    """Return the consumer group ClickHouse uses for one target database."""

    return _native.database_scoped_consumer_group(consumer_group, database)
