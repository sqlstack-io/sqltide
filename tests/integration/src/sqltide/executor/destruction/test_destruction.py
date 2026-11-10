import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import analyze_project
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterManifest,
    AdapterManifestResource,
    AdapterMutationResult,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import (
    build_compiler_adapter_profile,
)
from sqltide.compiler.compile.models import CompilerAdapterProfile, LogicalResourceKey
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction.classes.in_memory_destruction_plan_store import (
    InMemoryDestructionPlanStore,
)
from sqltide.executor.destruction.exceptions import (
    DestructionDriftError,
    DestructionExternalDependencyError,
)
from sqltide.executor.destruction.main.assemble_destruction_workflow import (
    assemble_destruction_workflow,
)
from sqltide.executor.destruction.main.execute_destruction import execute_destruction
from sqltide.executor.destruction.main.plan_destruction import plan_destruction
from sqltide.executor.destruction.main.verify_destruction_drift import (
    verify_destruction_drift,
)
from sqltide.executor.destruction.models import (
    DestructionActor,
    DestructionExecutionResult,
    DestructionPlan,
    DestructionRelationEvidence,
    DestructionRequest,
)
from sqltide.executor.workflow.exceptions import WorkflowExecutionError
from sqltide.executor.workflow.main._execute_warehouse_workflow import (
    execute_warehouse_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement
from tests.integration.src.sqltide.cli.helpers import (
    build_managed_clickhouse_client,
    run_direct_build,
    write_direct_build_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.destruction._test_types import (
    CompletionEventFailureIntegrationTestCase,
    DestructionIntegrationTestCase,
    DestructionSafetyIntegrationTestCase,
    OrphanCleanupIntegrationTestCase,
    VirtualHistoryIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.destruction.helpers import realized_relation_names

_ACTOR: DestructionActor = DestructionActor(actor_id="integration", actor_name="integration")


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        OrphanCleanupIntegrationTestCase(
            description="historical selected-pipeline orphan is dropped through frozen workflow",
            orphan_relation_name="legacy__orders",
            expected_outcome="succeeded",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_historical_orphan_when_destroying_selected_pipeline_then_guarded_drop_executes(
    test_case: OrphanCleanupIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "project"
    write_direct_build_project(project_root=project_root)
    execution_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    observation_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        assert (
            run_direct_build(
                project_root=project_root,
                database=clickhouse_database,
                connection=execution_connection,
            )
            == 0
        )
        execution_connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.{test_case.orphan_relation_name} "
            "(id UInt64) ENGINE = MergeTree ORDER BY id;"
        )
        historical_manifest: AdapterManifest = AdapterManifest(
            manifest_id="historical-manifest",
            invocation_id="historical-invocation",
            project_identity="direct_build",
            target_name="test",
            target_database=clickhouse_database,
            is_production=False,
            project_revision=None,
            manifest_fingerprint="historical-fingerprint",
            manifest_version=1,
            pipelines=("pl__orders",),
            resources=(
                AdapterManifestResource(
                    pipeline_name="pl__orders",
                    logical_type="model",
                    logical_name="legacy_orders",
                    resource_role="primary",
                    resource_database=clickhouse_database,
                    resource_name=test_case.orphan_relation_name,
                    resource_kind="table",
                ),
            ),
            tool_version="integration",
            published_at="2020-01-01 00:00:00.000000",
        )
        for statement in execution_connection.render_manifest_publication(
            database=clickhouse_database,
            manifest=historical_manifest,
        ):
            execution_connection.execute_workflow_sql(statement)
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=_native.load_project_input_for_path(path=project_root),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        request: DestructionRequest = DestructionRequest(
            operation="destroy_pipelines",
            target="test",
            database=clickhouse_database,
            metadata_database=clickhouse_database,
            pipeline_names=("pl__orders",),
            include_orphans=True,
        )
        plan: DestructionPlan = plan_destruction(
            request=request,
            analysis=analysis,
            connection=execution_connection,
        )
        store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore()
        store.save(plan=plan, actor="integration")
        reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="integration")

        result: DestructionExecutionResult = execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=execution_connection,
            observation_connection=observation_connection,
            project_dir=project_root,
            replan=lambda: plan_destruction(
                request=request,
                analysis=analysis,
                connection=execution_connection,
            ),
        )

        assert result.outcome == test_case.expected_outcome
        assert (
            test_case.orphan_relation_name
            not in execution_connection.load_catalog(clickhouse_database).relation_names()
        )
    finally:
        execution_connection.close()
        observation_connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DestructionIntegrationTestCase(
            description="destroy removes complete scope and reset preserves operation evidence",
            expected_destroy_outcome="succeeded",
            expected_reset_outcome="succeeded",
            expected_terminal_invocation_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_real_target_when_destroying_and_resetting_then_scope_and_evidence_are_exact(
    test_case: DestructionIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "project"
    write_direct_build_project(project_root=project_root)
    execution_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    observation_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        build_exit_code: int = run_direct_build(
            project_root=project_root,
            database=clickhouse_database,
            connection=execution_connection,
        )
        assert build_exit_code == 0
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(
            ClickHouseAdapter()
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
        execution_connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.stale_raw_orders "
            "(id UInt64) ENGINE = MergeTree ORDER BY id;"
        )
        execution_connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.tbl__orders_enriched_backup "
            "(id UInt64) ENGINE = MergeTree ORDER BY id;"
        )
        destroy_request: DestructionRequest = DestructionRequest(
            operation="destroy_pipelines",
            target="test",
            database=clickhouse_database,
            metadata_database=clickhouse_database,
            pipeline_names=("pl__orders",),
        )
        destroy_plan: DestructionPlan = plan_destruction(
            request=destroy_request,
            analysis=analysis,
            connection=execution_connection,
        )
        destroy_store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore()
        destroy_store.save(plan=destroy_plan, actor="integration")
        destroy_reviewed_at: datetime = destroy_store.mark_reviewed(
            plan_id=destroy_plan.plan_id,
            actor="integration",
        )
        destroy_result: DestructionExecutionResult = execute_destruction(
            frozen_plan=destroy_plan,
            actor=_ACTOR,
            challenge_responses=destroy_plan.challenges,
            reviewed_at=destroy_reviewed_at,
            store=destroy_store,
            connection=execution_connection,
            observation_connection=observation_connection,
            project_dir=project_root,
            replan=lambda: plan_destruction(
                request=destroy_request,
                analysis=analysis,
                connection=execution_connection,
            ),
        )
        assert destroy_result.outcome == test_case.expected_destroy_outcome

        destroy_catalog_names: frozenset[str] = execution_connection.load_catalog(
            clickhouse_database
        ).relation_names()
        source_keys: tuple[LogicalResourceKey, ...] = tuple(
            source.key for source in analysis.realized_project.project.sources
        )
        model_keys: tuple[LogicalResourceKey, ...] = tuple(
            model.key for model in analysis.realized_project.project.models
        )
        source_relation_names: frozenset[str] = realized_relation_names(
            analysis=analysis,
            logical_keys=source_keys,
        )
        model_relation_names: frozenset[str] = realized_relation_names(
            analysis=analysis,
            logical_keys=model_keys,
        )
        assert not source_relation_names & destroy_catalog_names
        assert "stale_raw_orders" in destroy_catalog_names
        assert not model_relation_names & destroy_catalog_names

        reset_request: DestructionRequest = DestructionRequest(
            operation="reset_target",
            target="test",
            database=clickhouse_database,
            metadata_database=clickhouse_database,
        )
        reset_plan: DestructionPlan = plan_destruction(
            request=reset_request,
            analysis=analysis,
            connection=execution_connection,
        )
        assert "stale_raw_orders" not in {relation.name for relation in reset_plan.relations}
        assert "tbl__orders_enriched_backup" not in {
            relation.name for relation in reset_plan.relations
        }
        reset_store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore()
        reset_store.save(plan=reset_plan, actor="integration")
        reset_reviewed_at: datetime = reset_store.mark_reviewed(
            plan_id=reset_plan.plan_id,
            actor="integration",
        )
        reset_result: DestructionExecutionResult = execute_destruction(
            frozen_plan=reset_plan,
            actor=_ACTOR,
            challenge_responses=reset_plan.challenges,
            reviewed_at=reset_reviewed_at,
            store=reset_store,
            connection=execution_connection,
            observation_connection=observation_connection,
            project_dir=project_root,
            replan=lambda: plan_destruction(
                request=reset_request,
                analysis=analysis,
                connection=execution_connection,
            ),
        )
        assert reset_result.outcome == test_case.expected_reset_outcome

        remaining_names: frozenset[str] = execution_connection.load_catalog(
            clickhouse_database
        ).relation_names()
        assert not source_relation_names & remaining_names
        assert "stale_raw_orders" in remaining_names
        assert "tbl__orders_enriched_backup" in remaining_names
        assert all(
            name.startswith("_sqltide_")
            or name in {"stale_raw_orders", "tbl__orders_enriched_backup"}
            for name in remaining_names
        )
        invocation_count: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM {clickhouse_database}._sqltide_invocations "
                "WHERE command IN ('destroy pipelines', 'reset target')"
            ).result_rows[0][0]
        )
        statement_count: int = int(
            clickhouse_client.query(
                f"SELECT count() FROM {clickhouse_database}._sqltide_run_statements "
                f"WHERE invocation_id = '{reset_result.invocation_id}'"
            ).result_rows[0][0]
        )
        assert invocation_count == test_case.expected_terminal_invocation_count
        assert statement_count == len(
            assemble_destruction_workflow(plan=reset_plan, connection=execution_connection)
        )
    finally:
        observation_connection.close()
        execution_connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DestructionSafetyIntegrationTestCase(
            description="manual replacement changes the frozen generation",
            expected_error_match="impact",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_manually_recreated_relation_when_replanning_then_frozen_generation_is_rejected(
    test_case: DestructionSafetyIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "replacement-project"
    write_direct_build_project(project_root=project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        assert (
            run_direct_build(
                project_root=project_root,
                database=clickhouse_database,
                connection=connection,
            )
            == 0
        )
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        request: DestructionRequest = DestructionRequest(
            operation="reset_target",
            target="test",
            database=clickhouse_database,
            metadata_database=clickhouse_database,
        )
        frozen: DestructionPlan = plan_destruction(
            request=request,
            analysis=analysis,
            connection=connection,
        )
        connection.execute_workflow_sql(
            f"DROP TABLE {clickhouse_database}.tbl__orders_enriched SYNC;"
        )
        connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.tbl__orders_enriched "
            "(replacement UInt64) ENGINE = MergeTree ORDER BY replacement;"
        )

        fresh: DestructionPlan = plan_destruction(
            request=request,
            analysis=analysis,
            connection=connection,
        )
        frozen_by_name: dict[str, DestructionRelationEvidence] = {
            relation.name: relation for relation in frozen.relations
        }
        fresh_by_name: dict[str, DestructionRelationEvidence] = {
            relation.name: relation for relation in fresh.relations
        }
        frozen_generation: str | None = frozen_by_name["tbl__orders_enriched"].catalog_fingerprint
        fresh_generation: str | None = fresh_by_name["tbl__orders_enriched"].catalog_fingerprint

        assert fresh_generation != frozen_generation
        with pytest.raises(DestructionDriftError, match=test_case.expected_error_match):
            verify_destruction_drift(frozen_plan=frozen, replan=lambda: fresh)
    finally:
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DestructionSafetyIntegrationTestCase(
            description="partial drop leaves exact residual catalog state",
            expected_error_match="injected second drop failure",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_second_drop_failure_when_destroying_then_catalog_has_exact_completed_prefix(
    test_case: DestructionSafetyIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root: Path = tmp_path / "partial-project"
    write_direct_build_project(project_root=project_root)
    project_config: Path = project_root / "sqltide_project.toml"
    project_config.write_text(
        project_config.read_text()
        + '\n[targets.test.destruction]\nmax_table_size_to_drop = "100GiB"\n'
    )
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        assert (
            run_direct_build(
                project_root=project_root,
                database=clickhouse_database,
                connection=connection,
            )
            == 0
        )
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        plan: DestructionPlan = plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="test",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                pipeline_names=("pl__orders",),
            ),
            analysis=analysis,
            connection=connection,
        )
        statements: tuple[WarehouseStatement, ...] = assemble_destruction_workflow(
            plan=plan,
            connection=connection,
        )
        drops: tuple[WarehouseStatement, ...] = tuple(
            filter(lambda statement: statement.sql.startswith("DROP "), statements)
        )
        assert all("max_table_size_to_drop = 107374182400" in drop.sql for drop in drops)
        original_mutation: Callable[..., AdapterMutationResult] = (
            connection.execute_workflow_mutation
        )

        def raise_injected_failure(**_: object) -> AdapterMutationResult:
            raise AdapterWarehouseError("injected second drop failure")

        mutation_by_sql: defaultdict[str, Callable[..., AdapterMutationResult]] = defaultdict(
            lambda: original_mutation
        )
        mutation_by_sql[drops[1].sql] = raise_injected_failure

        def route_mutation(*, statement: str, query_id: str | None) -> AdapterMutationResult:
            return mutation_by_sql[statement](statement=statement, query_id=query_id)

        monkeypatch.setattr(connection, "execute_workflow_mutation", route_mutation)

        with pytest.raises(WorkflowExecutionError, match=test_case.expected_error_match):
            execute_warehouse_workflow(statements=statements, connection=connection)

        first_name: str = drops[0].sql.split("`.`", maxsplit=1)[1].split("`", maxsplit=1)[0]
        second_name: str = drops[1].sql.split("`.`", maxsplit=1)[1].split("`", maxsplit=1)[0]
        catalog_names: frozenset[str] = connection.load_catalog(
            clickhouse_database
        ).relation_names()
        assert first_name not in catalog_names
        assert second_name in catalog_names
    finally:
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CompletionEventFailureIntegrationTestCase(
            description="first drop completion failure records an exact partial prefix",
            expected_outcome="failed",
            expected_residual_status="observed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_drop_completion_event_failure_when_destroying_then_partial_evidence_is_exact(
    test_case: CompletionEventFailureIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root: Path = tmp_path / "completion-event-project"
    write_direct_build_project(project_root=project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    observation_connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        assert (
            run_direct_build(
                project_root=project_root,
                database=clickhouse_database,
                connection=connection,
            )
            == 0
        )
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        request: DestructionRequest = DestructionRequest(
            operation="destroy_pipelines",
            target="test",
            database=clickhouse_database,
            metadata_database=clickhouse_database,
            pipeline_names=("pl__orders",),
        )
        plan: DestructionPlan = plan_destruction(
            request=request,
            analysis=analysis,
            connection=connection,
        )
        statements: tuple[WarehouseStatement, ...] = assemble_destruction_workflow(
            plan=plan,
            connection=connection,
        )
        drops: tuple[WarehouseStatement, ...] = tuple(
            filter(lambda statement: statement.sql.startswith("DROP "), statements)
        )
        first_drop: WarehouseStatement = drops[0]
        second_drop: WarehouseStatement = drops[1]
        original_observation_mutation: Callable[..., AdapterMutationResult] = (
            observation_connection.execute_workflow_mutation
        )

        def fail_first_drop_completion(
            *, statement: str, query_id: str | None
        ) -> AdapterMutationResult:
            action: Callable[..., AdapterMutationResult] = {
                True: raise_injected_completion_failure,
                False: original_observation_mutation,
            }["statement_completed" in statement and first_drop.step_id in statement]
            return action(statement=statement, query_id=query_id)

        def raise_injected_completion_failure(**_: object) -> AdapterMutationResult:
            raise AdapterWarehouseError("injected first drop completion event failure")

        monkeypatch.setattr(
            observation_connection,
            "execute_workflow_mutation",
            fail_first_drop_completion,
        )
        store: InMemoryDestructionPlanStore = InMemoryDestructionPlanStore()
        store.save(plan=plan, actor="integration")
        reviewed_at: datetime = store.mark_reviewed(plan_id=plan.plan_id, actor="integration")

        result: DestructionExecutionResult = execute_destruction(
            frozen_plan=plan,
            actor=_ACTOR,
            challenge_responses=plan.challenges,
            reviewed_at=reviewed_at,
            store=store,
            connection=connection,
            observation_connection=observation_connection,
            project_dir=project_root,
            replan=lambda: plan_destruction(
                request=request,
                analysis=analysis,
                connection=connection,
            ),
        )

        first_name: str = first_drop.sql.split("`.`", maxsplit=1)[1].split("`", maxsplit=1)[0]
        second_name: str = second_drop.sql.split("`.`", maxsplit=1)[1].split("`", maxsplit=1)[0]
        expected_completed: tuple[int, ...] = tuple(
            statement.sequence for statement in statements[: first_drop.sequence]
        )
        expected_pending: tuple[int, ...] = tuple(
            statement.sequence for statement in statements[first_drop.sequence :]
        )
        catalog_names: frozenset[str] = connection.load_catalog(
            clickhouse_database
        ).relation_names()
        invocation_rows: Sequence[Sequence[object]] = clickhouse_client.query(
            f"SELECT outcome, summary_json FROM {clickhouse_database}._sqltide_invocations "
            f"WHERE invocation_id = '{result.invocation_id}'"
        ).result_rows
        assert len(invocation_rows) == 1
        summary: dict[str, object] = json.loads(str(invocation_rows[0][1]))

        assert result.outcome == test_case.expected_outcome
        assert result.residual_catalog_status == test_case.expected_residual_status
        assert result.completed_statement_sequences == expected_completed
        assert result.pending_statement_sequences == expected_pending
        assert result.remaining_relation_names is not None
        assert first_name not in catalog_names
        assert second_name in catalog_names
        assert invocation_rows[0][0] == test_case.expected_outcome
        assert summary["completedStatementSequences"] == list(expected_completed)
        assert summary["pendingStatementSequences"] == list(expected_pending)
        assert summary["remainingObjects"] == list(result.remaining_relation_names)
        assert summary["actor"] == {"id": "integration", "username": "integration"}
        assert summary["planId"] == plan.plan_id
        assert summary["submittedChallenges"] == list(plan.challenges)
    finally:
        observation_connection.close()
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        VirtualHistoryIntegrationTestCase(
            description="recorded virtual history authorizes reset deletion scope",
            expected_reset_included_names=("retired_binding", "retired_physical"),
            expected_destroy_excluded_names=("retired_binding", "retired_physical"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_recorded_virtual_history_when_resetting_then_live_generations_are_frozen(
    test_case: VirtualHistoryIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "virtual-history-project"
    write_direct_build_project(project_root=project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        for statement in connection.render_migrate_metadata_state(clickhouse_database):
            connection.execute_workflow_sql(statement)
        connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.retired_physical "
            "(id UInt64) ENGINE = MergeTree ORDER BY id;"
        )
        connection.execute_workflow_sql(
            f"CREATE VIEW {clickhouse_database}.retired_binding AS "
            f"SELECT * FROM {clickhouse_database}.retired_physical;"
        )
        connection.execute_workflow_sql(
            f"INSERT INTO {clickhouse_database}._sqltide_virtual_deployments "
            "(deployment_id, workflow_fingerprint, replay_lineage_mode, boundary_time, "
            "created_at, tool_version) VALUES ('retired-deployment', 'retired-workflow', "
            "'offsets', now64(3, 'UTC'), now64(3, 'UTC'), 'test');"
        )
        connection.execute_workflow_sql(
            f"INSERT INTO {clickhouse_database}._sqltide_virtual_object_state "
            "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
            "logical_object_type, logical_object_name, physical_database_name, "
            "physical_relation_name, logical_model_database, logical_model_name, "
            "is_selected_root, object_fingerprint, canonical_query, observed_at) VALUES "
            "('retired-deployment', 'retired-observation', 'deployment', "
            "'retired-deployment', NULL, 'table', 'retired_binding', NULL, "
            "'retired_physical', NULL, 'retired_model', false, 'retired-fingerprint', "
            "NULL, now64(3, 'UTC'));"
        )
        connection.execute_workflow_sql(
            f"INSERT INTO {clickhouse_database}._sqltide_virtual_publications "
            "(publication_id, deployment_id, operation, previous_deployment_id, "
            "logical_database_name, logical_view_name, physical_database_name, "
            "physical_relation_name, published_at) VALUES ('retired-publication', "
            "'retired-deployment', 'promote', NULL, "
            f"'{clickhouse_database}', 'retired_binding', '{clickhouse_database}', "
            "'retired_physical', now64(3, 'UTC'));"
        )

        reset: DestructionPlan = plan_destruction(
            request=DestructionRequest(
                operation="reset_target",
                target="test",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
            ),
            analysis=analysis,
            connection=connection,
        )
        destroy: DestructionPlan = plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="test",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                pipeline_names=("pl__orders",),
            ),
            analysis=analysis,
            connection=connection,
        )

        reset_by_name: dict[str, DestructionRelationEvidence] = {
            relation.name: relation for relation in reset.relations
        }
        destroy_names: frozenset[str] = frozenset(relation.name for relation in destroy.relations)
        assert set(test_case.expected_reset_included_names) <= reset_by_name.keys()
        assert all(
            reset_by_name[name].catalog_fingerprint
            for name in test_case.expected_reset_included_names
        )
        assert set(test_case.expected_destroy_excluded_names).isdisjoint(destroy_names)
    finally:
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DestructionSafetyIntegrationTestCase(
            description="qualified dependant in another database blocks destruction",
            expected_error_match="external_reader",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cross_database_dependant_when_planning_then_destruction_is_blocked(
    test_case: DestructionSafetyIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "cross-database-dependant-project"
    write_direct_build_project(project_root=project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    external_database: str = f"{clickhouse_database}_external"
    try:
        assert (
            run_direct_build(
                project_root=project_root,
                database=clickhouse_database,
                connection=connection,
            )
            == 0
        )
        connection.execute_workflow_sql(f"CREATE DATABASE {external_database};")
        connection.execute_workflow_sql(
            f"CREATE VIEW {external_database}.external_reader AS SELECT * FROM "
            f"{clickhouse_database}.tbl__orders_enriched;"
        )
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )

        with pytest.raises(
            DestructionExternalDependencyError,
            match=test_case.expected_error_match,
        ):
            plan_destruction(
                request=DestructionRequest(
                    operation="destroy_pipelines",
                    target="test",
                    database=clickhouse_database,
                    metadata_database=clickhouse_database,
                    pipeline_names=("pl__orders",),
                ),
                analysis=analysis,
                connection=connection,
            )
        connection.execute_workflow_sql(f"DROP VIEW {external_database}.external_reader SYNC;")
        connection.execute_workflow_sql(
            f"CREATE TABLE {external_database}.tbl__orders_enriched "
            "(order_id String) ENGINE = MergeTree ORDER BY order_id;"
        )
        connection.execute_workflow_sql(
            f"CREATE VIEW {clickhouse_database}.external_name_reader AS SELECT * FROM "
            f"{external_database}.tbl__orders_enriched;"
        )

        allowed: DestructionPlan = plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="test",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                pipeline_names=("pl__orders",),
            ),
            analysis=analysis,
            connection=connection,
        )

        assert "external_name_reader" not in {relation.name for relation in allowed.relations}
    finally:
        connection.execute_workflow_sql(f"DROP DATABASE IF EXISTS {external_database} SYNC;")
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DestructionSafetyIntegrationTestCase(
            description="live manifest DDL mismatch remains associated",
            expected_error_match="tbl__orders_enriched",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_manifest_ddl_mismatch_when_planning_then_relation_is_included(
    test_case: DestructionSafetyIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
    tmp_path: Path,
) -> None:
    project_root: Path = tmp_path / "bootstrap-refusal-project"
    write_direct_build_project(project_root=project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        loaded_project: LoadedProject | None = _native.load_project_input_for_path(
            path=project_root
        )
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_root / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        for statement in connection.render_migrate_metadata_state(clickhouse_database):
            connection.execute_workflow_sql(statement)
        connection.execute_workflow_sql(
            f"CREATE TABLE {clickhouse_database}.tbl__orders_enriched "
            "(replacement UInt64) ENGINE = MergeTree ORDER BY replacement;"
        )

        plan: DestructionPlan = plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="test",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                pipeline_names=("pl__orders",),
            ),
            analysis=analysis,
            connection=connection,
        )

        by_name: dict[str, DestructionRelationEvidence] = {
            relation.name: relation for relation in plan.relations
        }
        assert by_name[test_case.expected_error_match].catalog_fingerprint
    finally:
        connection.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
