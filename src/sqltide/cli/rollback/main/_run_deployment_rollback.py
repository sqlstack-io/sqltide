"""CLI command for whole-deployment rollback."""

from sqltide._native import resolve_deployment_rollback
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.rollback._helpers.rendering import (
    confirm_rollback,
    render_rollback_plan,
    render_rollback_result,
)
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import (
    PublishRequest,
    PublishResult,
    RollbackPlan,
    RollbackRequest,
)


def run_deployment_rollback(
    *,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    previous: bool,
    auto_approve: bool,
    json_output: bool,
    client: AdapterConnection,
) -> int:
    """Resolve, confirm, and execute one whole-deployment rollback."""

    if json_output and not auto_approve:
        raise CliUserError("deployment rollback --json requires --auto-approve")
    resolved_metadata_database: str = metadata_database or database
    plan: RollbackPlan = resolve_deployment_rollback(
        request=RollbackRequest(
            deployment_id=deployment_id,
            previous=previous,
            metadata_database=resolved_metadata_database,
            default_database=database,
        ),
        client=client,
    )
    if not json_output:
        print(render_rollback_plan(plan=plan, database=database))
    if not auto_approve and not confirm_rollback():
        print("Rollback cancelled.")
        return 1
    result: PublishResult = execute_publish(
        request=PublishRequest(
            deployment_id=plan.target_deployment_id,
            metadata_database=resolved_metadata_database,
            default_database=database,
            operation="rollback",
            previous_deployment_id=plan.current_deployment_id,
        ),
        client=client,
    )
    print(render_rollback_result(result=result, database=database, json_output=json_output))
    return 0
