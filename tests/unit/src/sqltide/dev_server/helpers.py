import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import sqltide._native as _native
from sqltide._native import (
    DevApplication,
    analyze_project,
    build_activity_capabilities_query,
    build_extents_query,
    build_partitions_query,
    build_parts_activity_query,
    build_parts_query,
    build_relation_stats_query,
    build_replay_count_query,
    build_throughputs_query,
    create_dev_app,
    render_clickhouse_replay_coverage_query,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterCapabilities,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterMetadataObjectKey,
    AdapterMutationResult,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterQueryResult,
    AdapterReplayCoverageRequest,
    AdapterStableView,
    AdapterTable,
    AdapterView,
    AdapterWarehouseHealth,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.models import AuthSettings, UserAccount
from sqltide.auth.types import AuthenticationMode, AuthenticationSource, UnknownUserPolicy
from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
)
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.compile.models import LogicalResourceKey, ObjectKey
from sqltide.compiler.discovery.models import KafkaSettings
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import (
    DeploymentPlan,
    DeploymentStep,
    DirectPlan,
    DirectPlanEntry,
    DirectRelationOperation,
    DirectSourceDriftSelection,
    PlannerWarning,
    PreparedShadowObject,
)
from sqltide.dev_server._helpers.queries.message_query import (
    build_messages_sql,
    parse_messages_document,
)
from sqltide.dev_server.classes.dev_server_state import DevServerState
from sqltide.dev_server.classes.kafka_lag_reader import KafkaLagReader
from sqltide.dev_server.classes.kafka_topic_reader import KafkaTopicReader
from sqltide.dev_server.classes.overlay_reader import OverlayReader
from sqltide.dev_server.classes.state_snapshot import StateSnapshot
from sqltide.dev_server.models import (
    KafkaLagSnapshot,
    KafkaTopicsSnapshot,
    MessagesQueryRequest,
)
from sqltide.executor.destruction.models import DestructionPlan
from sqltide.executor.destruction.types import DestructionOperation
from sqltide.executor.direct.models import DirectBuildWorkflow
from sqltide.executor.population.models import (
    PopulationManagedSource,
    PopulationSourceRecreation,
)
from sqltide.executor.workflow.models import BuildWorkflow
from sqltide.executor.workflow.types import WorkflowMode
from tests.unit.src.sqltide.auth.helpers import build_control_store
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
    write_project_configuration_and_source,
)
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeTestClient


def changed_storage_identity(identity: dict[str, object]) -> str:
    """Return fingerprint metadata with a deterministic storage-only change."""

    storage: dict[str, object] = cast(dict[str, object], identity["storage"])
    return json.dumps(
        {**identity, "storage": {**storage, "ttl": "created_at + INTERVAL 1 DAY"}},
        sort_keys=True,
        separators=(",", ":"),
    )


_ORDERS_CLEAN_MODEL: str = """
MODEL (
  description "Cleaned order rows.",
  order_by ["order_id", "_replay_partition", "_replay_offset"],
  columns (
    order_id (description "Primary order id", audits [not_null]),
  ),
);

SELECT
  CAST(order_id AS String) AS order_id,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset
FROM __ref("orders")
"""

_NOT_NULL_GENERIC_AUDIT: str = """
AUDIT ();

SELECT @column
FROM __ref("@model")
WHERE @column IS NULL
"""


_ORDERS_SOURCE_WITH_FRESHNESS: str = """
sources:
  - name: orders
    kind: kafka
    broker_list: kafka:9092
    topic: source.orders
    replay_boundary:
      mode: offsets
    freshness:
      warn_after: 1h
      error_after: 4h
"""

_UNAVAILABLE_FINGERPRINTS: AdapterDirectFingerprintSnapshot = AdapterDirectFingerprintSnapshot(
    status="unavailable",
    baselines=(),
    warning="Direct SQL baselines unavailable in the test adapter",
)
_UNAVAILABLE_WAREHOUSE_HEALTH: AdapterWarehouseHealth = AdapterWarehouseHealth(
    availability="unavailable",
    status="unknown",
    version=None,
    uptime_seconds=None,
    disks=(),
    inode_total=None,
    inode_free=None,
    inode_status="unknown",
    memory=None,
    activity=None,
    tables=None,
    collection_duration_ms=0,
    warnings=("Warehouse diagnostics are unavailable.",),
)


def write_dev_server_project(*, project_dir: Path) -> None:
    write_project_configuration_and_source(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "pipelines" / "order_events" / "pipeline.toml",
        'mode = "direct"',
    )
    write_pipeline_file(project_dir / "sources" / "orders.yml", _ORDERS_SOURCE_WITH_FRESHNESS)
    write_pipeline_file(
        project_dir / "pipelines" / "order_events" / "orders_clean.sql",
        _ORDERS_CLEAN_MODEL,
    )
    write_pipeline_file(
        project_dir / "audits" / "generic" / "not_null.sql",
        _NOT_NULL_GENERIC_AUDIT,
    )


_QUALITY_ALERTS_SENSOR: str = '''
from sqltide.events import AuditCompleted
from sqltide.sensors import event_sensor


@event_sensor(on=AuditCompleted)
def quality_alerts(ctx):
    """Alert on audit transitions."""
'''


def write_sensor_file(*, project_dir: Path) -> None:
    write_pipeline_file(project_dir / "sensors" / "quality.py", _QUALITY_ALERTS_SENSOR)


def build_compile_callable(*, project_dir: Path) -> Callable[[], CompileAnalysis]:
    def run_compile() -> CompileAnalysis:
        return analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=_native.load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )

    return run_compile


def break_project_compile(*, project_dir: Path) -> None:
    write_pipeline_file(
        project_dir / "pipelines" / "order_events" / "broken.sql",
        "SELECT 1 AS value",
    )


def build_test_client(*, project_dir: Path) -> NativeTestClient:
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    return NativeTestClient(create_dev_app(state=state, project_dir=project_dir))


def build_proxy_test_client(*, project_dir: Path, store: ControlStore) -> NativeTestClient:
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    return NativeTestClient(
        create_dev_app(
            state=state,
            project_dir=project_dir,
            auth_settings=AuthSettings(
                mode=AuthenticationMode.TRUSTED_PROXY,
                control_store_url="sqlite://",
                unknown_user_policy=UnknownUserPolicy.DENY,
            ),
            control_store=store,
        )
    )


