from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from sqltide._native import plan_destruction
from sqltide.adapter.models import (
    AdapterColumn,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterIdentity,
    AdapterManagedSource,
    AdapterManifestSnapshot,
    AdapterMaterializedView,
    AdapterMetadataObjectKey,
    AdapterMutationResult,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterQueryResult,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
    AdapterStableBinding,
    AdapterTable,
    AdapterView,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import CompiledProject, LogicalResourceKey
from sqltide.compiler.graph.models import DependencyEdge, ProjectGraph
from sqltide.compiler.graph.types import DependencyEdgeType
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction.exceptions import DestructionPlanNotFoundError
from sqltide.executor.destruction.models import (
    DestructionPlan,
    DestructionRelationEvidence,
    DestructionRequest,
)
from sqltide.executor.destruction.types import (
    DestructionOperation,
    DestructionOwnership,
    DestructionPlanStore,
    DestructionRelationKind,
)
from sqltide.executor.workflow.models import WarehouseStatement
from sqltide.executor.workflow.types import StatementIntent, WorkflowPhase
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


@dataclass(frozen=True)
class PlanningFixture:
    analysis: CompileAnalysis
    connection: DestructionPlanningConnection


class DestructionPlanningConnection:
    def __init__(
        self,
        *,
        catalog: CatalogSnapshot,
        inventory: AdapterDeploymentInventory | None = None,
        stats: tuple[tuple[str, int, int], ...] = (),
        catalog_matches_resources: bool = True,
        external_dependants: tuple[str, ...] = (),
        relation_drop_size_limit: int | None = None,
        relation_drop_size_server_limit: int | None = None,
        manifests: AdapterManifestSnapshot | None = None,
    ) -> None:
        self.catalog = catalog
        self.inventory = inventory or AdapterDeploymentInventory(deployments=(), publish_events=())
        self.stats = stats
        self.catalog_matches_resources = catalog_matches_resources
        self.external_dependants = external_dependants
        self.relation_drop_size_limit = relation_drop_size_limit
        self.relation_drop_size_server_limit = relation_drop_size_server_limit
        self.manifests = manifests or AdapterManifestSnapshot(status="absent", manifests=())
        self.catalog_databases: list[str] = []
        self.inventory_databases: list[str] = []
        self.queries: list[str] = []
        self.catalog_match_count: int = 0

    def load_catalog(self, database: str) -> CatalogSnapshot:
        self.catalog_databases.append(database)
        return self.catalog

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        self.inventory_databases.append(database)
        return self.inventory

    def load_manifests(
        self,
        *,
        database: str,
        project_identity: str,
        target_name: str,
        target_database: str,
    ) -> AdapterManifestSnapshot:
        del database, project_identity, target_name, target_database
        return self.manifests

    def catalog_resource_matches(
        self, *, resource: object, relation: object, database: str
    ) -> bool:
        del resource, database
        self.catalog_match_count += 1
        return relation is not None and self.catalog_matches_resources

    def load_external_dependants(
        self, *, database: str, relation_names: tuple[str, ...]
    ) -> tuple[str, ...]:
        del database, relation_names
        return self.external_dependants

    def load_relation_drop_size_limit(self) -> int | None:
        return self.relation_drop_size_limit

    def load_relation_drop_size_server_default(self) -> int | None:
        return self.relation_drop_size_server_limit

    def query(self, statement: str) -> AdapterQueryResult:
        self.queries.append(statement)
        return AdapterQueryResult(
            rows=tuple((name, byte_count, parts) for name, byte_count, parts in self.stats),
            column_names=("relation_name", "total_bytes", "active_parts"),
        )


type EdgeMaps = tuple[
    dict[LogicalResourceKey, tuple[DependencyEdge, ...]],
    dict[LogicalResourceKey, tuple[DependencyEdge, ...]],
]
type EdgeMapsBuilder = Callable[
    [LogicalResourceKey, LogicalResourceKey, LogicalResourceKey], EdgeMaps
]


def _no_dependency_edges(
    source_key: LogicalResourceKey,
    orders_key: LogicalResourceKey,
    summary_key: LogicalResourceKey,
) -> EdgeMaps:
    upstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (),
        orders_key: (),
        summary_key: (),
    }
    downstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (),
        orders_key: (),
        summary_key: (),
    }
    return upstream, downstream


