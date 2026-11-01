"""Build and persist non-authoritative logical direct SQL baselines."""

from __future__ import annotations

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterDirectFingerprintRecord
from sqltide.compiler.compile.models import CompiledModel
from sqltide.compiler.planner.classes.direct_model_fingerprint import DirectModelFingerprint
from sqltide.executor.direct._helpers.workflow import (
    assemble_direct_fingerprint_statements,
)
from sqltide.executor.direct.models import DirectBuildRequest
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.main._execute_observation_workflow import (
    execute_observation_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement


def build_direct_fingerprint_records(
    *,
    models: tuple[CompiledModel, ...],
    database: str,
    workflow_id: str,
    tool_version: str,
) -> tuple[AdapterDirectFingerprintRecord, ...]:
    """Build direct fingerprint records for models known to match live relations."""

    return tuple(
        DirectModelFingerprint.record(
            model=model, database=database, workflow_id=workflow_id, tool_version=tool_version
        )
        for model in models
    )


def persist_direct_fingerprint_records(
    *,
    records: tuple[AdapterDirectFingerprintRecord, ...],
    database: str,
    connection: AdapterConnection,
) -> None:
    """Persist direct fingerprint records and propagate persistence failures."""

    rendered: tuple[str, ...] = connection.render_direct_fingerprint_observations(
        database=database,
        fingerprints=records,
    )
    statements: tuple[WarehouseStatement, ...] = assemble_direct_fingerprint_statements(
        rendered=rendered
    )
    _ = execute_observation_workflow(statements=statements, connection=connection)


def persist_direct_fingerprints(
    *,
    request: DirectBuildRequest,
    connection: AdapterConnection,
    applied_model_names: frozenset[str] | None = None,
) -> str | None:
    """Best-effort write applied logical SQL without changing command outcome."""

    try:
        records: tuple[AdapterDirectFingerprintRecord, ...] = _fingerprint_records(
            request=request,
            applied_model_names=applied_model_names,
        )
        rendered: tuple[str, ...] = connection.render_direct_fingerprint_observations(
            database=request.metadata_database,
            fingerprints=records,
        )
        statements: tuple[WarehouseStatement, ...] = assemble_direct_fingerprint_statements(
            rendered=rendered
        )
        _ = execute_observation_workflow(statements=statements, connection=connection)
    except (AdapterError, WorkflowExecutionError) as error:
        cause: BaseException = error.cause if isinstance(error, WorkflowExecutionError) else error
        return f"Direct SQL baseline was not recorded: {cause}"
    return None


def _fingerprint_records(
    *, request: DirectBuildRequest, applied_model_names: frozenset[str] | None
) -> tuple[AdapterDirectFingerprintRecord, ...]:
    model_by_name: dict[str, CompiledModel] = {
        model.key.name: model for model in request.realized_project.project.models
    }
    models: list[CompiledModel] = []
    for entry in request.plan.entries:
        if applied_model_names is not None and entry.model_key.name not in applied_model_names:
            continue
        models.append(model_by_name[entry.model_key.name])
    return build_direct_fingerprint_records(
        models=tuple(models),
        database=request.database,
        workflow_id=request.workflow_id,
        tool_version=request.tool_version,
    )