def build_assigned_proxy_reload_client(
    *, project_dir: Path
) -> tuple[NativeTestClient, ControlStore]:
    write_dev_server_project(project_dir=project_dir)
    write_reload_access_policy(project_dir=project_dir, permission="project.reload")
    store: ControlStore = build_control_store(tmp_path=project_dir)
    alice: UserAccount = store.create_user(
        username="alice",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="alice",
        roles=("viewer",),
    )
    store.create_user(
        username="bob",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="bob",
        roles=("viewer",),
    )
    store.grant_project_role(
        user_id=alice.user_id,
        project_name="test_project",
        role_name="operator",
        target_name="dev",
        actor_user_id=None,
    )
    return build_proxy_test_client(project_dir=project_dir, store=store), store


def write_reload_access_policy(*, project_dir: Path, permission: str) -> None:
    write_pipeline_file(
        project_dir / "access.yml",
        f"""roles:
  operator:
    grants: [{{scope: project, permissions: [{permission}]}}]
""",
    )


def build_assigned_proxy_message_client(
    *, project_dir: Path
) -> tuple[NativeTestClient, ControlStore]:
    write_dev_server_project(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "access.yml",
        """roles:
  message_reader:
    grants: [{scope: project, permissions: [source.messages.read]}]
""",
    )
    store: ControlStore = build_control_store(tmp_path=project_dir)
    alice: UserAccount = store.create_user(
        username="alice",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="alice",
        roles=("viewer",),
    )
    store.create_user(
        username="bob",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="bob",
        roles=("viewer",),
    )
    store.grant_project_role(
        user_id=alice.user_id,
        project_name="test_project",
        role_name="message_reader",
        target_name=None,
        actor_user_id=None,
    )
    return build_proxy_test_client(project_dir=project_dir, store=store), store


def build_assigned_proxy_quality_client(
    *, project_dir: Path
) -> tuple[NativeTestClient, ControlStore]:
    write_dev_server_project(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "access.yml",
        """roles:
  quality_operator:
    grants:
      - pipelines: [order_events]
        permissions: [quality.audit.run, quality.test.run]
""",
    )
    store: ControlStore = build_control_store(tmp_path=project_dir)
    alice: UserAccount = store.create_user(
        username="alice",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="alice",
        roles=("viewer",),
    )
    store.create_user(
        username="bob",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="bob",
        roles=("viewer",),
    )
    store.grant_project_role(
        user_id=alice.user_id,
        project_name="test_project",
        role_name="quality_operator",
        target_name=None,
        actor_user_id=None,
    )
    return build_proxy_test_client(project_dir=project_dir, store=store), store


def build_assigned_proxy_operations_client(
    *, project_dir: Path
) -> tuple[NativeTestClient, ControlStore]:
    """Build operational personas where administrator Alice holds authored grants."""

    return _build_assigned_proxy_operations_client(
        project_dir=project_dir,
        connection=build_fake_state_connection(),
        database="analytics",
    )


def build_assigned_proxy_operations_client_without_warehouse(
    *, project_dir: Path
) -> tuple[NativeTestClient, ControlStore]:
    """Build the operational personas without a warehouse connection."""

    return _build_assigned_proxy_operations_client(
        project_dir=project_dir,
        connection=None,
        database=None,
    )


def _build_assigned_proxy_operations_client(
    *, project_dir: Path, connection: AdapterConnection | None, database: str | None
) -> tuple[NativeTestClient, ControlStore]:
    write_dev_server_project(project_dir=project_dir)
    write_sensor_file(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "access.yml",
        """roles:
  operator:
    grants:
      - pipelines: [order_events]
        permissions:
          - build.direct.run
          - deployment.create
          - build.cancel
          - deployment.promote
          - pipeline.destroy
      - scope: target
        permissions:
          - build.kill
          - deployment.cleanup
          - target.reset
          - automation.manage
""",
    )
    store: ControlStore = build_control_store(tmp_path=project_dir)
    alice: UserAccount = store.create_user(
        username="alice",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="alice",
        roles=("admin",),
    )
    store.create_user(
        username="bob",
        authentication_source=AuthenticationSource.TRUSTED_PROXY,
        external_subject="bob",
        roles=("viewer",),
    )
    store.grant_project_role(
        user_id=alice.user_id,
        project_name="test_project",
        role_name="operator",
        target_name=None,
        actor_user_id=None,
    )
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    client: NativeTestClient = NativeTestClient(
        create_dev_app(
            state=state,
            connection=connection,
            database=database,
            project_dir=project_dir,
            auth_settings=AuthSettings(
                mode=AuthenticationMode.TRUSTED_PROXY,
                control_store_url="sqlite://",
                unknown_user_policy=UnknownUserPolicy.DENY,
            ),
            control_store=store,
        )
    )
    return client, store


def proxy_proof_headers(*, username: str) -> dict[str, str]:
    return {
        "X-SQLTide-User": username,
        "X-SQLTide-CSRF": "trusted-proxy",
    }


def build_pipeline_destruction_route_plan() -> DestructionPlan:
    now: datetime = datetime.now(tz=UTC)
    return DestructionPlan(
        plan_id="plan-1",
        operation=DestructionOperation.DESTROY_PIPELINES,
        target="dev",
        database="analytics",
        metadata_database="analytics",
        requested_pipeline_names=("order_events",),
        included_dependent_pipeline_names=(),
        affected_pipeline_names=("order_events",),
        affected_model_names=("orders_clean",),
        affected_source_names=(),
        relations=(),
        challenges=("order_events",),
        preserves_sources=True,
        preserves_replay_data=True,
        manifest_fingerprint="a" * 64,
        plan_fingerprint="b" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        relation_drop_size_policy_observed=True,
    )