def _model_dependency_edges(
    source_key: LogicalResourceKey,
    orders_key: LogicalResourceKey,
    summary_key: LogicalResourceKey,
) -> EdgeMaps:
    edge: DependencyEdge = DependencyEdge(
        upstream_key=orders_key,
        downstream_key=summary_key,
        edge_type=DependencyEdgeType.REFERENCE,
    )
    upstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (),
        orders_key: (),
        summary_key: (edge,),
    }
    downstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (),
        orders_key: (edge,),
        summary_key: (),
    }
    return upstream, downstream


def _source_dependency_edges(
    source_key: LogicalResourceKey,
    orders_key: LogicalResourceKey,
    summary_key: LogicalResourceKey,
) -> EdgeMaps:
    edge: DependencyEdge = DependencyEdge(
        upstream_key=source_key,
        downstream_key=summary_key,
        edge_type=DependencyEdgeType.REFERENCE,
    )
    upstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (),
        orders_key: (),
        summary_key: (edge,),
    }
    downstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = {
        source_key: (edge,),
        orders_key: (),
        summary_key: (),
    }
    return upstream, downstream


def build_planning_fixture(*, target_name: str = "uat") -> PlanningFixture:
    return _build_planning_fixture(
        target_name=target_name,
        edge_maps_builder=_no_dependency_edges,
    )


def build_model_dependency_planning_fixture() -> PlanningFixture:
    return _build_planning_fixture(
        target_name="uat",
        edge_maps_builder=_model_dependency_edges,
    )


def build_source_dependency_planning_fixture() -> PlanningFixture:
    return _build_planning_fixture(
        target_name="uat",
        edge_maps_builder=_source_dependency_edges,
    )


def build_transitive_source_dependency_planning_fixture() -> PlanningFixture:
    fixture: PlanningFixture = build_source_dependency_planning_fixture()
    original_project: SimpleNamespace = cast(
        SimpleNamespace, fixture.analysis.realized_project.project
    )
    original_realized: SimpleNamespace = cast(SimpleNamespace, fixture.analysis.realized_project)
    secondary_source_key: LogicalResourceKey = LogicalResourceKey("source", "secondary_events")
    secondary_source: SimpleNamespace = SimpleNamespace(key=secondary_source_key)
    original_beta: SimpleNamespace = original_project.pipelines[1]
    beta: SimpleNamespace = SimpleNamespace(
        pipeline=original_beta.pipeline,
        source=secondary_source,
    )
    gamma: SimpleNamespace = SimpleNamespace(
        pipeline=SimpleNamespace(name="gamma"),
        source=secondary_source,
    )
    project_values: dict[str, object] = {
        **vars(original_project),
        "pipelines": (original_project.pipelines[0], beta, gamma),
        "sources": (*original_project.sources, secondary_source),
    }
    project: SimpleNamespace = SimpleNamespace(**project_values)
    resources_by_key: dict[LogicalResourceKey, tuple[object, ...]] = dict(
        original_realized.resources_by_logical_key
    )
    resources_by_key[secondary_source_key] = (
        AdapterTable(
            name="raw__secondary_events",
            columns=(AdapterColumn(name="payload", type="String"),),
            engine="MergeTree()",
            order_by=("tuple()",),
        ),
    )
    relation_names_by_key: dict[LogicalResourceKey, str] = dict(
        original_realized.relation_name_by_logical_key
    )
    relation_names_by_key[secondary_source_key] = "raw__secondary_events"
    realized_values: dict[str, object] = {
        **vars(original_realized),
        "project": project,
        "resources_by_logical_key": resources_by_key,
        "relation_name_by_logical_key": relation_names_by_key,
    }
    realized: SimpleNamespace = SimpleNamespace(**realized_values)
    upstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = dict(
        fixture.analysis.graph.upstream_edges_by_key
    )
    downstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]] = dict(
        fixture.analysis.graph.downstream_edges_by_key
    )
    upstream[secondary_source_key] = ()
    downstream[secondary_source_key] = ()
    graph: ProjectGraph = replace(
        fixture.analysis.graph,
        project=project,
        upstream_edges_by_key=upstream,
        downstream_edges_by_key=downstream,
        ordered_keys=(*fixture.analysis.graph.ordered_keys, secondary_source_key),
    )
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=realized,
            discovered_inputs=SimpleNamespace(project_dir=Path("/tmp/commerce")),
            graph=graph,
            compiled_project=SimpleNamespace(
                project_name="commerce",
                production_target=False,
                destruction_relation_drop_size_limit=None,
            ),
        ),
    )
    return PlanningFixture(analysis=analysis, connection=fixture.connection)


