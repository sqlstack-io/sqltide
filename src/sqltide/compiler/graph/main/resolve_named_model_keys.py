"""Resolve explicitly named models and pipelines without any graph closure."""

from sqltide.compiler.compile.models import CompiledPipeline, LogicalResourceKey
from sqltide.compiler.graph.constants import (
    MODEL_SELECTOR_NAMESPACE,
    PIPELINE_SELECTOR_NAMESPACE,
    SELECTOR_NAMESPACE_SEPARATOR,
    UPSTREAM_SELECTOR_PREFIX,
)
from sqltide.compiler.graph.exceptions import GraphInputError


def resolve_named_model_keys(
    *, compiled_pipelines: tuple[CompiledPipeline, ...], selectors: tuple[str, ...]
) -> frozenset[LogicalResourceKey]:
    """Preserve selector syntax and validation; execution closure belongs to the caller."""

    pipeline_model_keys: dict[str, tuple[LogicalResourceKey, ...]] = {}
    model_key_by_name: dict[str, LogicalResourceKey] = {}
    for pipeline in compiled_pipelines:
        pipeline_model_keys[pipeline.pipeline.name] = tuple(model.key for model in pipeline.models)
        for model in pipeline.models:
            model_key_by_name[model.key.name] = model.key
    selected_model_keys: set[LogicalResourceKey] = set()
    for raw_selector in selectors:
        for token in raw_selector.split():
            selected_model_keys.update(
                _resolve_selector_token(
                    token=token,
                    pipeline_model_keys=pipeline_model_keys,
                    model_key_by_name=model_key_by_name,
                )
            )
    return frozenset(selected_model_keys)


def _resolve_selector_token(
    *,
    token: str,
    pipeline_model_keys: dict[str, tuple[LogicalResourceKey, ...]],
    model_key_by_name: dict[str, LogicalResourceKey],
) -> frozenset[LogicalResourceKey]:
    if token.startswith(UPSTREAM_SELECTOR_PREFIX) or token.endswith(UPSTREAM_SELECTOR_PREFIX):
        raise GraphInputError(f"Unsupported selector syntax '{token}': '+' is not supported.")
    if SELECTOR_NAMESPACE_SEPARATOR not in token:
        if token in pipeline_model_keys:
            return _resolve_pipeline_selector(value=token, pipeline_model_keys=pipeline_model_keys)
        if token in model_key_by_name:
            return frozenset({model_key_by_name[token]})
        raise GraphInputError(f"Unknown selected model or pipeline '{token}'")
    namespace: str
    value: str
    namespace, value = token.split(SELECTOR_NAMESPACE_SEPARATOR, 1)
    if namespace == PIPELINE_SELECTOR_NAMESPACE:
        return _resolve_pipeline_selector(value=value, pipeline_model_keys=pipeline_model_keys)
    if namespace == MODEL_SELECTOR_NAMESPACE:
        model_key: LogicalResourceKey | None = model_key_by_name.get(value)
        if model_key is None:
            raise GraphInputError(f"Unknown selected model '{value}'")
        return frozenset({model_key})
    raise GraphInputError(f"Unsupported selector namespace '{namespace}' in '{token}'")


def _resolve_pipeline_selector(
    *, value: str, pipeline_model_keys: dict[str, tuple[LogicalResourceKey, ...]]
) -> frozenset[LogicalResourceKey]:
    pipeline_keys: tuple[LogicalResourceKey, ...] | None = pipeline_model_keys.get(value)
    if pipeline_keys is None:
        raise GraphInputError(f"Unknown selected pipeline '{value}'")
    if not pipeline_keys:
        raise GraphInputError(f"Selected pipeline '{value}' does not define any models")
    return frozenset(pipeline_keys)