def build_target_reset_route_plan() -> DestructionPlan:
    now: datetime = datetime.now(tz=UTC)
    return DestructionPlan(
        plan_id="plan-1",
        operation=DestructionOperation.RESET_TARGET,
        target="dev",
        database="analytics",
        metadata_database="analytics",
        requested_pipeline_names=(),
        included_dependent_pipeline_names=(),
        affected_pipeline_names=("order_events",),
        affected_model_names=("orders_clean",),
        affected_source_names=(),
        relations=(),
        challenges=("order_events",),
        preserves_sources=False,
        preserves_replay_data=False,
        manifest_fingerprint="a" * 64,
        plan_fingerprint="b" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        relation_drop_size_policy_observed=True,
    )


_STATIC_INDEX_CONTENTS: str = "<html>sqt-dev-shell</html>"
_STATIC_APP_SCRIPT_CONTENTS: str = "console.log('sqt-app-script');"
_STATIC_IMMUTABLE_SCRIPT_CONTENTS: str = "console.log('sqt-immutable-script');"
_STATIC_VERSION_CONTENTS: str = '{"version":"example-version"}'
_STATIC_ROBOTS_CONTENTS: str = "User-agent: *"


def write_static_assets_build(*, assets_root: Path) -> None:
    (assets_root / "_app" / "immutable").mkdir(parents=True)
    (assets_root / "index.html").write_text(_STATIC_INDEX_CONTENTS)
    (assets_root / "_app" / "app.js").write_text(_STATIC_APP_SCRIPT_CONTENTS)
    (assets_root / "_app" / "immutable" / "app.example.js").write_text(
        _STATIC_IMMUTABLE_SCRIPT_CONTENTS
    )
    (assets_root / "_app" / "version.json").write_text(_STATIC_VERSION_CONTENTS)
    (assets_root / "robots.txt").write_text(_STATIC_ROBOTS_CONTENTS)


_UNSAFE_HTTP_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def registered_mutation_routes(*, client: NativeTestClient) -> frozenset[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for methods, path in client.app.routes():
        routes.update((method, path) for method in frozenset(methods) & _UNSAFE_HTTP_METHODS)
    return frozenset(routes)


def build_static_test_client(*, assets_root: Path) -> NativeTestClient:
    app: DevApplication = create_dev_app(
        state=DevServerState(run_compile=MagicMock()), project_dir=assets_root
    )
    app.mount_static_assets(assets_root)
    return NativeTestClient(app)


def named_payload_item(items: list, name: str) -> dict:
    by_name: dict = {item["name"]: item for item in items}
    return by_name[name]


def maybe_break_project_compile(*, project_dir: Path, break_compile: bool) -> None:
    writers: dict[bool, Callable[..., None]] = {
        True: break_project_compile,
        False: _skip_break,
    }
    writer: Callable[..., None] = writers[break_compile]
    writer(project_dir=project_dir)


def _skip_break(*, project_dir: Path) -> None:
    return None


class FakeAdapterConnection(AdapterConnection):
    """Exact-match canned warehouse: every expected query string maps to a result."""

    def __init__(
        self,
        *,
        catalog: CatalogSnapshot,
        results_by_query: dict[str, AdapterQueryResult],
        warehouse_timestamp: str,
        fingerprints: AdapterDirectFingerprintSnapshot = _UNAVAILABLE_FINGERPRINTS,
        warehouse_health: AdapterWarehouseHealth = _UNAVAILABLE_WAREHOUSE_HEALTH,
    ) -> None:
        self._catalog = catalog
        self._results_by_query = results_by_query
        self._warehouse_timestamp = warehouse_timestamp
        self._fingerprints = fingerprints
        self._warehouse_health = warehouse_health

    @property
    def adapter_identity(self) -> AdapterIdentity:
        return AdapterIdentity(name="clickhouse")

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            virtual_environments=False,
            managed_source_kinds=frozenset({"kafka"}),
            replay_boundary_modes=frozenset(),
            history_prefix_seed=False,
            stable_logical_bindings=False,
            per_relation_atomic_replace=False,
            graph_atomic_publish=False,
            set_difference_comparison=True,
            direct_rebuild=True,
        )

    def capture_warehouse_timestamp(self) -> str:
        return self._warehouse_timestamp

    def close(self) -> None:
        return None

    def compare_readiness(self, request: object) -> tuple:
        raise NotImplementedError

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        raise NotImplementedError

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        raise NotImplementedError

    def load_catalog(self, database: str) -> CatalogSnapshot:
        return self._catalog

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        return AdapterDeploymentInventory(deployments=(), publish_events=())

    def load_direct_fingerprints(
        self, *, database: str, logical_model_identities: tuple[str, ...]
    ) -> AdapterDirectFingerprintSnapshot:
        del database, logical_model_identities
        return self._fingerprints

    def load_warehouse_health(
        self,
        *,
        database: str,
        managed_source_names: tuple[str, ...] = (),
        include_dead_letters: bool = False,
    ) -> AdapterWarehouseHealth:
        del database
        del managed_source_names
        del include_dead_letters
        return self._warehouse_health

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        del database, table
        return frozenset(
            {
                "node_name",
                "binding_key",
                "definition_fingerprint",
                "execution_fingerprint",
                "trigger",
                "scheduled_for",
                "cadence_seconds",
                "warmup_seconds",
            }
        )

    def query(self, statement: str) -> AdapterQueryResult:
        return self._results_by_query[statement]

    def render_cleanup_relations(self, request: object) -> tuple[str, ...]:
        raise NotImplementedError

    def render_ensure_database(self, database: str) -> str:
        return f"CREATE DATABASE IF NOT EXISTS `{database}`"

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        return (f"CREATE DATABASE IF NOT EXISTS `{database}`;",)

    def catalog_resource_matches(
        self, *, resource: object, relation: object, database: str
    ) -> bool:
        del resource, database
        return relation is not None

    def render_persist_metadata_state(self, *, database: str, state: object) -> tuple[str, ...]:
        raise NotImplementedError

    def render_replace_stable_bindings(self, request: object) -> tuple[str, ...]:
        raise NotImplementedError

    def render_replay_coverage_query(self, request: object) -> str:
        return render_clickhouse_replay_coverage_query(cast(AdapterReplayCoverageRequest, request))

    def render_resource(
        self,
        *,
        resource: (
            AdapterManagedSource
            | AdapterTable
            | AdapterMaterializedView
            | AdapterView
            | AdapterStableView
        ),
        database: str,
        if_not_exists: bool = False,
    ) -> str:
        return ClickHouseAdapter().render_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )


