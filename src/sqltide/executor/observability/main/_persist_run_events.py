"""Persist externally coordinated run events through the observation workflow."""

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterRunEventRecord
from sqltide.executor.observability._helpers.workflow import assemble_observation_workflow
from sqltide.executor.workflow.main._execute_observation_workflow import (
    execute_observation_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement


def persist_run_events(
    *, connection: AdapterConnection, database: str, events: tuple[AdapterRunEventRecord, ...]
) -> None:
    """Persist run events through the sole observation mutation gateway."""

    rendered: tuple[str, ...] = connection.render_run_events(
        database=database,
        events=events,
    )
    statements: tuple[WarehouseStatement, ...] = assemble_observation_workflow(rendered)
    _ = execute_observation_workflow(statements=statements, connection=connection)
