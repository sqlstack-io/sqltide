"""Resolve retention policies and render them through the native TTL renderer."""

from __future__ import annotations

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import ModelRetentionResolution
from sqltide.compiler.compile.types import RetentionOrigin
from sqltide.compiler.discovery.models import (
    KafkaRetentionPolicy,
    LoadedPipeline,
    ModelRetentionPolicy,
    TransformStep,
)
from sqltide.compiler.discovery.types import (
    KafkaRetentionReference,
    RetentionMissingBehavior,
)


def render_kafka_retention(*, policy: KafkaRetentionPolicy) -> str:
    """Render retention over SQLTide's fixed managed Kafka replay columns."""

    return _native.render_kafka_retention_ttl(
        policy.duration_seconds,
        policy.fallback == KafkaRetentionReference.LANDED,
        policy.cap_at == KafkaRetentionReference.LANDED,
    )


def render_model_retention(
    *, policy: ModelRetentionPolicy, available_columns: Mapping[str, str], model_name: str
) -> str | None:
    """Render a model policy only when its required output columns are available."""

    try:
        return _native.render_model_retention_ttl(
            policy.duration_seconds,
            policy.timestamp_column,
            policy.cap_at_column,
            policy.when_missing == RetentionMissingBehavior.SKIP,
            dict(available_columns),
            model_name,
        )
    except ValueError as error:
        raise PipelineCompileError(str(error)) from None


def effective_model_retention(
    *, loaded_pipeline: LoadedPipeline, transform: TransformStep
) -> ModelRetentionResolution:
    """Resolve explicit, pipeline, then project model retention."""

    if transform.ttl is not None:
        return ModelRetentionResolution()
    if transform.retention is not None:
        return ModelRetentionResolution(
            value=transform.retention,
            origin=RetentionOrigin.MODEL,
        )
    if loaded_pipeline.pipeline.model_retention is not None:
        return ModelRetentionResolution(
            value=loaded_pipeline.pipeline.model_retention,
            origin=RetentionOrigin.PIPELINE,
        )
    if loaded_pipeline.project is not None:
        return ModelRetentionResolution(
            value=loaded_pipeline.project.model_retention,
            origin=(
                RetentionOrigin.PROJECT
                if loaded_pipeline.project.model_retention is not None
                else None
            ),
        )
    return ModelRetentionResolution()