_STATE_WAREHOUSE_NOW: str = "2026-08-03 12:00:00.000"
_STATE_NEWEST_EVENT: str = "2026-08-03 11:59:58.000"
_STATE_OLDEST_EVENT: str = "2026-08-01 00:00:00.000"
_STATE_MODEL_NEWEST: str = "2026-08-03 10:00:00.000"

_RAW_COLUMNS: tuple[CatalogColumn, ...] = (
    CatalogColumn(name="kafka_key", type="String"),
    CatalogColumn(name="kafka_value", type="String"),
    CatalogColumn(name="kafka_topic", type="String"),
    CatalogColumn(name="kafka_partition", type="Int32"),
    CatalogColumn(name="kafka_offset", type="Int64"),
    CatalogColumn(name="kafka_timestamp", type="Nullable(DateTime64(3))"),
    CatalogColumn(name="_replay_partition", type="Int32"),
    CatalogColumn(name="_replay_offset", type="Int64"),
    CatalogColumn(name="_replay_timestamp", type="Nullable(DateTime64(3))"),
    CatalogColumn(name="kafka_header_keys", type="Array(String)"),
    CatalogColumn(name="kafka_header_values", type="Array(String)"),
    CatalogColumn(name="kafka_landed_at", type="DateTime64(3)"),
    CatalogColumn(name="_replay_landed_at", type="DateTime64(3)"),
)

_MODEL_LIVE_COLUMNS: tuple[CatalogColumn, ...] = (
    CatalogColumn(name="order_id", type="String"),
    CatalogColumn(name="_replay_partition", type="Int64"),
    CatalogColumn(name="_replay_offset", type="Int64"),
    CatalogColumn(name="_replay_landed_at", type="DateTime64(3)"),
)


def build_state_test_client(*, project_dir: Path) -> NativeTestClient:
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    connection: FakeAdapterConnection = build_fake_state_connection()
    return NativeTestClient(
        create_dev_app(
            state=state, connection=connection, database="analytics", project_dir=project_dir
        )
    )


def build_virtual_plan_preparation(deployment_id: str) -> VirtualWorkflowPreparation:
    model_key: LogicalResourceKey = LogicalResourceKey("model", "orders_clean")
    object_key: ObjectKey = ObjectKey("analytics", "table", "tbl__orders_clean")
    target_key: ObjectKey = ObjectKey("analytics", "table", "tbl__orders_summary")
    preview: MagicMock = MagicMock()
    preview.database = "analytics"
    preview.deployment_id = deployment_id
    preview.start_time = "2026-08-01 12:00:00.000"
    preview.run_execution_scope = (
        model_key,
        LogicalResourceKey("model", "orders_summary"),
    )
    preview.run_context_scope = (LogicalResourceKey("source", "orders"),)
    preview.plan = DeploymentPlan(
        deployment_id=deployment_id,
        object_changes=(),
        rebuild_subtrees=(),
        steps=(
            DeploymentStep(
                step_id="plan_orders",
                phase="plan",
                action="plan_shadow_table",
                root_key=object_key,
                target_key=target_key,
                physical_name=f"tbl__orders_summary__{deployment_id}",
            ),
        ),
        prepared_shadow_objects=(
            PreparedShadowObject(
                logical_key=object_key,
                physical_name=f"tbl__orders_clean__{deployment_id}",
                logical_model_name="orders_clean",
            ),
            PreparedShadowObject(
                logical_key=target_key,
                physical_name=f"tbl__orders_summary__{deployment_id}",
                logical_model_name="orders_summary",
            ),
        ),
        warnings=(
            PlannerWarning(
                warning_code="bounded_replay",
                message="Replay is bounded.",
                root_key=object_key,
                target_key=target_key,
            ),
        ),
    )
    return VirtualWorkflowPreparation(
        preview=preview,
        request=MagicMock(),
        workflow=MagicMock(),
        plan_text="virtual",
    )


def build_mixed_plan_preparation(deployment_id: str) -> MixedWorkflowPreparation:
    return MixedWorkflowPreparation(
        virtual=build_virtual_plan_preparation(deployment_id),
        direct=build_direct_plan_preparation(),
        plan_text="mixed",
        plan_json="{}",
    )


def build_direct_plan_preparation() -> DirectWorkflowPreparation:
    model_key: LogicalResourceKey = LogicalResourceKey("model", "orders_clean")
    operation: DirectRelationOperation = DirectRelationOperation(
        relation_name="tbl__orders_clean",
        action="create",
        model_key=model_key,
        resource_kind="table",
    )
    preview: MagicMock = MagicMock()
    preview.database = "analytics"
    preview.effective_start_time = "2026-08-01 12:00:00.000"
    preview.plan = DirectPlan(
        database="analytics",
        user_scope=(model_key,),
        execution_scope=(model_key,),
        prerequisite_scope=(),
        entries=(
            DirectPlanEntry(
                model_key=model_key,
                reason="selected",
                relation_names=("tbl__orders_clean",),
                resource_kinds=("table",),
                driving_input_key=None,
                is_replay_root=False,
            ),
        ),
        replay_roots=(),
        teardown_operations=(),
        creation_operations=(operation,),
    )
    return DirectWorkflowPreparation(
        preview=preview,
        request=MagicMock(),
        workflow=MagicMock(),
        plan_text="direct",
    )


def build_direct_source_recreation_plan_preparation() -> DirectWorkflowPreparation:
    preparation: DirectWorkflowPreparation = build_direct_plan_preparation()
    return replace(
        preparation,
        workflow=DirectBuildWorkflow(
            template=BuildWorkflow(mode=WorkflowMode.DIRECT, plan_json="{}", statements=()),
            runtime_replays=(),
            workflow_id="workflow-1",
            recreated_sources=(
                PopulationSourceRecreation(
                    managed_source=PopulationManagedSource(
                        resource=AdapterManagedSource(
                            source_kind="kafka",
                            name="kafka__orders",
                            columns=(),
                            broker_list="kafka:9092",
                            topic="source.orders",
                            consumer_group="sqltide_orders",
                            format="JSONAsString",
                        ),
                        database="analytics",
                    ),
                    landing_view_name="mv__orders",
                    landing_relation_name="raw__orders",
                    consumer_group="sqltide_orders_analytics",
                    drifted_setting_names=("kafka_topic_list",),
                ),
            ),
        ),
    )


