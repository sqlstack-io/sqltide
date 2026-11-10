"""kafka-python glue deleting one consumer group's committed offsets."""

from __future__ import annotations

from kafka.errors import GroupIdNotFoundError, NoError

from sqltide.executor.kafka_admin.types import (
    KafkaAdminClientFactory,
    KafkaAdminClientProtocol,
)


def delete_consumer_group(
    *,
    client_factory: KafkaAdminClientFactory,
    config: dict[str, object],
    consumer_group: str,
) -> tuple[bool, str | None]:
    """Whether the group's offsets were deleted, or the client's failure text."""

    try:
        admin: KafkaAdminClientProtocol = client_factory(**config)
    except Exception as error:
        return (False, str(error))
    try:
        results: list[tuple[str, object]] = admin.delete_consumer_groups([consumer_group])
    except GroupIdNotFoundError:
        return (False, None)
    except Exception as error:
        return (False, str(error))
    finally:
        try:
            admin.close()
        except Exception:
            pass
    deleted: bool = False
    for _group_id, error in results:
        if error is NoError:
            deleted = True
        elif error is not GroupIdNotFoundError:
            return (False, getattr(error, "message", None) or str(error))
    return (deleted, None)
