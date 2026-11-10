"""Register the JSON API routes on one FastAPI application."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from time import monotonic_ns
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from sqltide._native import (
    prepare_build_workflow,
    prepared_pipeline_scope,
    resolve_prepared_build_scope,
    validate_build_pipeline_limit,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.auth.classes.control_store import ControlStore
from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.compiler.access.types import GrantScope, Permission
from sqltide.compiler.compile.models import CompiledSource
from sqltide.compiler.discovery.models import KafkaLandingStep
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.dev_server._helpers.payloads.definitions_payload import build_definitions_payload
from sqltide.dev_server._helpers.payloads.deployments_payload import (
    build_deployment_detail_payload,
    build_deployments_payload,
)
from sqltide.dev_server._helpers.payloads.plan_payload import (
    build_mode_aware_plan_payload,
    count_replay_rows,
)
from sqltide.dev_server._helpers.payloads.state_payload import (
    build_topics_payload,
)
from sqltide.dev_server._helpers.queries.message_query import (
    ensure_header_columns,
    read_source_message_facets,
    read_source_message_record,
    read_source_messages,
)
from sqltide.dev_server._helpers.queries.runs_query import (
    read_active_runs,
    read_run_events,
    read_run_statement,
    read_runs,
)
from sqltide.dev_server._helpers.server.authorization_enforcement import (
    build_access_policy_payload,
    build_capabilities_payload,
    is_system_admin,
    require_check_authorization,
    require_cleanup_authorization,
    require_kill_authorization,
    require_message_read_authorization,
    require_operation_authorization,
    require_prepared_build_authorization,
    require_promotion_authorization,
    require_run_cancel_authorization,
)
from sqltide.dev_server._helpers.server.checks_execution import (
    build_checks_status_payload,
    run_audit_batch,
    run_one_audit,
    run_one_test,
)
from sqltide.dev_server._helpers.server.compile_runner import build_status_payload
from sqltide.dev_server._helpers.server.deployment_operations import (
    build_deployment_diff_payload,
    promotion_executed_logical_ids,
    run_deployment_cleanup,
    run_deployment_promotion,
)
from sqltide.dev_server._helpers.server.destruction_routes import (
    register_destruction_routes,
)
from sqltide.dev_server._helpers.server.sensor_routes import register_sensor_routes
from sqltide.dev_server.classes.audit_scheduler import AuditScheduler
from sqltide.dev_server.classes.build_process import BuildProcessManager, build_invocation
from sqltide.dev_server.classes.dev_server_state import DevServerState
from sqltide.dev_server.classes.kafka_lag_reader import KafkaLagReader
from sqltide.dev_server.classes.kafka_topic_reader import KafkaTopicReader
from sqltide.dev_server.classes.sensor_scheduler import SensorScheduler
from sqltide.dev_server.classes.warehouse_runtime import WarehouseRuntime
from sqltide.dev_server.exceptions import (
    BuildInProgressError,
    BuildStartError,
    DevServerError,
    MessageQueryValidationError,
    MessageSchemaError,
    ProjectNotCompiledError,
)
from sqltide.dev_server.main._build_audit_scheduler_payload import (
    build_audit_scheduler_payload,
)
from sqltide.dev_server.models import (
    AuditBatchRunRequest,
    BuildRunRequest,
    ChecksRunRequest,
    CompileOutcome,
    DeploymentCleanupRequest,
    DevControlStores,
    DevExecutionContext,
    MessageFacetsRequest,
    MessageRecordRequest,
    MessagesQueryRequest,
    OperationAuthorizationContext,
)
from sqltide.dev_server.types import (
    ActivityTone,
    AuditScheduleState,
    CompileAuthorizationGuard,
    DevServerReporter,
    RunPresentationStatus,
)
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.direct.exceptions import DirectBuildError

_HTTP_BAD_REQUEST: int = 400
_HTTP_NOT_FOUND: int = 404
_HTTP_CONFLICT: int = 409
_HTTP_BAD_GATEWAY: int = 502
_HTTP_SERVICE_UNAVAILABLE: int = 503


@dataclass(frozen=True)
class _RouteConnections:
    required: Callable[[], AdapterConnection]
    read: Callable[[], AbstractContextManager[AdapterConnection]]


def register_api_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    warehouse: WarehouseRuntime,
    project_dir: Path,
    builds: BuildProcessManager,
    schedulers: tuple[AuditScheduler, SensorScheduler],
    broker_readers: tuple[KafkaLagReader, KafkaTopicReader],
    reporter: DevServerReporter,
    execution_context: DevExecutionContext,
    control_stores: DevControlStores,
) -> FastAPI:
    """Attach every /api route; handlers close over the shared server state."""

    database: str | None = execution_context.database
    control_store: ControlStore = control_stores.accounts
    authorization_context: OperationAuthorizationContext = OperationAuthorizationContext(
        store=control_store,
        project_dir=project_dir,
        selected_target=execution_context.selected_target,
    )

    def read_status() -> dict[str, object]:
        warehouse_status: dict[str, object] = warehouse.status()
        return build_status_payload(
            outcome=state.current(),
            warehouse_status=warehouse_status,
        )

    def refresh_warehouse() -> dict[str, object]:
        if warehouse.connection is None:
            _ = warehouse.connect_now()
        state.snapshot.invalidate()
        return read_status()

    def reload_project(request: Request) -> dict[str, object]:
        guard: CompileAuthorizationGuard = partial(
            require_operation_authorization,
            request=request,
            store=control_store,
            project_dir=project_dir,
            selected_target=execution_context.selected_target,
            permission=Permission.PROJECT_RELOAD,
            grant_scope=GrantScope.PROJECT,
            affected_pipelines=(),
            denial_message="Project reload is not permitted",
        )
        outcome: CompileOutcome = state.reload_guarded(guard=guard)
        reporter.report_reload(outcome=outcome)
        return build_status_payload(
            outcome=outcome,
            warehouse_status=warehouse.status(),
        )

    def read_definitions(*, request: Request) -> Response:
        try:
            outcome: CompileOutcome = state.current_servable_outcome()
            analysis: CompileAnalysis = state.current_analysis()
        except ProjectNotCompiledError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        etag: str = f'"{outcome.version_key}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(
            content=build_definitions_payload(analysis=analysis, version_key=outcome.version_key),
            headers={"ETag": etag},
        )

    def read_state() -> dict[str, object]:
        if warehouse.connection is None or database is None:
            raise HTTPException(
                status_code=_HTTP_SERVICE_UNAVAILABLE,
                detail="no warehouse connection",
            )
        try:
            return state.snapshot.current()
        except ProjectNotCompiledError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def _required_connection() -> AdapterConnection:
        connection: AdapterConnection | None = warehouse.connection
        if connection is None or database is None:
            raise HTTPException(
                status_code=_HTTP_SERVICE_UNAVAILABLE,
                detail="no warehouse connection",
            )
        return connection

    @contextmanager
    def _required_read_connection() -> Iterator[AdapterConnection]:
        with warehouse.read_connection() as connection:
            if connection is None or database is None:
                raise HTTPException(
                    status_code=_HTTP_SERVICE_UNAVAILABLE,
                    detail="no warehouse read connection",
                )
            yield connection

    def _servable_analysis() -> CompileAnalysis:
        try:
            return state.current_analysis()
        except ProjectNotCompiledError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error

    connections: _RouteConnections = _RouteConnections(
        required=_required_connection,
        read=_required_read_connection,
    )

    def read_topics() -> dict[str, object]:
        analysis: CompileAnalysis = _servable_analysis()
        connection: AdapterConnection | None = warehouse.connection
        try:
            with state.query_lock:
                return build_topics_payload(
                    analysis=analysis,
                    connection=connection,
                    database=database,
                    topic_reader=broker_readers[1],
                    kafka_lag_reader=broker_readers[0],
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.get("/api/status")(read_status)
    app.post("/api/warehouse/refresh")(refresh_warehouse)
    app.get("/api/state")(read_state)
    _register_plan_route(
        app=app,
        database=database,
        execution_context=execution_context,
        read_connection=_required_read_connection,
        servable_analysis=_servable_analysis,
    )
    app.post("/api/reload")(reload_project)
    _register_access_routes(
        app=app,
        servable_analysis=_servable_analysis,
        authorization=authorization_context,
    )
    app.get("/api/definitions")(read_definitions)
    app.get("/api/topics")(read_topics)
    _register_deployment_routes(
        app=app,
        state=state,
        database=database,
        authorization=authorization_context,
        connections=connections,
        servable_analysis=_servable_analysis,
    )
    _ = register_destruction_routes(
        app=app,
        state=state,
        warehouse=warehouse,
        database=database,
        project_dir=project_dir,
        authorization=authorization_context,
        servable_analysis=_servable_analysis,
        store=control_stores.destruction_plans,
    )
    _register_message_routes(
        app=app,
        state=state,
        database=database,
        authorization=authorization_context,
        required_connection=_required_connection,
        servable_analysis=_servable_analysis,
    )
    _ = register_sensor_routes(
        app=app,
        state=state,
        database=database,
        sensor_scheduler=schedulers[1],
        authorization=authorization_context,
        read_connection=connections.read,
        servable_analysis=_servable_analysis,
    )
    return _register_quality_routes(
        app=app,
        state=state,
        database=database,
        authorization=authorization_context,
        builds=builds,
        audit_scheduler=schedulers[0],
        reporter=reporter,
        connections=connections,
        servable_analysis=_servable_analysis,
        presumed_failed_after_seconds=execution_context.run_presumed_failed_after_seconds,
    )


def _register_plan_route(
    *,
    app: FastAPI,
    database: str | None,
    execution_context: DevExecutionContext,
    read_connection: Callable[[], AbstractContextManager[AdapterConnection]],
    servable_analysis: Callable[[], CompileAnalysis],
) -> None:
    """Attach the mode-aware plan preview route."""

    def read_plan(
        *,
        select: Annotated[list[str] | None, Query()] = None,
        start: Annotated[str | None, Query()] = None,
        deployment: Annotated[str | None, Query()] = None,
        counts: Annotated[bool, Query()] = False,
        changed: Annotated[bool, Query()] = False,
        include_missing_upstream: Annotated[bool, Query()] = False,
    ) -> dict[str, object]:
        analysis: CompileAnalysis = servable_analysis()
        request_started_ns: int = monotonic_ns()
        try:
            if start is not None and not select and not changed:
                raise CliUserError("--start-time requires --changed or at least one --select")
            with read_connection() as client:
                connection_acquired_ns: int = monotonic_ns()
                planned_at: str = client.capture_warehouse_timestamp()
                preparation: (
                    DirectWorkflowPreparation
                    | MixedWorkflowPreparation
                    | VirtualWorkflowPreparation
                ) = prepare_build_workflow(
                    analysis=analysis,
                    options=WorkflowPreparationOptions(
                        database=database,
                        metadata_database=database,
                        selectors=tuple(select or ()),
                        deployment_id=deployment,
                        full_refresh=False,
                        start_time=start,
                        verbose=False,
                        changed=changed,
                        include_missing_upstream=include_missing_upstream,
                    ),
                    client=client,
                    adapter_profile=analysis.adapter_profile,
                )
                validate_build_pipeline_limit(analysis=analysis, preparation=preparation)
                direct: DirectWorkflowPreparation | None = _direct_preparation(preparation)
                resolved_deployment_id: str | None = _resolved_deployment_id(preparation)
                _, command = build_invocation(
                    selectors=tuple(select or ()),
                    start_time=start,
                    changed=changed,
                    include_missing_upstream=include_missing_upstream,
                    deployment_id=resolved_deployment_id,
                    execution_context=execution_context,
                )
                planning_completed_ns: int = monotonic_ns()
                replay_row_counts: dict[str, int | None] = {}
                if counts and direct is not None:
                    replay_row_counts = count_replay_rows(
                        connection=client,
                        database=direct.preview.database,
                        plan=direct.preview.plan,
                        start_time=direct.preview.effective_start_time,
                    )
                payload: dict[str, object] = build_mode_aware_plan_payload(
                    preparation=preparation,
                    analysis=analysis,
                    selectors=tuple(select or ()),
                    planned_at=planned_at,
                    command=command,
                    replay_row_counts=replay_row_counts,
                )
                completed_ns: int = monotonic_ns()
                payload["timings"] = {
                    "connectionWaitMs": (connection_acquired_ns - request_started_ns) // 1_000_000,
                    "planningMs": (planning_completed_ns - connection_acquired_ns) // 1_000_000,
                    "replayCountsMs": (completed_ns - planning_completed_ns) // 1_000_000,
                    "totalMs": (completed_ns - request_started_ns) // 1_000_000,
                }
                return payload
        except (
            BackfillExecutionError,
            CliUserError,
            DirectBuildError,
            DirectPlanError,
            ValueError,
        ) as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.get("/api/plan")(read_plan)


def _register_access_routes(
    *,
    app: FastAPI,
    servable_analysis: Callable[[], CompileAnalysis],
    authorization: OperationAuthorizationContext,
) -> FastAPI:
    """Attach the capability and read-only compiled-policy routes."""

    def read_capabilities(request: Request) -> dict[str, object]:
        return build_capabilities_payload(
            analysis=_optional_servable_analysis(servable_analysis=servable_analysis),
            request=request,
            context=authorization,
        )

    def read_access_policy() -> dict[str, object]:
        return build_access_policy_payload(
            analysis=_optional_servable_analysis(servable_analysis=servable_analysis)
        )

    app.get("/api/auth/capabilities")(read_capabilities)
    app.get("/api/access-policy")(read_access_policy)
    return app


def _register_deployment_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    database: str | None,
    authorization: OperationAuthorizationContext,
    connections: _RouteConnections,
    servable_analysis: Callable[[], CompileAnalysis],
) -> FastAPI:
    """Attach the deployment inventory, detail and lifecycle routes."""

    project_dir: Path = authorization.project_dir

    def read_deployments() -> dict[str, object]:
        try:
            with connections.read() as client:
                return build_deployments_payload(
                    connection=client,
                    database=database or "",
                    metadata_database=database or "",
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_one_deployment(*, deployment_id: str) -> dict[str, object]:
        try:
            with connections.read() as client:
                payload: dict[str, object] | None = build_deployment_detail_payload(
                    connection=client,
                    database=database or "",
                    metadata_database=database or "",
                    deployment_id=deployment_id,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        if payload is None:
            raise HTTPException(
                status_code=_HTTP_NOT_FOUND,
                detail=f"deployment '{deployment_id}' was not found",
            )
        return payload

    def read_deployment_diff(
        *, deployment_id: str, against: Annotated[str | None, Query()] = None
    ) -> dict[str, object]:
        comparison: str = deployment_id if against is None else f"{against}:{deployment_id}"
        try:
            with connections.read() as client:
                return build_deployment_diff_payload(
                    connection=client,
                    database=database or "",
                    metadata_database=database or "",
                    comparison=comparison,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error

    def promote_deployment(*, http_request: Request, deployment_id: str) -> dict[str, object]:
        client: AdapterConnection = connections.required()
        if not is_system_admin(request=http_request):
            analysis: CompileAnalysis = servable_analysis()
            with state.query_lock:
                logical_ids: tuple[str, ...] = promotion_executed_logical_ids(
                    connection=client,
                    metadata_database=database or "",
                    deployment_id=deployment_id,
                )
            require_promotion_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                deployment_id=deployment_id,
                logical_ids=logical_ids,
            )
        try:
            with state.query_lock:
                return run_deployment_promotion(
                    connection=client,
                    database=database or "",
                    metadata_database=database or "",
                    deployment_id=deployment_id,
                    project_dir=project_dir,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error

    def cleanup_deployments(
        *, http_request: Request, request: DeploymentCleanupRequest
    ) -> dict[str, object]:
        require_cleanup_authorization(
            analysis=_optional_servable_analysis(servable_analysis=servable_analysis),
            request=http_request,
            context=authorization,
        )
        client: AdapterConnection = connections.required()
        try:
            with state.query_lock:
                return run_deployment_cleanup(
                    connection=client,
                    database=database or "",
                    metadata_database=database or "",
                    retention_days=request.retentionDays,
                    project_dir=project_dir,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error

    app.get("/api/deployments")(read_deployments)
    app.get("/api/deployments/{deployment_id}")(read_one_deployment)
    app.get("/api/deployments/{deployment_id}/diff")(read_deployment_diff)
    app.post("/api/deployments/{deployment_id}/promote")(promote_deployment)
    app.post("/api/deployments/cleanup")(cleanup_deployments)
    return app


def _register_message_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    database: str | None,
    authorization: OperationAuthorizationContext,
    required_connection: Callable[[], AdapterConnection],
    servable_analysis: Callable[[], CompileAnalysis],
) -> FastAPI:
    """Attach the warehouse-backed source message browsing routes."""

    def _authorized_analysis(*, http_request: Request) -> CompileAnalysis:
        analysis: CompileAnalysis = servable_analysis()
        require_message_read_authorization(
            analysis=analysis,
            request=http_request,
            context=authorization,
        )
        return analysis

    def _browsable_relation_name(*, analysis: CompileAnalysis, source_name: str) -> str:
        source: CompiledSource | None = next(
            (
                candidate
                for candidate in analysis.compiled_project.sources
                if candidate.key.name == source_name
            ),
            None,
        )
        if source is None:
            raise HTTPException(
                status_code=_HTTP_NOT_FOUND,
                detail=f"unknown source '{source_name}'",
            )
        if not isinstance(source.source, KafkaLandingStep):
            raise HTTPException(
                status_code=_HTTP_BAD_REQUEST,
                detail=f"source '{source_name}' is not a managed Kafka source",
            )
        return analysis.realized_project.relation_name_by_logical_key[source.key]

    def read_messages(
        *, http_request: Request, name: str, request: MessagesQueryRequest
    ) -> dict[str, object]:
        analysis: CompileAnalysis = _authorized_analysis(http_request=http_request)
        client: AdapterConnection = required_connection()
        relation_name: str = _browsable_relation_name(analysis=analysis, source_name=name)
        try:
            with state.query_lock:
                ensure_header_columns(
                    connection=client, database=database or "", relation_name=relation_name
                )
                return read_source_messages(
                    connection=client,
                    database=database or "",
                    relation_name=relation_name,
                    request=request,
                )
        except MessageQueryValidationError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except MessageSchemaError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_message_record(
        *, http_request: Request, name: str, request: MessageRecordRequest
    ) -> dict[str, object]:
        analysis: CompileAnalysis = _authorized_analysis(http_request=http_request)
        client: AdapterConnection = required_connection()
        relation_name: str = _browsable_relation_name(analysis=analysis, source_name=name)
        try:
            with state.query_lock:
                ensure_header_columns(
                    connection=client, database=database or "", relation_name=relation_name
                )
                record: dict[str, object] | None = read_source_message_record(
                    connection=client,
                    database=database or "",
                    relation_name=relation_name,
                    partition=request.partition,
                    offset=request.offset,
                )
        except MessageQueryValidationError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except MessageSchemaError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        if record is None:
            raise HTTPException(
                status_code=_HTTP_NOT_FOUND,
                detail=(
                    f"no message at partition {request.partition} "
                    f"offset {request.offset} in '{name}'"
                ),
            )
        return record

    def read_message_facets(
        *, http_request: Request, name: str, request: MessageFacetsRequest
    ) -> dict[str, object]:
        analysis: CompileAnalysis = _authorized_analysis(http_request=http_request)
        client: AdapterConnection = required_connection()
        relation_name: str = _browsable_relation_name(analysis=analysis, source_name=name)
        try:
            with state.query_lock:
                ensure_header_columns(
                    connection=client, database=database or "", relation_name=relation_name
                )
                return read_source_message_facets(
                    connection=client,
                    database=database or "",
                    relation_name=relation_name,
                    request=request,
                )
        except MessageQueryValidationError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except MessageSchemaError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.post("/api/sources/{name}/messages")(read_messages)
    app.post("/api/sources/{name}/messages/record")(read_message_record)
    app.post("/api/sources/{name}/messages/facets")(read_message_facets)
    return app


def _run_audit_batch_request(
    *,
    http_request: Request,
    request: AuditBatchRunRequest,
    analysis: CompileAnalysis,
    connection: AdapterConnection,
    state: DevServerState,
    authorization: OperationAuthorizationContext,
    database: str,
) -> list[dict[str, object]]:
    for name in request.names:
        _ = require_check_authorization(
            analysis=analysis,
            request=http_request,
            store=authorization.store,
            project_dir=authorization.project_dir,
            selected_target=authorization.selected_target,
            kind="audit",
            name=name,
        )
    try:
        with state.query_lock:
            return run_audit_batch(
                analysis=analysis,
                connection=connection,
                names=tuple(request.names),
                project_dir=authorization.project_dir,
                database=database,
            )
    except DevServerError as error:
        raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
    except AdapterError as error:
        raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error


def _register_quality_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    database: str | None,
    authorization: OperationAuthorizationContext,
    builds: BuildProcessManager,
    audit_scheduler: AuditScheduler,
    reporter: DevServerReporter,
    connections: _RouteConnections,
    servable_analysis: Callable[[], CompileAnalysis],
    presumed_failed_after_seconds: int,
) -> FastAPI:
    """Attach the checks, run-history, and build routes."""

    project_dir: Path = authorization.project_dir

    def run_check(*, http_request: Request, request: ChecksRunRequest) -> dict[str, object]:
        analysis: CompileAnalysis = servable_analysis()
        runners: dict[str, Callable[..., dict[str, object]]] = {
            "audit": run_one_audit,
            "test": run_one_test,
        }
        runner: Callable[..., dict[str, object]] | None = runners.get(request.kind)
        if runner is None:
            raise HTTPException(
                status_code=_HTTP_BAD_REQUEST,
                detail="kind must be 'audit' or 'test'",
            )
        _ = require_check_authorization(
            analysis=analysis,
            request=http_request,
            store=authorization.store,
            project_dir=project_dir,
            selected_target=authorization.selected_target,
            kind=request.kind,
            name=request.name,
        )
        client: AdapterConnection = connections.required()
        try:
            with state.query_lock:
                payload: dict[str, object] = runner(
                    analysis=analysis,
                    connection=client,
                    name=request.name,
                    project_dir=project_dir,
                    database=database or "",
                )
        except DevServerError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        deferred: bool = payload.get("deferredUntil") is not None
        passed: bool = bool(payload.get("passed"))
        reporter.report_activity(
            category=request.kind,
            status="deferred" if deferred else ("pass" if passed else "fail"),
            tone=(
                ActivityTone.CAUTION
                if deferred
                else (ActivityTone.GOOD if passed else ActivityTone.BAD)
            ),
            detail=request.name,
        )
        return payload

    def run_audits(
        *, http_request: Request, request: AuditBatchRunRequest
    ) -> list[dict[str, object]]:
        return _run_audit_batch_request(
            http_request=http_request,
            request=request,
            analysis=servable_analysis(),
            connection=connections.required(),
            state=state,
            authorization=authorization,
            database=database or "",
        )

    def read_checks_status() -> list[dict[str, object]]:
        client: AdapterConnection = connections.required()
        analysis: CompileAnalysis = servable_analysis()
        try:
            with state.query_lock:
                return build_checks_status_payload(
                    analysis=analysis,
                    connection=client,
                    database=database or "",
                    project_dir=project_dir,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_audit_scheduler() -> dict[str, object]:
        client: AdapterConnection = connections.required()
        health: dict[str, object] = audit_scheduler.health()
        if health["state"] in {
            AuditScheduleState.RUNNING,
            AuditScheduleState.BACKING_OFF,
        }:
            return {
                "enabled": True,
                "state": health["state"],
                "warehouseNow": health["lastSuccessfulTick"],
                "dueCount": health["runningAuditCount"],
                "audits": [],
                "health": health,
            }
        analysis: CompileAnalysis = servable_analysis()
        try:
            with state.query_lock:
                payload: dict[str, object] = build_audit_scheduler_payload(
                    analysis=analysis,
                    connection=client,
                    database=database or "",
                    project_dir=project_dir,
                )
                payload["health"] = health
                return payload
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_run_history() -> list[dict[str, object]]:
        try:
            with connections.read() as client:
                return read_runs(
                    connection=client,
                    database=database or "",
                    presumed_failed_after_seconds=presumed_failed_after_seconds,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_one_run_events(
        *, invocation_id: str, after: Annotated[int, Query(ge=0)] = 0
    ) -> dict[str, object]:
        try:
            with connections.read() as client:
                return read_run_events(
                    connection=client,
                    database=database or "",
                    invocation_id=invocation_id,
                    after=after,
                    presumed_failed_after_seconds=presumed_failed_after_seconds,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_one_run_statement(*, invocation_id: str, statement_sequence: int) -> dict[str, object]:
        try:
            with connections.read() as client:
                return read_run_statement(
                    connection=client,
                    database=database or "",
                    invocation_id=invocation_id,
                    statement_sequence=statement_sequence,
                )
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.post("/api/checks/run")(run_check)
    app.post("/api/audits/run")(run_audits)
    app.get("/api/checks/status")(read_checks_status)
    app.get("/api/audit-scheduler")(read_audit_scheduler)
    app.get("/api/runs")(read_run_history)
    app.get("/api/runs/{invocation_id}/events")(read_one_run_events)
    app.get("/api/runs/{invocation_id}/statements/{statement_sequence}")(read_one_run_statement)
    return _register_build_routes(
        app=app,
        builds=builds,
        authorization=authorization,
        required_connection=connections.required,
        database=database or "",
        state=state,
        servable_analysis=servable_analysis,
        presumed_failed_after_seconds=presumed_failed_after_seconds,
    )


def _register_build_routes(
    *,
    app: FastAPI,
    builds: BuildProcessManager,
    authorization: OperationAuthorizationContext,
    required_connection: Callable[[], AdapterConnection],
    database: str,
    state: DevServerState,
    servable_analysis: Callable[[], CompileAnalysis],
    presumed_failed_after_seconds: int,
) -> FastAPI:
    """Attach the execute and live-feed routes."""

    project_dir: Path = authorization.project_dir

    def start_build(*, http_request: Request, request: BuildRunRequest) -> dict[str, object]:
        try:
            with state.query_lock:
                client: AdapterConnection = required_connection()
                analysis: CompileAnalysis = servable_analysis()
                needs_authorization: bool = not is_system_admin(request=http_request)
                dynamic_selection: bool = request.changed or request.includeMissingUpstream
                if not dynamic_selection:
                    validate_build_pipeline_limit(
                        analysis=analysis,
                        selectors=tuple(request.selectors),
                    )
                active_runs: list[dict[str, object]] = [
                    run
                    for run in read_active_runs(
                        connection=client,
                        database=database,
                        presumed_failed_after_seconds=presumed_failed_after_seconds,
                    )
                    if run["status"]
                    in {
                        RunPresentationStatus.RUNNING,
                        RunPresentationStatus.UNRESPONSIVE,
                    }
                ]
                preparation: (
                    DirectWorkflowPreparation
                    | MixedWorkflowPreparation
                    | VirtualWorkflowPreparation
                    | None
                ) = None
                if needs_authorization or active_runs or dynamic_selection:
                    preparation = prepare_build_workflow(
                        analysis=analysis,
                        options=WorkflowPreparationOptions(
                            database=database,
                            metadata_database=database,
                            selectors=tuple(request.selectors),
                            deployment_id=request.deploymentId,
                            full_refresh=False,
                            start_time=request.startTime,
                            verbose=False,
                            changed=request.changed,
                            include_missing_upstream=request.includeMissingUpstream,
                        ),
                        client=client,
                        adapter_profile=analysis.adapter_profile,
                    )
                if dynamic_selection and preparation is not None:
                    validate_build_pipeline_limit(
                        analysis=analysis,
                        preparation=preparation,
                    )
                if needs_authorization and preparation is not None:
                    require_prepared_build_authorization(
                        analysis=analysis,
                        request=http_request,
                        context=authorization,
                        preparation=preparation,
                    )
                blocking_run: dict[str, object] | None = None
                if active_runs and preparation is not None:
                    requested_writes, requested_reads = _preparation_logical_scopes(preparation)
                    blocking_run = next(
                        (
                            run
                            for run in active_runs
                            if _run_overlaps_requested_scope(
                                run=run,
                                requested_writes=requested_writes,
                                requested_reads=requested_reads,
                            )
                        ),
                        None,
                    )
                if blocking_run is not None:
                    raise BuildInProgressError(
                        _blocking_run_message(
                            run=blocking_run,
                            presumed_failed_after_seconds=presumed_failed_after_seconds,
                        )
                    )
                expected_write_scope: frozenset[str] | None = None
                expected_read_scope: frozenset[str] | None = None
                expected_pipeline_scope: frozenset[str] | None = None
                if preparation is not None:
                    expected_write_scope, expected_read_scope = resolve_prepared_build_scope(
                        preparation
                    )
                    expected_pipeline_scope = prepared_pipeline_scope(
                        preparation=preparation,
                        analysis=analysis,
                    )
                return builds.start(
                    project_dir=project_dir,
                    selectors=tuple(request.selectors),
                    start_time=request.startTime,
                    changed=request.changed,
                    include_missing_upstream=request.includeMissingUpstream,
                    deployment_id=request.deploymentId,
                    confirmations=tuple(request.confirmations),
                    expected_write_scope=expected_write_scope,
                    expected_read_scope=expected_read_scope,
                    expected_pipeline_scope=expected_pipeline_scope,
                )
        except BuildInProgressError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except BuildStartError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except (
            BackfillExecutionError,
            CliUserError,
            DirectBuildError,
            DirectPlanError,
            ValueError,
        ) as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_build_feed(*, after: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]:
        return builds.feed(after=after)

    def cancel_build(*, http_request: Request, request: dict[str, str]) -> dict[str, object]:
        invocation_id: str = request.get("invocationId", "")
        if not is_system_admin(request=http_request):
            client: AdapterConnection = required_connection()
            analysis: CompileAnalysis = servable_analysis()
            with state.query_lock:
                active_runs: list[dict[str, object]] = read_active_runs(
                    connection=client,
                    database=database,
                    presumed_failed_after_seconds=presumed_failed_after_seconds,
                )
            require_run_cancel_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                invocation_id=invocation_id,
                active_runs=active_runs,
            )
        try:
            return builds.cancel(invocation_id=invocation_id)
        except BuildInProgressError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error

    def kill_build(*, http_request: Request, request: dict[str, str]) -> dict[str, object]:
        require_kill_authorization(
            analysis=_optional_servable_analysis(servable_analysis=servable_analysis),
            request=http_request,
            context=authorization,
        )
        try:
            return builds.kill(invocation_id=request.get("invocationId", ""))
        except BuildInProgressError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error

    app.post("/api/build")(start_build)
    app.get("/api/build/current")(read_build_feed)
    app.post("/api/build/cancel")(cancel_build)
    app.post("/api/build/kill")(kill_build)
    return app


def _optional_servable_analysis(
    *, servable_analysis: Callable[[], CompileAnalysis]
) -> CompileAnalysis | None:
    try:
        return servable_analysis()
    except HTTPException:
        return None


def _blocking_run_message(*, run: dict[str, object], presumed_failed_after_seconds: int) -> str:
    invocation_id: object = run["invocationId"]
    status: object = run["status"]
    signal_age_seconds: int = int(str(run["lastSignalAgeSeconds"]))
    if status == RunPresentationStatus.UNRESPONSIVE:
        retry_seconds: int = max(presumed_failed_after_seconds - signal_age_seconds, 0)
        return (
            f"Run {invocation_id} is unresponsive: no signal for {signal_age_seconds}s. "
            "No new run was started. To prevent overlapping warehouse writes, SQLTide "
            f"will wait {retry_seconds}s more before treating it as presumed failed "
            f"(configured safety window: {presumed_failed_after_seconds}s via "
            "defaults.run_presumed_failed_after). Retry after that."
        )
    return (
        f"Run {invocation_id} is still {status} (last signal {signal_age_seconds}s ago), "
        "so no new run was started. Wait for it to finish or cancel it from Runs."
    )


def _direct_preparation(
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
) -> DirectWorkflowPreparation | None:
    if isinstance(preparation, DirectWorkflowPreparation):
        return preparation
    if isinstance(preparation, MixedWorkflowPreparation):
        return preparation.direct
    return None


def _resolved_deployment_id(
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
) -> str | None:
    if isinstance(preparation, VirtualWorkflowPreparation):
        return preparation.preview.deployment_id
    if isinstance(preparation, MixedWorkflowPreparation):
        return preparation.virtual.preview.deployment_id
    return None


def _preparation_logical_scopes(
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
) -> tuple[frozenset[str], frozenset[str]]:
    return resolve_prepared_build_scope(preparation)


def _run_overlaps_requested_scope(
    *,
    run: dict[str, object],
    requested_writes: frozenset[str],
    requested_reads: frozenset[str],
) -> bool:
    """Conservatively detect read/write conflicts between two direct build plans."""

    active_writes: frozenset[str] | None = _logical_id_scope(run.get("executedLogicalIds"))
    active_reads: frozenset[str] | None = _logical_id_scope(run.get("contextLogicalIds"))
    if active_writes is None or active_reads is None:
        return True
    return bool(
        (active_writes & requested_writes)
        or (active_writes & requested_reads)
        or (active_reads & requested_writes)
    )


def _logical_id_scope(value: object) -> frozenset[str] | None:
    if not isinstance(value, list):
        return None
    scope: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        scope.add(item)
    return frozenset(scope)