def build_direct_source_drift_plan_preparation() -> DirectWorkflowPreparation:
    """Plan a source recreation that a changed selection reached through its owning pipeline."""

    preparation: DirectWorkflowPreparation = build_direct_source_recreation_plan_preparation()
    model_key: LogicalResourceKey = LogicalResourceKey("model", "orders_clean")
    plan: DirectPlan = preparation.preview.plan
    preview: MagicMock = MagicMock()
    preview.database = "analytics"
    preview.effective_start_time = None
    preview.plan = replace(
        plan,
        selection_mode="changed",
        entries=tuple(replace(entry, reason="source_drift") for entry in plan.entries),
        source_drift_selections=(
            DirectSourceDriftSelection(
                source_key=LogicalResourceKey("source", "orders"),
                pipeline_name="order_events",
                reader_pipeline_names=("order_audit", "order_events"),
                drifted_setting_names=("kafka_topic_list",),
                model_keys=(model_key,),
            ),
        ),
    )
    return replace(preparation, preview=preview)


def build_fake_state_connection(
    *,
    fingerprints: AdapterDirectFingerprintSnapshot = _UNAVAILABLE_FINGERPRINTS,
    additional_results: dict[str, AdapterQueryResult] | None = None,
    warehouse_health: AdapterWarehouseHealth = _UNAVAILABLE_WAREHOUSE_HEALTH,
    catalog: CatalogSnapshot | None = None,
) -> FakeAdapterConnection:
    default_catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="raw__orders",
                engine="MergeTree",
                columns=_RAW_COLUMNS,
                order_by=("_replay_partition", "_replay_offset"),
            ),
            CatalogRelation(
                name="tbl__orders_clean",
                engine="ReplacingMergeTree",
                columns=_MODEL_LIVE_COLUMNS,
                order_by=("order_id", "_replay_partition", "_replay_offset"),
            ),
        ),
    )
    model_lineage_relations: tuple[str, ...] = ("tbl__orders_clean",)
    results: dict[str, AdapterQueryResult] = {
        build_activity_capabilities_query(): AdapterQueryResult(
            rows=(),
            column_names=("name",),
        ),
        build_parts_activity_query(database="analytics"): AdapterQueryResult(
            rows=(
                ("raw__orders", _STATE_NEWEST_EVENT),
                ("tbl__orders_clean", _STATE_MODEL_NEWEST),
            ),
            column_names=("table", "last_modified_at"),
        ),
        build_relation_stats_query(database="analytics"): AdapterQueryResult(
            rows=(
                ("raw__orders", 1000, 4096),
                ("tbl__orders_clean", 900, 2048),
            ),
            column_names=("name", "total_rows", "total_bytes"),
        ),
        build_parts_query(database="analytics"): AdapterQueryResult(
            rows=(("raw__orders", 3), ("tbl__orders_clean", 2)),
            column_names=("table", "parts"),
        ),
        build_extents_query(
            database="analytics", relation_names=model_lineage_relations
        ): AdapterQueryResult(
            rows=(("tbl__orders_clean", _STATE_OLDEST_EVENT, _STATE_MODEL_NEWEST, 900),),
            column_names=("relation", "oldest", "newest", "rows"),
        ),
        build_throughputs_query(
            database="analytics",
            windows=(("raw__orders", 3600, 60),),
        ): AdapterQueryResult(
            rows=(
                ("raw__orders", 1754218740, 120),
                ("raw__orders", 1754218800, 180),
            ),
            column_names=("relation", "bucket", "rows"),
        ),
        build_partitions_query(
            database="analytics", relation_names=("raw__orders",)
        ): AdapterQueryResult(
            rows=(
                (
                    "raw__orders",
                    0,
                    91822,
                    _STATE_OLDEST_EVENT,
                    _STATE_NEWEST_EVENT,
                    1000,
                ),
            ),
            column_names=(
                "relation",
                "partition",
                "max_offset",
                "oldest",
                "newest",
                "rows",
            ),
        ),
        (
            "SELECT count() AS present FROM system.tables "
            "WHERE database = 'analytics' AND name = '_sqltide_invocations'"
        ): AdapterQueryResult(rows=((0,),), column_names=("present",)),
        (
            "SELECT count() AS present FROM system.tables "
            "WHERE database = 'analytics' AND name = '_sqltide_run_events'"
        ): AdapterQueryResult(rows=((1,),), column_names=("present",)),
        (
            "SELECT invocation_id, sequence, toString(emitted_at) AS emitted_at, event_kind, "
            "step_id, phase, payload_json FROM (SELECT invocation_id, sequence, "
            "toString(emitted_at) AS emitted_at, event_kind, step_id, phase, payload_json, "
            "row_number() OVER (PARTITION BY invocation_id ORDER BY sequence DESC) AS recency "
            "FROM `analytics`.`_sqltide_run_events` WHERE invocation_id = 'inv-42') "
            "WHERE recency <= 400 OR event_kind = 'run_started' "
            "ORDER BY invocation_id, sequence"
        ): AdapterQueryResult(
            rows=(
                (
                    "inv-42",
                    1,
                    "2026-08-03 12:00:00.000",
                    "run_started",
                    None,
                    None,
                    '{"command": "build", "executedLogicalIds": ["model:orders"], '
                    '"contextLogicalIds": ["source:order_events"]}',
                ),
                (
                    "inv-42",
                    2,
                    "2026-08-03 12:00:01.000",
                    "statement_completed",
                    "replay_orders",
                    "replay",
                    '{"writtenRows": 42}',
                ),
            ),
            column_names=(
                "invocation_id",
                "sequence",
                "emitted_at",
                "event_kind",
                "step_id",
                "phase",
                "payload_json",
            ),
        ),
        (
            "SELECT invocation_id, sequence, toString(emitted_at) AS emitted_at, event_kind, "
            "step_id, phase, payload_json FROM `analytics`.`_sqltide_run_events` "
            "WHERE invocation_id = 'inv-42' ORDER BY invocation_id, sequence LIMIT 500"
        ): AdapterQueryResult(
            rows=(
                (
                    "inv-42",
                    1,
                    "2026-08-03 12:00:00.000",
                    "run_started",
                    None,
                    None,
                    '{"command": "build", "executedLogicalIds": ["model:orders"], '
                    '"contextLogicalIds": ["source:order_events"]}',
                ),
                (
                    "inv-42",
                    2,
                    "2026-08-03 12:00:01.000",
                    "statement_completed",
                    "replay_orders",
                    "replay",
                    '{"writtenRows": 42}',
                ),
            ),
            column_names=(
                "invocation_id",
                "sequence",
                "emitted_at",
                "event_kind",
                "step_id",
                "phase",
                "payload_json",
            ),
        ),
        build_replay_count_query(
            database="analytics",
            relation_name="raw__orders",
            time_column="_replay_landed_at",
            start_time=None,
        ): AdapterQueryResult(rows=((1000,),), column_names=("rows",)),
        # The neutral base connection renders the latest-node-status query as ""
        # (unsupported); the fake answers it with one recorded audit outcome.
        "": AdapterQueryResult(
            rows=(
                (
                    "audit",
                    "orders_clean.order_id.not_null.1",
                    "binding",
                    "fingerprint",
                    "execution",
                    None,
                    0,
                    "passed",
                    [],
                    "error",
                    0,
                    "2026-08-03 09:00:00.000",
                    '{"sample_column_names": ["order_id"], "sample_rows": []}',
                    None,
                ),
            ),
            column_names=(
                "node_kind",
                "node_name",
                "binding_key",
                "definition_fingerprint",
                "execution_fingerprint",
                "cadence_seconds",
                "warmup_seconds",
                "current_status",
                "drift_reasons",
                "severity",
                "failure_count",
                "completed_at",
                "payload_json",
                "error_message",
            ),
        ),
    }
    results.update(additional_results or {})
    return FakeAdapterConnection(
        catalog=catalog or default_catalog,
        results_by_query=results,
        warehouse_timestamp=_STATE_WAREHOUSE_NOW,
        fingerprints=fingerprints,
        warehouse_health=warehouse_health,
    )


