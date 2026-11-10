"""CLI command for explicit active-view repair."""

import json
import sys

from sqltide._native import render_expected_warehouse_error
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.executor.repair.main.execute_repair_active_view import execute_repair_active_view
from sqltide.executor.repair.models import RepairActiveViewRequest, RepairActiveViewResult


def run_repair_active_view(
    *,
    database: str,
    table: str,
    deployment_id: str,
    client: AdapterConnection,
) -> int:
    """Repair a stable active view by rebinding it to a chosen deployment table."""

    try:
        result: RepairActiveViewResult = execute_repair_active_view(
            request=RepairActiveViewRequest(
                default_database=database,
                table_name=table,
                deployment_id=deployment_id,
            ),
            client=client,
        )
    except AdapterWarehouseError as error:
        rendered_error: str | None = render_expected_warehouse_error(
            command_name="repair active-view",
            database=database,
            error=error,
        )
        if rendered_error is not None:
            print(rendered_error, file=sys.stderr)
            return 1
        raise
    payload: dict[str, object] = {
        "table_name": result.table_name,
        "target_table_name": result.target_table_name,
    }
    print(json.dumps(payload, indent=2))
    return 0
