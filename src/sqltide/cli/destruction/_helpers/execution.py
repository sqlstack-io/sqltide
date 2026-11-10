"""Authorized CLI destruction planning and execution."""

from datetime import datetime
from typing import cast
from uuid import UUID

import sqltide._native as _native
from sqltide._native import analyze_project, plan_destruction
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.constants import SQLITE_MEMORY_URLS
from sqltide.auth.main.default_control_store_url import default_control_store_url
from sqltide.auth.models import UserAccount
from sqltide.cli.destruction._helpers.authorization import require_same_destruction_admin
from sqltide.cli.destruction._helpers.rendering import (
    confirm_destruction_review,
    print_destruction_result,
    read_destruction_challenges,
    render_destruction_plan,
)
from sqltide.cli.destruction.constants import DESTRUCTION_SUCCESS_OUTCOME
from sqltide.cli.destruction.models import DestructionCommandOptions
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.main._resolve_default_database import resolve_default_database
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction.classes.relational_destruction_plan_store import (
    RelationalDestructionPlanStore,
)
from sqltide.executor.destruction.exceptions import DestructionDriftError
from sqltide.executor.destruction.main.execute_destruction import execute_destruction
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionExecutionResult,
    DestructionPlan,
    DestructionRequest,
)
from sqltide.executor.destruction.types import DestructionPlanningConnection


def run_authorized_destruction(
    *,
    options: DestructionCommandOptions,
    account: UserAccount,
    control_store: ControlStore,
    os_username: str,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Plan and execute destruction for one persisted administrator."""

    creator_user_id: UUID = account.user_id
    actor_id: str = str(creator_user_id)
    pipeline_names: tuple[str, ...] = _pipeline_names(options.selectors)
    analysis: CompileAnalysis = _analyze(
        options=options,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
    )
    database: str = resolve_default_database(
        loaded_pipelines=list(analysis.compile_inputs.pipelines), override=options.database
    )
    request: DestructionRequest = DestructionRequest(
        operation=options.operation,
        target=options.selected_target,
        database=database,
        metadata_database=database,
        pipeline_names=pipeline_names,
        include_orphans=options.include_orphans,
    )
    planning_connection: DestructionPlanningConnection = cast(DestructionPlanningConnection, client)
    plan: DestructionPlan = plan_destruction(
        request=request, analysis=analysis, connection=planning_connection
    )
    plan_store_url: str = (
        default_control_store_url(project_dir=options.project_dir)
        if options.control_store_url in SQLITE_MEMORY_URLS
        else options.control_store_url
    )
    store: RelationalDestructionPlanStore = RelationalDestructionPlanStore(url=plan_store_url)
    try:
        store.save(plan=plan, actor=actor_id)
        print(render_destruction_plan(plan))
        if not confirm_destruction_review():
            print("Destruction cancelled.")
            return 1
        reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor=actor_id)
        responses: tuple[str, ...] = read_destruction_challenges(plan)

        def replan() -> DestructionPlan:
            _ = require_same_destruction_admin(
                store=control_store,
                os_username=os_username,
                expected_user_id=creator_user_id,
            )
            fresh_analysis: CompileAnalysis = _analyze(
                options=options,
                loaded_project=_native.load_project_input_for_path(
                    path=options.project_dir,
                    selected_target=options.selected_target,
                    cli_variables=dict(options.cli_variables),
                    environment={} if options.environment is None else options.environment,
                ),
                adapter_profile=adapter_profile,
            )
            fresh_database: str = resolve_default_database(
                loaded_pipelines=list(fresh_analysis.compile_inputs.pipelines),
                override=options.database,
            )
            if fresh_database != request.database:
                raise DestructionDriftError(
                    "Destruction target physical database changed after plan review"
                )
            return plan_destruction(
                request=request,
                analysis=fresh_analysis,
                connection=planning_connection,
            )

        account = require_same_destruction_admin(
            store=control_store,
            os_username=os_username,
            expected_user_id=creator_user_id,
        )
        result: DestructionExecutionResult = execute_destruction(
            frozen_plan=plan,
            actor=DestructionActor(actor_id=actor_id, actor_name=account.username),
            challenge_responses=responses,
            reviewed_at=reviewed_at,
            store=store,
            connection=client,
            observation_connection=observation_client,
            project_dir=options.project_dir,
            replan=replan,
        )
        print_destruction_result(result)
        return 0 if result.outcome == DESTRUCTION_SUCCESS_OUTCOME else 1
    finally:
        store.close()


def _analyze(
    *,
    options: DestructionCommandOptions,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> CompileAnalysis:
    return analyze_project(
        pipelines_root=options.pipelines_root,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
    )


def _pipeline_names(selectors: tuple[str, ...]) -> tuple[str, ...]:
    prefix: str = "pipeline:"
    invalid: tuple[str, ...] = tuple(
        selector for selector in selectors if not selector.startswith(prefix)
    )
    empty: tuple[str, ...] = tuple(selector for selector in selectors if selector == prefix)
    if invalid or empty:
        raise CliUserError("sqt destroy selections must use a non-empty pipeline:NAME selector")
    return tuple(selector.removeprefix(prefix) for selector in selectors)