class FakeEmptyResultConnection(FakeAdapterConnection):
    """Returns a zero count for every query; audits therefore pass."""

    def query(self, statement: str) -> AdapterQueryResult:
        return AdapterQueryResult(rows=((0,),), column_names=("value",))


_ADOPTED_ORDERS_SOURCE: str = """
sources:
  - name: orders
    kind: stream_table
    table_name: raw_orders_external
    replay_boundary:
      mode: offsets
      columns:
        _replay_partition: _replay_partition
        _replay_offset: _replay_offset
        _replay_timestamp: _replay_timestamp
"""


def write_adopted_dev_server_project(*, project_dir: Path) -> None:
    """The dev fixture project with its only source adopted instead of managed."""

    write_dev_server_project(project_dir=project_dir)
    write_pipeline_file(project_dir / "sources" / "orders.yml", _ADOPTED_ORDERS_SOURCE)


class FakeKafkaTopicReader(KafkaTopicReader):
    """Returns one canned inventory for every broker list without touching Kafka."""

    def __init__(self, *, snapshot: KafkaTopicsSnapshot | None) -> None:
        super().__init__()
        self._snapshot = snapshot

    def read(self, *, kafka: KafkaSettings) -> KafkaTopicsSnapshot | None:
        del kafka
        return self._snapshot


class FakeKafkaLagReader(KafkaLagReader):
    """Returns one canned lag snapshot for every source without touching Kafka."""

    def __init__(self, *, snapshot: KafkaLagSnapshot | None) -> None:
        super().__init__()
        self._snapshot = snapshot

    def read(self, *, kafka: KafkaSettings, database: str) -> KafkaLagSnapshot | None:
        del kafka, database
        return self._snapshot


MESSAGE_HEADER_SCHEMA_QUERY: str = (
    "SELECT count() AS present FROM system.columns "
    "WHERE database = 'analytics' AND table = 'raw__orders' "
    "AND name = 'kafka_header_keys'"
)

MESSAGE_LIST_COLUMN_NAMES: tuple[str, ...] = (
    "landed_at",
    "kafka_timestamp",
    "partition",
    "offset",
    "key",
    "key_bytes",
    "value_preview",
    "value_bytes",
    "header_keys",
    "header_values",
)

MESSAGE_RECORD_COLUMN_NAMES: tuple[str, ...] = (
    "landed_at",
    "kafka_timestamp",
    "partition",
    "offset",
    "topic",
    "key",
    "key_bytes",
    "value",
    "value_bytes",
    "header_keys",
    "header_values",
)


def build_expected_messages_sql(
    *,
    projections: str = "",
    where_clause: str = "",
    limit: int = 50,
    database: str = "analytics",
    relation: str = "raw__orders",
) -> str:
    return (
        "SELECT toString(_replay_landed_at) AS landed_at, "
        "toString(kafka_timestamp) AS kafka_timestamp, "
        "_replay_partition AS partition, _replay_offset AS offset, "
        "kafka_key AS key, length(kafka_key) AS key_bytes, "
        "substring(kafka_value, 1, 512) AS value_preview, "
        "length(kafka_value) AS value_bytes, "
        "kafka_header_keys AS header_keys, kafka_header_values AS header_values"
        f"{projections} "
        f"FROM `{database}`.`{relation}`{where_clause} "
        "ORDER BY _replay_landed_at DESC, _replay_partition DESC, _replay_offset DESC "
        f"LIMIT {limit}"
    )


def build_canned_messages_sql(*, limit: int, window_seconds: int | None) -> str:
    return build_messages_sql(
        database="analytics",
        relation_name="raw__orders",
        document=parse_messages_document(MessagesQueryRequest(limit=limit)),
        window_seconds=window_seconds,
    )


