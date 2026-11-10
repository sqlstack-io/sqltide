"""CLI command for deployment promotion."""

import sys

from sqltide._native import build_publish_deployment_candidates
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import InspectedManagedTableState
from sqltide.cli.entry.main._errors import render_expected_warehouse_error
from sqltide.cli.promotion._helpers.candidates import (
    candidate_root_names,
    enrich_candidates,
)
from sqltide.cli.promotion.main.render_promotion_result import render_promotion_result
from sqltide.cli.readiness.main._render_no_deployment_candidates_message import (
    render_no_deployment_candidates_message,
)
from sqltide.cli.readiness.main.render_ambiguous_deployment_message import (
    render_ambiguous_deployment_message,
)
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest, PublishResult
from sqltide.executor.readiness.models import AuditDeploymentCandidate


def run_deployment_promotion(
    *,
    database: str,
    metadata_database: str | None,
    deployment_id: str | None,
    json_output: bool,
    client: AdapterConnection,
) -> int:
    """Promote a staged deployment and print the result payload."""

    resolved_metadata_database: str = metadata_database or database
    try:
        if deployment_id is None:
            candidates: tuple[AuditDeploymentCandidate, ...] = build_publish_deployment_candidates(
                client=client,
                metadata_database=resolved_metadata_database,
                default_database=database,
            )
            if not candidates:
                print(
                    render_no_deployment_candidates_message(
                        command_name="deployment promote",
                        database=database,
                    ),
                    file=sys.stderr,
                )
                return 1
            if len(candidates) > 1:
                inspected_state: InspectedManagedTableState = client.inspect_managed_table_state(
                    database
                )
                print(
                    render_ambiguous_deployment_message(
                        command_name="deployment promote",
                        database=database,
                        root_names=candidate_root_names(inspected_state),
                        candidates=enrich_candidates(
                            client=client,
                            metadata_database=resolved_metadata_database,
                            candidates=candidates,
                        ),
                    ),
                    file=sys.stderr,
                )
                return 1
        result: PublishResult = execute_publish(
            request=PublishRequest(
                deployment_id=deployment_id,
                metadata_database=resolved_metadata_database,
                default_database=database,
            ),
            client=client,
        )
    except AdapterWarehouseError as error:
        rendered_error: str | None = render_expected_warehouse_error(
            command_name="deployment promote",
            database=database,
            error=error,
        )
        if rendered_error is not None:
            print(rendered_error, file=sys.stderr)
            return 1
        raise
    print(
        render_promotion_result(
            result=result,
            database=database,
            json_output=json_output,
        )
    )
    return 0
