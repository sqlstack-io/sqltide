"""Frozen-plan API for recorded pipeline destruction and target reset."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from sqltide._native import execute_destruction, list_inactive_pipelines, plan_destruction
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import (
    AdapterError,
    AdapterTargetMutationLockError,
)
from sqltide.auth.main.read_authenticated_request import read_authenticated_request
from sqltide.auth.models import AuthenticatedRequest
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server._helpers.queries.runs_query import read_destruction_recovery_run
from sqltide.dev_server._helpers.server.authorization_enforcement import (
    require_destruction_authorization,
)
from sqltide.dev_server.classes.dev_server_state import DevServerState
from sqltide.dev_server.classes.warehouse_runtime import WarehouseRuntime
from sqltide.dev_server.models import (
    CompileOutcome,
    DestructionExecutionRequest,
    DestructionPlanRequest,
    OperationAuthorizationContext,
)
from sqltide.executor.destruction.exceptions import (
    DestructionChallengeError,
    DestructionDependencyError,
    DestructionDriftError,
    DestructionExternalDependencyError,
    DestructionPlanCorruptError,
    DestructionPlanExpiredError,
    DestructionPlanNotFoundError,
    DestructionPlanNotReviewedError,
    DestructionRecordingError,
    DestructionRecoveryError,
    DestructionRecoveryNotFoundError,
    DestructionResourceError,
    DestructionSelectionError,
)
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionPlan,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.destruction.types import DestructionOperation, DestructionPlanStore
from sqltide.executor.observability.main.logical_project_identity import (
    logical_project_identity,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)
_HTTP_ACCEPTED: int = 202
_HTTP_BAD_REQUEST: int = 400
_HTTP_NOT_FOUND: int = 404
_HTTP_CONFLICT: int = 409
_HTTP_GONE: int = 410
_HTTP_BAD_GATEWAY: int = 502
_HTTP_SERVICE_UNAVAILABLE: int = 503
_DESTRUCTIVE_MODE: str = "destructive"
_FAILED_OUTCOME: str = "failed"


class _DestructionDispatchRegistry:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._plan_ids: set[str] = set()

    def reserve(self, *, plan_id: str) -> bool:
        with self._lock:
            if plan_id in self._plan_ids:
                return False
            self._plan_ids.add(plan_id)
            return True

    def release(self, *, plan_id: str) -> None:
        with self._lock:
            self._plan_ids.discard(plan_id)


def register_destruction_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    warehouse: WarehouseRuntime,
    database: str | None,
    project_dir: Path,
    authorization: OperationAuthorizationContext,
    servable_analysis: Callable[[], CompileAnalysis],
    store: DestructionPlanStore,
) -> FastAPI:
    """Register the durable, actor-bound single-use frozen-plan service."""

    _ = _register_destruction_plan_routes(
        app=app,
        state=state,
        warehouse=warehouse,
        database=database,
        authorization=authorization,
        servable_analysis=servable_analysis,
        store=store,
    )
    _ = _register_destruction_recovery_route(
        app=app,
        state=state,
        warehouse=warehouse,
        database=database,
        project_dir=project_dir,
        authorization=authorization,
        servable_analysis=servable_analysis,
        store=store,
    )
    return _register_destruction_execution_route(
        app=app,
        state=state,
        warehouse=warehouse,
        database=database,
        project_dir=project_dir,
        authorization=authorization,
        servable_analysis=servable_analysis,
        store=store,
    )


def _register_destruction_plan_routes(
    *,
    app: FastAPI,
    state: DevServerState,
    warehouse: WarehouseRuntime,
    database: str | None,
    authorization: OperationAuthorizationContext,
    servable_analysis: Callable[[], CompileAnalysis],
    store: DestructionPlanStore,
) -> FastAPI:
    """Attach frozen-plan creation, inspection, and review routes."""

    def create_plan(*, http_request: Request, request: DestructionPlanRequest) -> dict[str, object]:
        actor: AuthenticatedRequest = _actor(http_request)
        analysis: CompileAnalysis = servable_analysis()
        target: str = analysis.compiled_project.target_name or authorization.selected_target or ""
        connection: AdapterConnection = _required_connection(warehouse=warehouse, database=database)
        requested_names: tuple[str, ...] = tuple(request.pipelineNames)
        included_names: tuple[str, ...] = tuple(request.includedDependentPipelineNames)
        require_destruction_authorization(
            analysis=analysis,
            request=http_request,
            context=authorization,
            operation=request.operation,
            affected_pipelines=requested_names,
        )
        try:
            with state.query_lock:
                plan: DestructionPlan = plan_destruction(
                    request=DestructionRequest(
                        operation=request.operation,
                        target=target,
                        database=database or "",
                        metadata_database=database or "",
                        pipeline_names=requested_names,
                        included_dependent_pipeline_names=included_names,
                    ),
                    analysis=analysis,
                    connection=connection,
                )
            require_destruction_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                operation=request.operation,
                affected_pipelines=plan.affected_pipeline_names,
            )
            store.save(plan=plan, actor=str(actor.principal.user_id))
            return _plan_payload(plan=plan)
        except DestructionDependencyError as error:
            with state.query_lock:
                blocked_plan: DestructionPlan = plan_destruction(
                    request=DestructionRequest(
                        operation=request.operation,
                        target=target,
                        database=database or "",
                        metadata_database=database or "",
                        pipeline_names=requested_names,
                        included_dependent_pipeline_names=tuple(
                            sorted({*included_names, *error.dependent_pipeline_names})
                        ),
                    ),
                    analysis=analysis,
                    connection=connection,
                )
            require_destruction_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                operation=request.operation,
                affected_pipelines=blocked_plan.affected_pipeline_names,
            )
            return {
                **_plan_payload(plan=blocked_plan),
                "selectedPipelines": list(requested_names),
                "requiredDependentPipelines": list(error.dependent_pipeline_names),
                "blocked": True,
            }
        except DestructionExternalDependencyError as error:
            raise HTTPException(
                status_code=_HTTP_CONFLICT,
                detail={
                    "message": str(error),
                    "blockingRelations": list(error.relation_names),
                },
            ) from error
        except DestructionResourceError as error:
            raise HTTPException(
                status_code=_HTTP_CONFLICT,
                detail={"message": str(error), "reason": "resource_conflict"},
            ) from error
        except (DestructionSelectionError, ValueError) as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_inactive_pipelines(*, http_request: Request) -> dict[str, object]:
        analysis: CompileAnalysis = servable_analysis()
        target: str = analysis.compiled_project.target_name or authorization.selected_target or ""
        destruction_request: DestructionRequest = DestructionRequest(
            operation=DestructionOperation.DESTROY_PIPELINES,
            target=target,
            database=database or "",
            metadata_database=database or "",
        )
        require_destruction_authorization(
            analysis=analysis,
            request=http_request,
            context=authorization,
            operation=DestructionOperation.DESTROY_PIPELINES,
            affected_pipelines=(),
        )
        connection: AdapterConnection = _required_connection(warehouse=warehouse, database=database)
        try:
            with state.query_lock:
                pipelines: tuple[InactivePipeline, ...] = list_inactive_pipelines(
                    request=destruction_request,
                    analysis=analysis,
                    connection=connection,
                )
            require_destruction_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                operation=DestructionOperation.DESTROY_PIPELINES,
                affected_pipelines=tuple(pipeline.name for pipeline in pipelines),
            )
            return {
                "pipelines": [
                    {
                        "name": pipeline.name,
                        "modelCount": pipeline.model_count,
                        "resourceCount": pipeline.resource_count,
                        "lastPublishedAt": pipeline.last_published_at,
                    }
                    for pipeline in pipelines
                ]
            }
        except DestructionResourceError as error:
            raise HTTPException(
                status_code=_HTTP_CONFLICT,
                detail={"message": str(error), "reason": "resource_conflict"},
            ) from error
        except (DestructionSelectionError, ValueError) as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    def read_plan(*, http_request: Request, plan_id: str) -> dict[str, object]:
        actor: AuthenticatedRequest = _actor(http_request)
        actor_id: str = str(actor.principal.user_id)
        try:
            plan: DestructionPlan = store.get(plan_id=plan_id, actor=actor_id)
            try:
                reviewed_at: datetime | None = store.reviewed_at(plan_id=plan_id, actor=actor_id)
            except DestructionPlanNotReviewedError:
                reviewed_at = None
        except DestructionPlanExpiredError as error:
            raise HTTPException(status_code=_HTTP_GONE, detail=str(error)) from error
        except DestructionPlanNotFoundError as error:
            raise HTTPException(status_code=_HTTP_NOT_FOUND, detail=str(error)) from error
        except DestructionPlanCorruptError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        require_destruction_authorization(
            analysis=servable_analysis(),
            request=http_request,
            context=authorization,
            operation=plan.operation.value,
            affected_pipelines=plan.affected_pipeline_names,
        )
        return _plan_payload(plan=plan, reviewed_at=reviewed_at)

    def review_plan(*, http_request: Request, plan_id: str) -> dict[str, object]:
        actor: AuthenticatedRequest = _actor(http_request)
        actor_id: str = str(actor.principal.user_id)
        try:
            plan: DestructionPlan = store.get(plan_id=plan_id, actor=actor_id)
            _authorize_plan(
                plan=plan,
                analysis=servable_analysis(),
                database=database,
                http_request=http_request,
                authorization=authorization,
            )
            reviewed_at: datetime = store.mark_reviewed(plan_id=plan_id, actor=actor_id)
        except DestructionPlanExpiredError as error:
            raise HTTPException(status_code=_HTTP_GONE, detail=str(error)) from error
        except DestructionPlanNotFoundError as error:
            raise HTTPException(status_code=_HTTP_NOT_FOUND, detail=str(error)) from error
        except DestructionPlanCorruptError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except DestructionDriftError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        return _plan_payload(plan=plan, reviewed_at=reviewed_at)

    app.get("/api/destruction/pipelines/inactive")(read_inactive_pipelines)
    app.post("/api/destruction/plans")(create_plan)
    app.get("/api/destruction/plans/{plan_id}")(read_plan)
    app.post("/api/destruction/plans/{plan_id}/review")(review_plan)
    return app


def _register_destruction_recovery_route(
    *,
    app: FastAPI,
    state: DevServerState,
    warehouse: WarehouseRuntime,
    database: str | None,
    project_dir: Path,
    authorization: OperationAuthorizationContext,
    servable_analysis: Callable[[], CompileAnalysis],
    store: DestructionPlanStore,
) -> FastAPI:
    """Attach server-authoritative failed-run recovery planning."""

    def create_recovery_plan(*, http_request: Request, invocation_id: str) -> dict[str, object]:
        actor: AuthenticatedRequest = _actor(http_request)
        analysis: CompileAnalysis = servable_analysis()
        connection: AdapterConnection = _required_connection(warehouse=warehouse, database=database)
        try:
            with warehouse.read_connection() as read_connection:
                if read_connection is None or database is None:
                    raise HTTPException(
                        status_code=_HTTP_SERVICE_UNAVAILABLE,
                        detail="no warehouse read connection",
                    )
                run: dict[str, object] | None = read_destruction_recovery_run(
                    connection=read_connection,
                    database=database,
                    invocation_id=invocation_id,
                )
            if run is None:
                raise HTTPException(status_code=_HTTP_NOT_FOUND, detail="failed run was not found")
            recovery_request: DestructionRequest = _recovery_request_from_run(
                run=run,
                analysis=analysis,
                database=database,
                project_dir=project_dir,
                authorization=authorization,
            )
            explicit_scope: tuple[str, ...] = tuple(
                sorted(
                    {
                        *recovery_request.pipeline_names,
                        *recovery_request.included_dependent_pipeline_names,
                    }
                )
            )
            require_destruction_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                operation=str(recovery_request.operation),
                affected_pipelines=explicit_scope,
            )
            with state.query_lock:
                plan: DestructionPlan = plan_destruction(
                    request=recovery_request,
                    analysis=analysis,
                    connection=connection,
                )
            require_destruction_authorization(
                analysis=analysis,
                request=http_request,
                context=authorization,
                operation=str(recovery_request.operation),
                affected_pipelines=plan.affected_pipeline_names,
            )
            store.save(plan=plan, actor=str(actor.principal.user_id))
            return _plan_payload(plan=plan)
        except DestructionDependencyError as error:
            raise HTTPException(
                status_code=_HTTP_CONFLICT,
                detail={
                    "message": str(error),
                    "missingPipelines": list(error.dependent_pipeline_names),
                },
            ) from error
        except (DestructionExternalDependencyError, DestructionResourceError) as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except DestructionRecoveryNotFoundError as error:
            raise HTTPException(status_code=_HTTP_NOT_FOUND, detail=str(error)) from error
        except DestructionRecoveryError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except (DestructionSelectionError, ValueError) as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except AdapterError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.post("/api/runs/{invocation_id}/recovery-plan")(create_recovery_plan)
    return app


def _register_destruction_execution_route(
    *,
    app: FastAPI,
    state: DevServerState,
    warehouse: WarehouseRuntime,
    database: str | None,
    project_dir: Path,
    authorization: OperationAuthorizationContext,
    servable_analysis: Callable[[], CompileAnalysis],
    store: DestructionPlanStore,
) -> FastAPI:
    """Attach the reviewed frozen-plan execution route."""

    dispatches: _DestructionDispatchRegistry = _DestructionDispatchRegistry()

    def execute_plan(
        *,
        http_request: Request,
        plan_id: str,
        request: DestructionExecutionRequest,
    ) -> dict[str, object]:
        actor: AuthenticatedRequest = _actor(http_request)
        actor_id: str = str(actor.principal.user_id)
        connection: AdapterConnection = _required_connection(warehouse=warehouse, database=database)
        try:
            plan: DestructionPlan = store.get(plan_id=plan_id, actor=actor_id)
            _authorize_plan(
                plan=plan,
                analysis=servable_analysis(),
                database=database,
                http_request=http_request,
                authorization=authorization,
            )
            reviewed_at: datetime = store.reviewed_at(plan_id=plan_id, actor=actor_id)
            challenge_responses: tuple[str, ...] = tuple(request.responses)
            if challenge_responses != plan.challenges:
                raise DestructionChallengeError(
                    "Challenge responses must exactly match the frozen plan in order"
                )
            observation_connection: AdapterConnection | None = warehouse.observation_connection
            if observation_connection is None:
                raise HTTPException(
                    status_code=_HTTP_SERVICE_UNAVAILABLE,
                    detail="no warehouse observation connection",
                )
            if not dispatches.reserve(plan_id=plan_id):
                raise DestructionPlanNotFoundError(
                    f"Destruction plan {plan_id!r} is already executing"
                )
            invocation_id: str = str(uuid4())

            def run() -> None:
                try:
                    with state.query_lock:
                        _ = execute_destruction(
                            frozen_plan=plan,
                            actor=DestructionActor(
                                actor_id=actor_id,
                                actor_name=actor.principal.username,
                            ),
                            challenge_responses=challenge_responses,
                            reviewed_at=reviewed_at,
                            store=store,
                            connection=connection,
                            observation_connection=observation_connection,
                            project_dir=project_dir,
                            replan=lambda: _fresh_plan(
                                state=state,
                                plan=plan,
                                connection=connection,
                                database=database,
                                http_request=http_request,
                                authorization=authorization,
                            ),
                            invocation_id=invocation_id,
                        )
                    state.snapshot.invalidate()
                except (Exception, KeyboardInterrupt):
                    _LOGGER.exception(
                        "Background destruction execution failed for invocation %s",
                        invocation_id,
                    )
                finally:
                    dispatches.release(plan_id=plan_id)

            threading.Thread(
                target=run,
                name=f"sqltide-destruction-{invocation_id}",
                daemon=True,
            ).start()
            return {"invocationId": invocation_id, "status": "starting"}
        except DestructionPlanExpiredError as error:
            raise HTTPException(status_code=_HTTP_GONE, detail=str(error)) from error
        except DestructionPlanNotFoundError as error:
            raise HTTPException(status_code=_HTTP_NOT_FOUND, detail=str(error)) from error
        except DestructionPlanCorruptError as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error
        except DestructionChallengeError as error:
            raise HTTPException(status_code=_HTTP_BAD_REQUEST, detail=str(error)) from error
        except (DestructionPlanNotReviewedError, DestructionDriftError) as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except AdapterTargetMutationLockError as error:
            raise HTTPException(status_code=_HTTP_CONFLICT, detail=str(error)) from error
        except (AdapterError, DestructionRecordingError) as error:
            raise HTTPException(status_code=_HTTP_BAD_GATEWAY, detail=str(error)) from error

    app.post("/api/destruction/plans/{plan_id}/execute", status_code=_HTTP_ACCEPTED)(execute_plan)
    return app


def _fresh_plan(
    *,
    state: DevServerState,
    plan: DestructionPlan,
    connection: AdapterConnection,
    database: str | None,
    http_request: Request,
    authorization: OperationAuthorizationContext,
) -> DestructionPlan:
    outcome: CompileOutcome = state.reload()
    if outcome.analysis is None:
        raise DestructionDriftError("Project compilation changed or failed after plan review")
    _authorize_plan(
        plan=plan,
        analysis=outcome.analysis,
        database=database,
        http_request=http_request,
        authorization=authorization,
    )
    return plan_destruction(
        request=DestructionRequest(
            operation=plan.operation,
            target=plan.target,
            database=plan.database,
            metadata_database=plan.metadata_database,
            pipeline_names=plan.requested_pipeline_names,
            included_dependent_pipeline_names=plan.included_dependent_pipeline_names,
        ),
        analysis=outcome.analysis,
        connection=connection,
    )


def _recovery_request_from_run(
    *,
    run: Mapping[str, object],
    analysis: CompileAnalysis,
    database: str,
    project_dir: Path,
    authorization: OperationAuthorizationContext,
) -> DestructionRequest:
    if run.get("mode") != _DESTRUCTIVE_MODE or run.get("outcome") != _FAILED_OUTCOME:
        raise DestructionRecoveryError("Only a terminal failed destruction run can be recovered")
    expected_project: str | None = logical_project_identity(project_dir=project_dir)
    if run.get("projectIdentity") != expected_project:
        raise DestructionRecoveryNotFoundError("Failed destruction run was not found")
    summary: object = run.get("summary")
    if not isinstance(summary, Mapping):
        raise DestructionRecoveryError("Failed destruction run has no complete recovery evidence")
    recorded: Mapping[str, object] = cast(Mapping[str, object], summary)
    operation_value: object = recorded.get("operationKind")
    if not isinstance(operation_value, str):
        raise DestructionRecoveryError("Failed destruction run has no recorded operation kind")
    try:
        operation: DestructionOperation = DestructionOperation(operation_value)
    except ValueError as error:
        raise DestructionRecoveryError(
            "Failed destruction run operation kind is invalid"
        ) from error
    expected_command: str = (
        "destroy pipelines"
        if operation == DestructionOperation.DESTROY_PIPELINES
        else "reset target"
    )
    if run.get("command") != expected_command:
        raise DestructionRecoveryError("Failed destruction run command and evidence disagree")
    current_target: str = (
        analysis.compiled_project.target_name or authorization.selected_target or ""
    )
    if recorded.get("target") != current_target or recorded.get("database") != database:
        raise DestructionRecoveryError(
            "Failed destruction run target or database differs from the active server"
        )
    return DestructionRequest(
        operation=operation,
        target=current_target,
        database=database,
        metadata_database=database,
        pipeline_names=_recorded_recovery_names(summary=recorded, field="originalSelection"),
        included_dependent_pipeline_names=_recorded_recovery_names(
            summary=recorded,
            field="includedDependentPipelines",
        ),
    )


def _recorded_recovery_names(*, summary: Mapping[str, object], field: str) -> tuple[str, ...]:
    value: object = summary.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DestructionRecoveryError(
            f"Failed destruction run has invalid recorded {field} evidence"
        )
    return tuple(cast(list[str], value))


def _authorize_plan(
    *,
    plan: DestructionPlan,
    analysis: CompileAnalysis,
    database: str | None,
    http_request: Request,
    authorization: OperationAuthorizationContext,
) -> None:
    if not plan.relation_drop_size_policy_observed:
        raise DestructionDriftError(
            "Destruction plan predates frozen DROP safety evidence; create a fresh plan"
        )
    current_target: str = (
        analysis.compiled_project.target_name or authorization.selected_target or ""
    )
    if plan.target != current_target or plan.database != (database or ""):
        raise DestructionDriftError(
            "Destruction plan target or physical database differs from the active server target"
        )
    require_destruction_authorization(
        analysis=analysis,
        request=http_request,
        context=authorization,
        operation=plan.operation.value,
        affected_pipelines=plan.affected_pipeline_names,
    )


def _required_connection(*, warehouse: WarehouseRuntime, database: str | None) -> AdapterConnection:
    connection: AdapterConnection | None = warehouse.connection
    if connection is None or database is None:
        raise HTTPException(
            status_code=_HTTP_SERVICE_UNAVAILABLE,
            detail="no warehouse connection",
        )
    return connection


def _actor(request: Request) -> AuthenticatedRequest:
    return read_authenticated_request(request=request)


def _plan_payload(
    *, plan: DestructionPlan, reviewed_at: datetime | None = None
) -> dict[str, object]:
    return {
        "planId": plan.plan_id,
        "planFingerprint": plan.plan_fingerprint,
        "operation": plan.operation.value,
        "target": plan.target,
        "database": plan.database,
        "selectedPipelines": list(plan.requested_pipeline_names),
        "includedDependentPipelines": list(plan.included_dependent_pipeline_names),
        "affectedPipelines": list(plan.affected_pipeline_names),
        "requiredDependentPipelines": [],
        "blocked": False,
        "models": list(plan.affected_model_names),
        "resources": [
            {
                "name": relation.name,
                "kind": str(relation.kind),
                "logicalName": relation.logical_names[0],
                "logicalNames": list(relation.logical_names),
                "pipelineName": relation.pipeline_names[0] if relation.pipeline_names else None,
                "exists": relation.exists,
                "bytes": relation.total_bytes,
                "activeParts": relation.active_parts,
            }
            for relation in plan.relations
        ],
        "managedSourcesIncluded": not plan.preserves_sources,
        "retainedReplayDataIncluded": not plan.preserves_replay_data,
        "estimatedBytes": plan.estimated_bytes,
        "dropSizeLimitBytes": plan.relation_drop_size_limit,
        "dropSizeServerLimitBytes": plan.relation_drop_size_server_limit,
        "dropSizeOverrideBytes": plan.relation_drop_size_override,
        "dropSizePolicyObserved": plan.relation_drop_size_policy_observed,
        "challengeValues": list(plan.challenges),
        "expiresAt": plan.expires_at.isoformat(),
        "reviewedAt": None if reviewed_at is None else reviewed_at.isoformat(),
    }