def build_message_test_client(
    *, project_dir: Path, results_by_query: dict[str, AdapterQueryResult]
) -> NativeTestClient:
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    connection: FakeAdapterConnection = FakeAdapterConnection(
        catalog=CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=AdapterIdentity(name="clickhouse"), database="analytics"
            ),
            warehouse_timezone="UTC",
            relations=(),
        ),
        results_by_query=results_by_query,
        warehouse_timestamp=_STATE_WAREHOUSE_NOW,
    )
    return NativeTestClient(
        create_dev_app(
            state=state, connection=connection, database="analytics", project_dir=project_dir
        )
    )


class FakeDeploymentConnection(FakeAdapterConnection):
    """Canned warehouse that also answers deployment lifecycle inspection."""

    def __init__(
        self,
        *,
        catalog: CatalogSnapshot,
        results_by_query: dict[str, AdapterQueryResult],
        warehouse_timestamp: str,
        inventory: AdapterDeploymentInventory,
        managed_state: InspectedManagedTableState,
    ) -> None:
        super().__init__(
            catalog=catalog,
            results_by_query=results_by_query,
            warehouse_timestamp=warehouse_timestamp,
        )
        self._inventory = inventory
        self._managed_state = managed_state

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        del database
        return self._managed_state

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        del database
        return self._inventory


def build_deployment_catalog(*, relation_names: tuple[str, ...]) -> CatalogSnapshot:
    """Catalog containing exactly the supplied relations."""

    return CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="analytics"),
        warehouse_timezone="UTC",
        relations=tuple(
            CatalogRelation(
                name=relation_name,
                engine="ReplacingMergeTree",
                columns=(CatalogColumn(name="order_id", type="String"),),
                order_by=("order_id",),
            )
            for relation_name in relation_names
        ),
    )


def build_deployment_stats_result(
    *, storage_rows: tuple[tuple[str, int, int], ...]
) -> dict[str, AdapterQueryResult]:
    """Map the shared relation-stats query onto canned row and byte totals."""

    return {
        build_relation_stats_query(database="analytics"): AdapterQueryResult(
            rows=storage_rows,
            column_names=("name", "total_rows", "total_bytes"),
        )
    }


DEPLOYMENT_ACTIVE_ID: str = "20260408T091200Z_a1b2cd"
DEPLOYMENT_STAGED_ID: str = "20260410T005500Z_cd34ef"
DEPLOYMENT_ACTIVE_RELATIONS: tuple[str, ...] = (
    f"tbl__orders__{DEPLOYMENT_ACTIVE_ID}",
    f"tbl__revenue__{DEPLOYMENT_ACTIVE_ID}",
    f"mv__orders__{DEPLOYMENT_ACTIVE_ID}",
)
DEPLOYMENT_STAGED_RELATIONS: tuple[str, ...] = (
    f"tbl__orders__{DEPLOYMENT_STAGED_ID}",
    f"tbl__revenue__{DEPLOYMENT_STAGED_ID}",
    f"tbl__refunds__{DEPLOYMENT_STAGED_ID}",
    f"mv__orders__{DEPLOYMENT_STAGED_ID}",
)
_DEPLOYMENT_STORAGE_ROWS: tuple[tuple[str, int, int], ...] = (
    (f"tbl__orders__{DEPLOYMENT_ACTIVE_ID}", 1000, 4096),
    (f"tbl__revenue__{DEPLOYMENT_ACTIVE_ID}", 50, 512),
    (f"mv__orders__{DEPLOYMENT_ACTIVE_ID}", 0, 0),
    (f"tbl__orders__{DEPLOYMENT_STAGED_ID}", 1200, 5120),
    (f"tbl__revenue__{DEPLOYMENT_STAGED_ID}", 60, 640),
    (f"tbl__refunds__{DEPLOYMENT_STAGED_ID}", 7, 128),
    (f"mv__orders__{DEPLOYMENT_STAGED_ID}", 0, 0),
)


def build_deployment_mapping(*, relation_name: str) -> AdapterPreparedObjectMapping:
    """One prepared mapping whose logical name is the relation without its suffix."""

    logical_name: str = relation_name.rsplit("__", 1)[0]
    object_type_by_prefix: dict[str, str] = {"mv": "materialized_view", "tbl": "table"}
    return AdapterPreparedObjectMapping(
        logical_key=AdapterMetadataObjectKey(
            database="analytics",
            object_type=object_type_by_prefix[logical_name.partition("__")[0]],
            name=logical_name,
        ),
        physical_name=relation_name,
        logical_model_name=logical_name,
    )


def build_deployment_record(
    *, deployment_id: str, relations: tuple[str, ...], status: str
) -> AdapterDeploymentRecord:
    """One persisted deployment record covering the supplied relations."""

    return AdapterDeploymentRecord(
        deployment_id=deployment_id,
        created_at="2026-04-10 00:55:00.000000",
        status=status,
        replay_lineage_mode="offsets",
        selected_root_keys=(),
        warning_codes=(),
        prepared_object_mappings=tuple(
            build_deployment_mapping(relation_name=relation_name) for relation_name in relations
        ),
    )


def build_fake_deployment_connection() -> FakeDeploymentConnection:
    """One published deployment plus one staged successor with known storage."""

    return FakeDeploymentConnection(
        catalog=build_deployment_catalog(
            relation_names=DEPLOYMENT_ACTIVE_RELATIONS + DEPLOYMENT_STAGED_RELATIONS
        ),
        results_by_query=build_deployment_stats_result(storage_rows=_DEPLOYMENT_STORAGE_ROWS),
        warehouse_timestamp="2026-04-10 01:00:00.000",
        inventory=AdapterDeploymentInventory(
            deployments=(
                build_deployment_record(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    relations=DEPLOYMENT_ACTIVE_RELATIONS,
                    status="published",
                ),
                build_deployment_record(
                    deployment_id=DEPLOYMENT_STAGED_ID,
                    relations=DEPLOYMENT_STAGED_RELATIONS,
                    status="staged",
                ),
            ),
            publish_events=(
                AdapterPublishEventRecord(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    published_at="2026-04-08 09:15:00.000000",
                    logical_view_names=("tbl__orders", "tbl__revenue"),
                ),
            ),
        ),
        managed_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__orders",
                    physical_name=f"tbl__orders__{DEPLOYMENT_ACTIVE_ID}",
                ),
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__revenue",
                    physical_name=f"tbl__revenue__{DEPLOYMENT_ACTIVE_ID}",
                ),
            ),
            physical_candidates=(),
        ),
    )