def _build_planning_fixture(
    *, target_name: str, edge_maps_builder: EdgeMapsBuilder
) -> PlanningFixture:
    orders_key: LogicalResourceKey = LogicalResourceKey("model", "orders")
    summary_key: LogicalResourceKey = LogicalResourceKey("model", "summary")
    source_key: LogicalResourceKey = LogicalResourceKey("source", "events")
    adopted_key: LogicalResourceKey = LogicalResourceKey("source", "warehouse_users")

    orders: SimpleNamespace = SimpleNamespace(key=orders_key, pipeline_name="alpha", kind="table")
    summary: SimpleNamespace = SimpleNamespace(key=summary_key, pipeline_name="beta", kind="view")
    managed_source: SimpleNamespace = SimpleNamespace(key=source_key)
    adopted_source: SimpleNamespace = SimpleNamespace(key=adopted_key)
    alpha: SimpleNamespace = SimpleNamespace(
        pipeline=SimpleNamespace(name="alpha"), source=managed_source
    )
    beta: SimpleNamespace = SimpleNamespace(pipeline=SimpleNamespace(name="beta"), source=None)
    project: SimpleNamespace = SimpleNamespace(
        pipelines=(alpha, beta),
        models=(orders, summary),
        sources=(managed_source, adopted_source),
        project_name="commerce",
        target_name=target_name,
    )
    upstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]]
    downstream: dict[LogicalResourceKey, tuple[DependencyEdge, ...]]
    upstream, downstream = edge_maps_builder(source_key, orders_key, summary_key)
    graph: ProjectGraph = ProjectGraph(
        project=cast(CompiledProject, project),
        upstream_edges_by_key=upstream,
        downstream_edges_by_key=downstream,
        ordered_keys=(source_key, adopted_key, orders_key, summary_key),
    )
    table: AdapterTable = AdapterTable(
        name="tbl__orders",
        columns=(AdapterColumn(name="id", type="UInt64"),),
        engine="MergeTree()",
        order_by=("id",),
    )
    materialized_view: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__orders",
        source_relation_name="raw__events",
        target_relation_name="tbl__orders",
        query="SELECT id FROM raw__events",
        database_template="{database}",
    )
    summary_view: AdapterView = AdapterView(
        name="vw__summary",
        query="SELECT count() FROM tbl__orders",
        database_template="{database}",
    )
    kafka: AdapterManagedSource = AdapterManagedSource(
        source_kind="kafka",
        name="kafka__events",
        columns=(AdapterColumn(name="payload", type="String"),),
        broker_list="localhost:9092",
        topic="events",
        consumer_group="sqltide-events",
        format="JSONEachRow",
    )
    landing: AdapterTable = AdapterTable(
        name="raw__events",
        columns=(AdapterColumn(name="payload", type="String"),),
        engine="MergeTree()",
        order_by=("tuple()",),
    )
    source_view: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__events",
        source_relation_name="kafka__events",
        target_relation_name="raw__events",
        query="SELECT payload FROM kafka__events",
        database_template="{database}",
    )
    internal: AdapterTable = AdapterTable(
        name="_sqltide_manifest_accident",
        columns=(),
        engine="MergeTree()",
        order_by=("tuple()",),
    )
    resources: dict[LogicalResourceKey, tuple[object, ...]] = {
        orders_key: (table, materialized_view),
        summary_key: (summary_view,),
        source_key: (kafka, landing, source_view, internal),
        adopted_key: (),
    }
    realized: SimpleNamespace = SimpleNamespace(
        project=project,
        resources_by_logical_key=resources,
        relation_name_by_logical_key={
            orders_key: "tbl__orders",
            summary_key: "vw__summary",
            source_key: "raw__events",
            adopted_key: "external_users",
        },
        resolved_query_by_model_key={
            orders_key: "SELECT id FROM raw__events",
            summary_key: "SELECT count() FROM tbl__orders",
        },
    )
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=realized,
            discovered_inputs=SimpleNamespace(project_dir=Path("/tmp/commerce")),
            graph=graph,
            compiled_project=SimpleNamespace(
                project_name="commerce",
                production_target=False,
                destruction_relation_drop_size_limit=None,
            ),
        ),
    )
    inventory: AdapterDeploymentInventory = AdapterDeploymentInventory(
        deployments=(
            AdapterDeploymentRecord(
                deployment_id="deployment_1",
                created_at="2026-08-24 10:00:00.000",
                status="published",
                replay_lineage_mode="retained",
                selected_root_keys=(),
                warning_codes=(),
                prepared_object_mappings=(
                    AdapterPreparedObjectMapping(
                        logical_key=AdapterMetadataObjectKey(
                            database=None,
                            object_type="table",
                            name="tbl__orders",
                        ),
                        physical_name="tbl__orders__deployment_1",
                        logical_model_name="orders",
                    ),
                    AdapterPreparedObjectMapping(
                        logical_key=AdapterMetadataObjectKey(
                            database=None,
                            object_type="table",
                            name="old__orders",
                        ),
                        physical_name="old__orders__deployment_1",
                        logical_model_name="orders",
                    ),
                    AdapterPreparedObjectMapping(
                        logical_key=AdapterMetadataObjectKey(
                            database=None,
                            object_type="table",
                            name="raw__events",
                        ),
                        physical_name="raw__events__deployment_1",
                        logical_model_name="events",
                    ),
                ),
            ),
        ),
        publish_events=(
            AdapterPublishEventRecord(
                deployment_id="deployment_1",
                published_at="2026-08-24 10:30:00.000",
                logical_view_names=("tbl__orders",),
                bindings=(
                    AdapterStableBinding(
                        database="analytics",
                        logical_name="tbl__orders",
                        physical_name="tbl__orders__deployment_1",
                    ),
                ),
            ),
        ),
    )
    catalog: CatalogSnapshot = build_catalog(
        (
            ("tbl__orders", "View"),
            ("tbl__orders__deployment_1", "MergeTree"),
            ("mv__orders", "MaterializedView"),
            ("vw__summary", "View"),
            ("kafka__events", "Kafka"),
            ("raw__events", "MergeTree"),
            ("raw__events__deployment_1", "MergeTree"),
            ("mv__events", "MaterializedView"),
            ("_sqltide_manifest_accident", "MergeTree"),
            ("tbl__orders_backup", "MergeTree"),
            ("external_users", "MergeTree"),
        )
    )
    connection: DestructionPlanningConnection = DestructionPlanningConnection(
        catalog=catalog,
        inventory=inventory,
        stats=(
            ("tbl__orders__deployment_1", 2048, 2),
            ("raw__events", 4096, 4),
        ),
    )
    return PlanningFixture(analysis=analysis, connection=connection)


