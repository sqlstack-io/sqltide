"""Best-effort committed-offset deletion for consumer groups of fresh landing tables."""

from __future__ import annotations

from collections.abc import Mapping

from kafka.errors import GroupIdNotFoundError, NoError

from sqltide._native import database_scoped_consumer_group
from sqltide.adapter.models import AdapterManagedSource
from sqltide.executor.kafka_admin.models import ConsumerGroupOffsetReset
from sqltide.executor.kafka_admin.types import (
    KafkaAdminClientFactory,
    KafkaAdminClientProtocol,
)

_API_VERSION_AUTO_TIMEOUT_MS: int = 1_000
_REQUEST_TIMEOUT_MS: int = 5_000
_SECURITY_PROTOCOL_CONFIG_NAME: str = "security_protocol"
_TRANSLATED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("kafka_security_protocol", _SECURITY_PROTOCOL_CONFIG_NAME),
    ("kafka_sasl_mechanism", "sasl_mechanism"),
    ("kafka_sasl_username", "sasl_plain_username"),
    ("kafka_sasl_password", "sasl_plain_password"),
)


def reset_one_consumer_group(
    *,
    resource: AdapterManagedSource,
    database: str,
    landing_relation_name: str,
    client_factory: KafkaAdminClientFactory,
) -> ConsumerGroupOffsetReset:
    """Delete one source's committed offsets so a fresh landing consumes from earliest."""

    consumer_group: str = database_scoped_consumer_group(
        consumer_group=resource.consumer_group,
        database=database,
    )
    deleted: bool
    error: str | None
    deleted, error = _delete_consumer_group(
        broker_list=resource.broker_list,
        consumer_group=consumer_group,
        settings=dict(resource.settings),
        client_factory=client_factory,
    )
    return ConsumerGroupOffsetReset(
        consumer_group=consumer_group,
        landing_relation_name=landing_relation_name,
        deleted=deleted,
        error=error,
        notice=_notice(
            consumer_group=consumer_group,
            landing_relation_name=landing_relation_name,
            deleted=deleted,
            error=error,
        ),
    )


def _notice(
    *,
    consumer_group: str,
    landing_relation_name: str,
    deleted: bool,
    error: str | None,
) -> str | None:
    if error is not None:
        return (
            f"Could not reset committed offsets for consumer group '{consumer_group}' ahead "
            f"of creating {landing_relation_name}: {error}. If this landing table replaces a "
            "previous one, ingestion may resume from stale offsets; reset the group manually."
        )
    if deleted:
        return (
            f"Reset committed offsets for consumer group '{consumer_group}' ahead of creating "
            f"{landing_relation_name}; ingestion starts from the earliest retained messages."
        )
    return None


def _delete_consumer_group(
    *,
    broker_list: str,
    consumer_group: str,
    settings: Mapping[str, str],
    client_factory: KafkaAdminClientFactory,
) -> tuple[bool, str | None]:
    try:
        admin: KafkaAdminClientProtocol = client_factory(
            **_client_config(broker_list=broker_list, settings=settings)
        )
    except Exception as error:
        return (False, str(error))
    try:
        results: list[tuple[str, object]] = admin.delete_consumer_groups([consumer_group])
    except GroupIdNotFoundError:
        return (False, None)
    except Exception as error:
        return (False, str(error))
    finally:
        _close_quietly(admin)
    return _interpret_delete_results(results)


def _interpret_delete_results(results: list[tuple[str, object]]) -> tuple[bool, str | None]:
    deleted: bool = False
    for _group_id, error in results:
        if error is NoError:
            deleted = True
        elif error is not GroupIdNotFoundError:
            return (False, getattr(error, "message", None) or str(error))
    return (deleted, None)


def _close_quietly(admin: KafkaAdminClientProtocol) -> None:
    try:
        admin.close()
    except Exception:
        return


def _client_config(*, broker_list: str, settings: Mapping[str, str]) -> dict[str, object]:
    config: dict[str, object] = {
        "bootstrap_servers": [item.strip() for item in broker_list.split(",")],
        "request_timeout_ms": _REQUEST_TIMEOUT_MS,
        "api_version_auto_timeout_ms": _API_VERSION_AUTO_TIMEOUT_MS,
    }
    source_name: str
    target_name: str
    for source_name, target_name in _TRANSLATED_SETTINGS:
        value: str | None = settings.get(source_name)
        if value is not None:
            config[target_name] = (
                value.upper() if target_name == _SECURITY_PROTOCOL_CONFIG_NAME else value
            )
    return config
