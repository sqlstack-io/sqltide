"""Load model timestamps used to anchor audit warmup."""

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.executor.auditing._helpers.warmup import load_model_anchors_impl


def load_model_anchors(
    *,
    client: AdapterConnection,
    metadata_database: str,
    target_database: str,
    model_names: tuple[str, ...],
    virtual_environments: bool,
) -> dict[str, str]:
    """Load latest successful apply or publication timestamps by logical model."""

    return load_model_anchors_impl(
        client=client,
        metadata_database=metadata_database,
        target_database=target_database,
        model_names=model_names,
        virtual_environments=virtual_environments,
    )