def build_catalog(relations: tuple[tuple[str, str], ...]) -> CatalogSnapshot:
    source_names_by_relation: dict[str, tuple[str, ...]] = {
        "mv__orders": ("raw__events",),
        "mv__events": ("kafka__events",),
    }
    target_name_by_relation: dict[str, str] = {
        "mv__orders": "tbl__orders",
        "mv__events": "raw__events",
    }
    stable_binding_name_by_relation: dict[str, str] = {"tbl__orders": "tbl__orders__deployment_1"}
    return CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=tuple(
            CatalogRelation(
                name=name,
                engine=engine,
                columns=(),
                source_relation_names=source_names_by_relation.get(name, ()),
                target_relation_name=target_name_by_relation.get(name),
                stable_binding_name=stable_binding_name_by_relation.get(name),
                ownership_generation=f"generation:{name}",
            )
            for name, engine in relations
        ),
    )


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def build_stored_destruction_plan(*, now: datetime) -> DestructionPlan:
    fixture: PlanningFixture = build_planning_fixture()
    return plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=now,
        ttl=timedelta(minutes=5),
        plan_id="plan-1",
    )


def destruction_store_url(*, tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'control.db'}"


def build_complete_stored_destruction_plan(*, now: datetime) -> DestructionPlan:
    plan: DestructionPlan = build_stored_destruction_plan(now=now)
    relation: DestructionRelationEvidence = DestructionRelationEvidence(
        database="analytics",
        name="alpha_table",
        kind=DestructionRelationKind.TABLE,
        exists=True,
        total_bytes=None,
        active_parts=3,
        catalog_fingerprint=None,
        logical_names=("alpha", "alpha_alias"),
        pipeline_names=("alpha", "downstream"),
        ownership=(
            DestructionOwnership.CURRENT_MANIFEST,
            DestructionOwnership.OWNERSHIP_LEDGER,
        ),
        dependency_relation_names=("analytics.upstream",),
    )
    return replace(
        plan,
        included_dependent_pipeline_names=("downstream",),
        affected_pipeline_names=("alpha", "downstream"),
        affected_source_names=("events",),
        relations=(relation,),
    )