def build_fake_partial_promotion_connection() -> FakeDeploymentConnection:
    """A staged successor with one binding switched before promotion failed."""

    return FakeDeploymentConnection(
        catalog=build_deployment_catalog(
            relation_names=DEPLOYMENT_ACTIVE_RELATIONS + DEPLOYMENT_STAGED_RELATIONS
        ),
        results_by_query=build_deployment_stats_result(storage_rows=_DEPLOYMENT_STORAGE_ROWS),
        warehouse_timestamp="2026-04-10 01:00:00.000",
        inventory=AdapterDeploymentInventory(
            deployments=(
                build_deployment_record(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    relations=DEPLOYMENT_ACTIVE_RELATIONS,
                    status="published",
                ),
                build_deployment_record(
                    deployment_id=DEPLOYMENT_STAGED_ID,
                    relations=DEPLOYMENT_STAGED_RELATIONS,
                    status="staged",
                ),
            ),
            publish_events=(
                AdapterPublishEventRecord(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    published_at="2026-04-08 09:15:00.000000",
                    logical_view_names=("tbl__orders", "tbl__revenue"),
                ),
            ),
        ),
        managed_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__orders",
                    physical_name=f"tbl__orders__{DEPLOYMENT_STAGED_ID}",
                ),
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__revenue",
                    physical_name=f"tbl__revenue__{DEPLOYMENT_ACTIVE_ID}",
                ),
            ),
            physical_candidates=(),
        ),
    )


def build_fake_candidate_only_promotion_connection() -> FakeDeploymentConnection:
    """A renamed binding whose staged candidate appears new but removes its live predecessor."""

    active_relation: str = f"tbl__orders_legacy__{DEPLOYMENT_ACTIVE_ID}"
    staged_relation: str = f"tbl__orders_current__{DEPLOYMENT_STAGED_ID}"
    active_mapping: AdapterPreparedObjectMapping = AdapterPreparedObjectMapping(
        logical_key=AdapterMetadataObjectKey(
            database="analytics", object_type="table", name="tbl__orders_legacy"
        ),
        physical_name=active_relation,
        logical_model_name="orders",
    )
    staged_mapping: AdapterPreparedObjectMapping = AdapterPreparedObjectMapping(
        logical_key=AdapterMetadataObjectKey(
            database="analytics", object_type="table", name="tbl__orders_current"
        ),
        physical_name=staged_relation,
        logical_model_name="orders",
    )
    return FakeDeploymentConnection(
        catalog=build_deployment_catalog(relation_names=(active_relation, staged_relation)),
        results_by_query=build_deployment_stats_result(
            storage_rows=((active_relation, 1000, 4096), (staged_relation, 1200, 5120))
        ),
        warehouse_timestamp="2026-04-10 01:00:00.000",
        inventory=AdapterDeploymentInventory(
            deployments=(
                AdapterDeploymentRecord(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    created_at="2026-04-08 09:12:00.000000",
                    status="published",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=(active_mapping,),
                ),
                AdapterDeploymentRecord(
                    deployment_id=DEPLOYMENT_STAGED_ID,
                    created_at="2026-04-10 00:55:00.000000",
                    status="staged",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=(staged_mapping,),
                ),
            ),
            publish_events=(
                AdapterPublishEventRecord(
                    deployment_id=DEPLOYMENT_ACTIVE_ID,
                    published_at="2026-04-08 09:15:00.000000",
                    logical_view_names=("tbl__orders_legacy",),
                ),
            ),
        ),
        managed_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__orders_legacy",
                    physical_name=active_relation,
                ),
            ),
            physical_candidates=(),
        ),
    )


def recording_state_build(calls: list[str]) -> Callable[[], dict[str, object]]:
    """Return a build that records every call and reports which build produced it."""

    def build() -> dict[str, object]:
        calls.append("build")
        return {"capturedAt": f"build-{len(calls)}"}

    return build


def failing_state_build(calls: list[str]) -> Callable[[], dict[str, object]]:
    """Return a build that records its call and then fails."""

    def build() -> dict[str, object]:
        calls.append("build")
        raise RuntimeError("warehouse unavailable")

    return build


def sequenced_state_build(
    builds: list[Callable[[], dict[str, object]]],
) -> Callable[[], dict[str, object]]:
    """Return a build that delegates to each supplied build in turn."""

    def build() -> dict[str, object]:
        return builds.pop(0)()

    return build


def write_connection_settings_project(*, project_dir: Path) -> None:
    """Write a dev-server project whose target connection declares a nested settings table."""

    write_dev_server_project(project_dir=project_dir)
    configuration: Path = project_dir / "sqltide_project.toml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8")
        + '\n[targets.dev.connection]\nhost = "localhost"\n'
        + '\n[targets.dev.connection.settings]\nmax_threads = "16"\n',
        encoding="utf-8",
    )


def build_snapshot_counting_client(*, project_dir: Path, calls: list[str]) -> NativeTestClient:
    """Build a client whose state overlay records every build it performs."""

    write_dev_server_project(project_dir=project_dir)
    state: DevServerState = DevServerState(
        run_compile=build_compile_callable(project_dir=project_dir)
    )
    app: DevApplication = create_dev_app(
        state=state,
        connection=build_fake_state_connection(),
        database="analytics",
        project_dir=project_dir,
    )

    def build() -> dict[str, object]:
        calls.append("build")
        return {"capturedAt": f"build-{len(calls)}", "models": {}, "sources": {}}

    state.attach_snapshot(StateSnapshot(build=build))
    return NativeTestClient(app)


def build_overlay_reader(
    *, project_dir: Path, connections: list[FakeAdapterConnection]
) -> OverlayReader:
    """Return an overlay reader recording every connection its factory opens."""

    write_dev_server_project(project_dir=project_dir)

    def connection_factory() -> AdapterConnection:
        connection: FakeAdapterConnection = build_fake_state_connection()
        connections.append(connection)
        return connection

    return OverlayReader(
        connection_factory=connection_factory,
        kafka_lag_reader=KafkaLagReader(),
    )
