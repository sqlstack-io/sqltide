"""SQL reference vocabulary."""

from enum import StrEnum


class RefType(StrEnum):
    REFERENCE = "reference"
    MUTABLE = "mutable"


class SqlRelationType(StrEnum):
    SOURCE = "source"
    REF = "ref"


class SqlQueryShape(StrEnum):
    """The supported outer shape of an authored model query."""

    SELECT = "select"


class SqlStorageExpressionKind(StrEnum):
    """One model storage clause analyzed against its output schema."""

    ORDER_BY = "order_by"
    PARTITION_BY = "partition_by"
    TTL = "ttl"