def consume_saved_plan_once(_: int, *, store: DestructionPlanStore, plan: DestructionPlan) -> str:
    try:
        store.consume(
            plan_id=plan.plan_id,
            challenge_responses=plan.challenges,
            actor="alice",
        )
    except DestructionPlanNotFoundError:
        return "not_found"
    return "consumed"


class DestructionExecutionConnection(RecordingAdapterConnection):
    """Execute deterministic DROP/tombstone statements against an in-memory catalog."""

    def __init__(self, *, relation_names: tuple[str, ...]) -> None:
        super().__init__()
        self.relation_names: set[str] = set(relation_names)
        self.drop_names: list[str] = []
        self.tombstone_names: list[str] = []
        self.fail_catalog: bool = False

    def load_catalog(self, database: str) -> CatalogSnapshot:
        action: Callable[[str], CatalogSnapshot] = {
            False: self._load_catalog,
            True: self._raise_catalog_failure,
        }[self.fail_catalog]
        return action(database)

    def _load_catalog(self, database: str) -> CatalogSnapshot:
        del database
        return build_catalog(tuple((name, "MergeTree") for name in sorted(self.relation_names)))

    def _raise_catalog_failure(self, database: str) -> CatalogSnapshot:
        del database
        raise RuntimeError("injected residual catalog failure")

    def execute_workflow_mutation(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        del query_id
        action: Callable[[str], None] = {
            (True, False): self._record_drop,
            (False, True): self._record_tombstone,
            (False, False): self._ignore_statement,
        }[(statement.startswith("DROP "), statement.startswith("RECORD_TOMBSTONE "))]
        action(statement)
        return self.execute_workflow_sql(statement)

    def _record_drop(self, statement: str) -> None:
        name: str = statement.split("`.`", maxsplit=1)[1].split("`", maxsplit=1)[0]
        self.drop_names.append(name)
        self.relation_names.discard(name)

    def _record_tombstone(self, statement: str) -> None:
        self.tombstone_names.append(statement.removeprefix("RECORD_TOMBSTONE ").removesuffix(";"))

    def _ignore_statement(self, statement: str) -> None:
        del statement


class InterruptingDestructionExecutionConnection(DestructionExecutionConnection):
    """Interrupt immediately after the first DROP reaches its catalog postcondition."""

    def execute_workflow_mutation(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        result: AdapterMutationResult = super().execute_workflow_mutation(
            statement=statement,
            query_id=query_id,
        )
        action: Callable[[], AdapterMutationResult] = {
            False: lambda: result,
            True: self._raise_interrupt,
        }[statement.startswith("DROP ")]
        return action()

    def _raise_interrupt(self) -> AdapterMutationResult:
        raise KeyboardInterrupt("injected destructive interruption")


class DestructionObservationConnection(RecordingAdapterConnection):
    """Persist run evidence with targeted deterministic failure injection."""

    def __init__(self) -> None:
        super().__init__()
        self.run_events: list[AdapterRunEventRecord] = []
        self.fail_workflow_prepared: bool = False
        self.failing_statements: set[str] = set()

    def render_run_events(
        self,
        *,
        database: str,
        events: tuple[AdapterRunEventRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        del database, include_migration
        self.run_events.extend(events)
        return tuple(
            f"RECORD_RUN_EVENT {event.event_kind} {event.step_id or '-'};" for event in events
        )

    def render_run_statements(
        self,
        *,
        database: str,
        statements: tuple[AdapterRunStatementRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        action: Callable[..., tuple[str, ...]] = {
            False: super().render_run_statements,
            True: self._render_failing_run_statements,
        }[self.fail_workflow_prepared]
        return action(
            database=database,
            statements=statements,
            include_migration=include_migration,
        )

    def _render_failing_run_statements(
        self,
        *,
        database: str,
        statements: tuple[AdapterRunStatementRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        del database, include_migration
        self.run_statements = statements
        return ("FAIL_WORKFLOW_PREPARED;",)

    def execute_workflow_mutation(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        del query_id
        action: Callable[[str], AdapterMutationResult] = {
            False: self.execute_workflow_sql,
            True: self._raise_mutation_failure,
        }[statement in self.failing_statements or statement == "FAIL_WORKFLOW_PREPARED;"]
        return action(statement)

    def _raise_mutation_failure(self, statement: str) -> AdapterMutationResult:
        message_by_statement: defaultdict[str, str] = defaultdict(
            lambda: "injected observation persistence failure"
        )
        message_by_statement["FAIL_WORKFLOW_PREPARED;"] = "injected workflow prepared failure"
        message_by_statement["RECORD_RUN_EVENT statement_completed destroy_relation_0001;"] = (
            "injected statement completed failure"
        )
        message_by_statement["RECORD_RUN_EVENT run_completed -;"] = "injected run completed failure"
        raise RuntimeError(message_by_statement[statement])


def build_execution_plan(*, now: datetime) -> DestructionPlan:
    return DestructionPlan(
        plan_id="execution-plan",
        operation=DestructionOperation.DESTROY_PIPELINES,
        target="uat",
        database="analytics",
        metadata_database="metadata",
        requested_pipeline_names=("alpha",),
        included_dependent_pipeline_names=(),
        affected_pipeline_names=("alpha",),
        affected_model_names=("one", "two"),
        affected_source_names=(),
        relations=tuple(
            DestructionRelationEvidence(
                database="analytics",
                name=name,
                kind="table",
                exists=True,
                total_bytes=1,
                active_parts=1,
                catalog_fingerprint=f"fingerprint-{name}",
                logical_names=(name,),
                pipeline_names=("alpha",),
                ownership=(),
                dependency_relation_names=(),
            )
            for name in ("relation_one", "relation_two")
        ),
        challenges=("DESTROY", "analytics"),
        preserves_sources=False,
        preserves_replay_data=False,
        manifest_fingerprint="m" * 64,
        plan_fingerprint="p" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        relation_drop_size_policy_observed=True,
    )


def build_execution_statements() -> tuple[WarehouseStatement, ...]:
    statement_values: tuple[tuple[str, str], ...] = (
        ("destroy_relation_0001", "DROP TABLE IF EXISTS `analytics`.`relation_one` SYNC;"),
        ("destroy_relation_0002", "DROP TABLE IF EXISTS `analytics`.`relation_two` SYNC;"),
    )
    return tuple(
        WarehouseStatement(
            sequence=index,
            step_id=step_id,
            phase=WorkflowPhase.TEARDOWN,
            intent=StatementIntent.MUTATION,
            sql=sql,
        )
        for index, (step_id, sql) in enumerate(statement_values, start=1)
    )
