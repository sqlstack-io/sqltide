"""Load one authoritative deployment summary."""

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.executor.deployment.exceptions import DeploymentNotFoundError
from sqltide.executor.deployment.main.load_deployments import load_deployments
from sqltide.executor.deployment.models import DeploymentInventory, DeploymentSummary


def load_deployment(
    *,
    deployment_id: str,
    client: AdapterConnection,
    metadata_database: str,
    default_database: str,
) -> DeploymentSummary:
    """Return one deployment or raise a stable explicit-ID error."""
    inventory: DeploymentInventory = load_deployments(
        client=client,
        metadata_database=metadata_database,
        default_database=default_database,
    )
    deployment: DeploymentSummary
    for deployment in inventory.deployments:
        if deployment.deployment_id == deployment_id:
            return deployment
    raise DeploymentNotFoundError(
        f"Deployment '{deployment_id}' was not found in database '{default_database}'"
    )
