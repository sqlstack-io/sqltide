"""Parse SQLBuild-style MODEL header values."""

from __future__ import annotations

from typing import Literal

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import ModelHeaderSyntaxError
from sqltide.compiler.discovery.models import KafkaRetentionPolicy, ModelRetentionPolicy


def parse_model_header(*, header: str) -> dict[str, object]:
    """Parse one MODEL header with SQLBuild's map and list grammar."""

    try:
        return _native.parse_model_header(header)
    except ValueError as error:
        raise ModelHeaderSyntaxError(str(error)) from None


def parse_model_retention(
    *, value: object, field_path: str
) -> ModelRetentionPolicy | Literal[False] | None:
    """Parse one typed model retention choice or explicit disable."""

    return _native.parse_model_retention(value, field_path)


def parse_kafka_retention(
    *, value: object, field_path: str
) -> KafkaRetentionPolicy | Literal[False] | None:
    """Parse one managed Kafka retention choice or explicit disable."""

    return _native.parse_kafka_retention(value, field_path)
