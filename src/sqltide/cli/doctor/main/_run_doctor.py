"""CLI command for runtime diagnosis."""

import json
import sys

from sqltide._native import render_expected_warehouse_error
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.executor.doctor.main.execute_doctor import execute_doctor
from sqltide.executor.doctor.models import DoctorRequest, DoctorResult


def run_doctor(
    *,
    database: str,
    client: AdapterConnection,
) -> int:
    """Run runtime diagnosis and print the result payload."""

    try:
        result: DoctorResult = execute_doctor(
            request=DoctorRequest(default_database=database),
            client=client,
        )
    except AdapterWarehouseError as error:
        rendered_error: str | None = render_expected_warehouse_error(
            command_name="doctor",
            database=database,
            error=error,
        )
        if rendered_error is not None:
            print(rendered_error, file=sys.stderr)
            return 1
        raise
    payload: dict[str, object] = {
        "active_views": [
            {
                "table_name": status.table_name,
                "state_kind": status.state_kind,
                "active_deployment_id": status.active_deployment_id,
                "candidate_deployment_ids": list(status.candidate_deployment_ids),
            }
            for status in result.active_views
        ]
    }
    print(json.dumps(payload, indent=2))
    return 0
