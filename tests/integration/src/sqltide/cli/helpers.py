import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from textwrap import dedent
from typing import cast

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME
from sqltide.adapter.exceptions import AdapterAuthenticationError, AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterCapabilities,
    AdapterCapturedReplayRequest,
    AdapterConnectionConfig,
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterInvocationRecord,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterMutationResult,
    AdapterNodeResultRecord,
    AdapterQueryResult,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterRelationCleanupRequest,
    AdapterReplayCoverageRequest,
    AdapterReplayRequest,
    AdapterStableView,
    AdapterTable,
    AdapterTargetMutationLock,
    AdapterView,
    CatalogRelation,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse._helpers.inspection import load_clickhouse_catalog
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.build._helpers.execution_artifacts import render_direct_execution_json
from sqltide.cli.build._helpers.virtual_preview import build_virtual_build_preview
from sqltide.cli.build.constants import SQLTIDE_TOOL_VERSION
from sqltide.cli.build.main._run_build import run_build
from sqltide.cli.build.main.build_direct_build_preview import build_direct_build_preview
from sqltide.cli.build.models import (
    BuildCommandOptions,
    DirectBuildPreviewContext,
    VirtualBuildPreviewContext,
    WorkflowPreparationOptions,
)
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.cli.plan.main._render_direct_plan_json import render_direct_plan_json
from sqltide.cli.plan.main._run_plan import run_plan
from sqltide.cli.plan.main.render_plan_result import render_plan_result
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.cli.workflow_artifacts.main._publish_build_workflow import publish_build_workflow
from sqltide.compiler.discovery.main.load_project_input_for_path import (
    load_project_input_for_path,
)
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.backfill.main.assemble_virtual_build_workflow import (
    assemble_virtual_build_workflow,
)
from sqltide.executor.backfill.models import BackfillBootstrapRequest
from sqltide.executor.direct.main.assemble_direct_build_workflow import (
    assemble_direct_build_workflow,
)
from sqltide.executor.direct.main.build_direct_execution_result import (
    build_direct_execution_result,
)
from sqltide.executor.direct.main.execute_direct_build_workflow import (
    execute_direct_build_workflow,
)
from sqltide.executor.direct.main.persist_direct_fingerprints import (
    persist_direct_fingerprints,
)
from sqltide.executor.direct.models import (
    DirectBuildRequest,
    DirectBuildResult,
    DirectBuildWorkflow,
    DirectRuntimeExecution,
)
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    PublishedBuildWorkflow,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings

BACKFILL_PIPELINES_ROOT: Path = Path("tests/fixtures/basic_project/pipelines")
SELECTOR_PIPELINES_ROOT: Path = Path("tests/fixtures/selector_project/pipelines")


def _is_model_realization_sql(statement: str) -> bool:
    return (
        statement.startswith("CREATE MATERIALIZED VIEW ")
        or statement.startswith("CREATE VIEW ")
        or (statement.startswith("CREATE TABLE ") and " IF NOT EXISTS " not in statement)
    )


def _is_coverage_capture_sql(statement: str) -> bool:
    return "AS driving_input_relation_name" in statement


def write_managed_source_project(
    *, project_dir: Path, replay_boundary_mode: str = "offsets"
) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "integration_project"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    source_dir: Path = project_dir / "sources"
    source_dir.mkdir()
    (source_dir / "orders.yml").write_text(
        "sources:\n"
        "  - name: orders\n"
        "    kind: kafka\n"
        "    broker_list: kafka:9092\n"
        "    topic: source.orders\n"
        "    replay_boundary:\n"
        f"      mode: {replay_boundary_mode}\n",
        encoding="utf-8",
    )


class RecordingDelegatingConnection(AdapterConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        self._delegate: AdapterConnection = delegate
        self.catalog_load_count: int = 0
        self.query_statements: list[str] = []

    @property
    def adapter_identity(self) -> AdapterIdentity:
        return self._delegate.adapter_identity

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._delegate.capabilities

    def load_catalog(self, database: str) -> CatalogSnapshot:
        self.catalog_load_count += 1
        return load_clickhouse_catalog(
            connection=self,
            adapter_identity=self.adapter_identity,
            database=database,
        )

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        return self._delegate.metadata_columns(database=database, table=table)

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        return self._delegate.inspect_managed_table_state(database)

    def query(self, statement: str) -> AdapterQueryResult:
        self.query_statements.append(statement)
        return self._delegate.query(statement)

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        return self._delegate.execute_workflow_sql(statement)

    def render_terminal_observations(
        self,
        *,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> tuple[str, ...]:
        return self._delegate.render_terminal_observations(
            database=database,
            invocation=invocation,
            node_results=node_results,
        )

    def capture_warehouse_timestamp(self) -> str:
        return self._delegate.capture_warehouse_timestamp()

    def render_ensure_database(self, database: str) -> str:
        return self._delegate.render_ensure_database(database)

    def render_resource(
        self,
        *,
        resource: AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView,
        database: str,
        if_not_exists: bool = False,
    ) -> str:
        return self._delegate.render_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        return self._delegate.render_migrate_metadata_state(database)

    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]:
        return self._delegate.render_persist_metadata_state(database=database, state=state)

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        return self._delegate.load_deployment_inventory(database)

    def load_direct_fingerprints(
        self, *, database: str, logical_model_identities: tuple[str, ...]
    ) -> AdapterDirectFingerprintSnapshot:
        return self._delegate.load_direct_fingerprints(
            database=database,
            logical_model_identities=logical_model_identities,
        )

    def render_direct_fingerprint_observations(
        self,
        *,
        database: str,
        fingerprints: tuple[AdapterDirectFingerprintRecord, ...],
    ) -> tuple[str, ...]:
        return self._delegate.render_direct_fingerprint_observations(
            database=database,
            fingerprints=fingerprints,
        )

    def render_replay_coverage_query(self, request: AdapterReplayCoverageRequest) -> str:
        return self._delegate.render_replay_coverage_query(request)

    def render_replay_from_capture(self, request: AdapterCapturedReplayRequest) -> str:
        return self._delegate.render_replay_from_capture(request)

    def compare_readiness(
        self, request: AdapterReadinessRequest
    ) -> tuple[AdapterReadinessRootObservation, ...]:
        return self._delegate.compare_readiness(request)

    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]:
        return self._delegate.render_replace_stable_bindings(request)

    def render_cleanup_relations(self, request: AdapterRelationCleanupRequest) -> tuple[str, ...]:
        return self._delegate.render_cleanup_relations(request)

    def catalog_resource_matches(
        self,
        *,
        resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
        relation: CatalogRelation,
        database: str,
    ) -> bool:
        return self._delegate.catalog_resource_matches(
            resource=resource,
            relation=relation,
            database=database,
        )

    def acquire_target_mutation_lock(
        self, *, database: str, owner_id: str
    ) -> AdapterTargetMutationLock:
        return self._delegate.acquire_target_mutation_lock(
            database=database,
            owner_id=owner_id,
        )

    def release_target_mutation_lock(self, lock: AdapterTargetMutationLock) -> None:
        self._delegate.release_target_mutation_lock(lock)

    def close(self) -> None:
        self._delegate.close()


class FailOnceRealizationConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self._failed: bool = False

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        action: Callable[[str], AdapterMutationResult] = {
            True: self._reject_realization,
            False: super().execute_workflow_sql,
        }[not self._failed and _is_model_realization_sql(statement)]
        return action(statement)

    def _reject_realization(self, statement: str) -> AdapterMutationResult:
        del statement
        self._failed = True
        raise AdapterWarehouseError("injected failure after direct teardown")


class FailSecondReplayOnceConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self.replay_targets: list[str] = []
        self._replay_statements: set[str] = set()
        self._replay_count: int = 0
        self._failed: bool = False

    def render_replay_from_capture(self, request: AdapterCapturedReplayRequest) -> str:
        self.replay_targets.append(request.replay.relations.target)
        statement: str = super().render_replay_from_capture(request)
        self._replay_statements.add(statement.rstrip().rstrip(";"))
        return statement

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        is_replay: bool = statement.rstrip().rstrip(";") in self._replay_statements
        self._replay_count += int(is_replay)
        action: Callable[[str], AdapterMutationResult] = {
            True: self._reject_replay,
            False: super().execute_workflow_sql,
        }[not self._failed and self._replay_count == 2 and is_replay]
        return action(statement)

    def _reject_replay(self, statement: str) -> AdapterMutationResult:
        del statement
        self._failed = True
        raise AdapterWarehouseError("injected failure during second population segment")


class FailOnceDropConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self._failed: bool = False

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        action: Callable[[str], AdapterMutationResult] = {
            True: self._reject_drop,
            False: super().execute_workflow_sql,
        }[not self._failed and statement.startswith("DROP ") and "._sqltide_" not in statement]
        return action(statement)

    def _reject_drop(self, statement: str) -> AdapterMutationResult:
        del statement
        self._failed = True
        raise AdapterWarehouseError("injected failure during selected teardown")


class FailOnceViewRealizationConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self._failed: bool = False

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        action: Callable[[str], AdapterMutationResult] = {
            True: self._reject_view,
            False: super().execute_workflow_sql,
        }[not self._failed and statement.startswith("CREATE MATERIALIZED VIEW ")]
        return action(statement)

    def _reject_view(self, statement: str) -> AdapterMutationResult:
        del statement
        self._failed = True
        raise AdapterWarehouseError("injected failure during selected view attachment")


class FailOnceBoundaryQueryConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self._failed: bool = False

    def query(self, statement: str) -> AdapterQueryResult:
        action: Callable[[str], AdapterQueryResult] = {
            True: self._reject_boundary,
            False: super().query,
        }[not self._failed and _is_coverage_capture_sql(statement)]
        return action(statement)

    def _reject_boundary(self, statement: str) -> AdapterQueryResult:
        del statement
        self._failed = True
        raise AdapterWarehouseError("injected failure during selected boundary capture")


class DeniedDirectMetadataConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection, *, denied_database: str) -> None:
        super().__init__(delegate)
        self._denied_database: str = denied_database

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        action: Callable[..., frozenset[str]] = {
            True: self._reject_metadata_columns,
            False: super().metadata_columns,
        }[database == self._denied_database]
        return action(database=database, table=table)

    def load_direct_fingerprints(
        self, *, database: str, logical_model_identities: tuple[str, ...]
    ) -> AdapterDirectFingerprintSnapshot:
        del database, logical_model_identities
        return AdapterDirectFingerprintSnapshot(
            status="unavailable",
            baselines=(),
            warning="injected denied direct metadata read",
        )

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        action: Callable[[str], AdapterMutationResult] = {
            True: self._reject_metadata_write,
            False: super().execute_workflow_sql,
        }[self._denied_database in statement]
        return action(statement)

    def _reject_metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        del database, table
        raise AdapterAuthenticationError("injected denied direct metadata read")

    def _reject_metadata_write(self, statement: str) -> AdapterMutationResult:
        del statement
        raise AdapterAuthenticationError("injected denied direct metadata write")


class DirectActionRecordingConnection(RecordingDelegatingConnection):
    def __init__(self, delegate: AdapterConnection) -> None:
        super().__init__(delegate)
        self.command_statements: list[str] = []
        self.source_barrier_statements: list[str] = []
        self.realized_relation_names: list[str] = []
        self.replay_targets: list[str] = []
        self.replay_requests: list[AdapterReplayRequest] = []

    def render_resource(
        self,
        *,
        resource: AdapterManagedSource
        | AdapterTable
        | AdapterMaterializedView
        | AdapterView
        | AdapterStableView,
        database: str,
        if_not_exists: bool = False,
    ) -> str:
        self.realized_relation_names.append(resource.name)
        return super().render_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )

    def render_replay_from_capture(self, request: AdapterCapturedReplayRequest) -> str:
        self.replay_targets.append(request.replay.relations.target)
        self.replay_requests.append(request.replay)
        return super().render_replay_from_capture(request)

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        self.command_statements.extend(
            (statement.removesuffix(";"),)
            * int(statement.startswith("DROP ") and "._sqltide_" not in statement)
        )
        self.source_barrier_statements.extend((statement,) * int(".mv__orders" in statement))
        return super().execute_workflow_sql(statement)


class AdoptedLiveInsertConnection(DirectActionRecordingConnection):
    def __init__(
        self,
        delegate: AdapterConnection,
        *,
        clickhouse_client: Client,
        database: str,
        values_sql: str,
    ) -> None:
        super().__init__(delegate)
        self._clickhouse_client: Client = clickhouse_client
        self._database: str = database
        self._values_sql: str = values_sql
        self._realization_count: int = 0

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        result: AdapterMutationResult = super().execute_workflow_sql(statement)
        self._realization_count += int(_is_model_realization_sql(statement))
        action: Callable[[], None] = {
            True: self._insert_live_rows,
            False: self._do_nothing,
        }[self._realization_count == 2 and _is_model_realization_sql(statement)]
        action()
        return result

    def _do_nothing(self) -> None:
        return None

    def _insert_live_rows(self) -> None:
        self._clickhouse_client.command(
            f"INSERT INTO {self._database}.orders_existing VALUES {self._values_sql}"
        )


class ManagedLiveInsertConnection(DirectActionRecordingConnection):
    def __init__(
        self,
        delegate: AdapterConnection,
        *,
        clickhouse_client: Client,
        database: str,
        rows: tuple[tuple[str, int, int], ...],
    ) -> None:
        super().__init__(delegate)
        self._clickhouse_client: Client = clickhouse_client
        self._database: str = database
        self._rows: tuple[tuple[str, int, int], ...] = rows
        self._realization_count: int = 0

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        result: AdapterMutationResult = super().execute_workflow_sql(statement)
        self._realization_count += int(_is_model_realization_sql(statement))
        action: Callable[[], None] = {
            True: self._insert_live_rows,
            False: self._do_nothing,
        }[self._realization_count == 2 and _is_model_realization_sql(statement)]
        action()
        return result

    def _do_nothing(self) -> None:
        return None

    def _insert_live_rows(self) -> None:
        insert_landing_rows(
            clickhouse_client=self._clickhouse_client,
            database=self._database,
            rows=self._rows,
        )


def build_managed_clickhouse_client(
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    *,
    database: str,
) -> AdapterConnection:
    return ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=database,
        )
    )


def write_source_mode_plan_project(
    *, project_dir: Path, source_contents: str, model_contents: str
) -> Path:
    """Write one registry-backed plan project for a managed or adopted source mode."""

    pipelines_root: Path = project_dir / "pipelines"
    pipeline_dir: Path = pipelines_root / "pl__orders"
    pipeline_dir.mkdir(parents=True)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "source_mode_project"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    source_dir: Path = project_dir / "sources"
    source_dir.mkdir()
    (source_dir / "orders.yml").write_text(
        dedent(source_contents).strip() + "\n",
        encoding="utf-8",
    )
    (pipeline_dir / "orders_enriched.sql").write_text(
        dedent(model_contents).strip() + "\n",
        encoding="utf-8",
    )
    return pipelines_root


def write_audit_project_files(project_dir: Path) -> None:
    from tests.unit.src.sqltide.compiler.audit_discovery.helpers import (
        write_sql_audit_file,
    )
    from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
        write_pipeline_file,
    )

    write_managed_source_project(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "pipelines" / "pl__order_events" / "order_items.sql",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __ref("orders")
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "singular" / "order_events" / "negative_line_totals.sql",
        """
        AUDIT (
          severity warning,
          description "Line totals should not be negative",
        );

        SELECT order_id, line_total
        FROM __ref("order_items")
        WHERE line_total < 0 -- a trailing comment must not swallow the wrapper
        """,
    )


def write_backfill_audit_project_files(project_dir: Path) -> None:
    from tests.unit.src.sqltide.compiler.audit_discovery.helpers import (
        write_sql_audit_file,
    )
    from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
        write_pipeline_file,
    )

    write_managed_source_project(project_dir=project_dir, replay_boundary_mode="timestamp")
    write_pipeline_file(
        project_dir / "pipelines" / "pl__order_events" / "orders_enriched.sql",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(kafka_key AS String) AS order_id,
          CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp
        FROM __ref("orders")
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "singular" / "order_events" / "known_order_id.sql",
        """
        AUDIT (
          description "ord_001 should be flagged by the staged quality check",
        );

        SELECT order_id
        FROM __ref("orders_enriched")
        WHERE order_id = 'ord_001'
        """,
    )


def write_generic_audit_project_files(project_dir: Path) -> None:
    from tests.unit.src.sqltide.compiler.audit_discovery.helpers import (
        write_sql_audit_file,
    )
    from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
        write_pipeline_file,
    )

    write_managed_source_project(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "pipelines" / "pl__order_events" / "order_items.sql",
        """
        MODEL (
          order_by ["order_id"],
          columns (
            order_id (
              audits [not_null (name "order items order id not null", severity warning)],
            ),
          ),
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __ref("orders")
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "generic" / "not_null.sql",
        """
        AUDIT ();

        SELECT @column
        FROM __ref("@model")
        WHERE @column IS NULL
        """,
    )


def write_multi_audit_project_files(project_dir: Path) -> None:
    from tests.unit.src.sqltide.compiler.audit_discovery.helpers import (
        write_sql_audit_file,
    )
    from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
        write_pipeline_file,
    )

    write_managed_source_project(project_dir=project_dir)
    write_pipeline_file(
        project_dir / "pipelines" / "pl__order_events" / "order_items.sql",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __ref("orders")
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "singular" / "order_events" / "quality.sql",
        """
        AUDIT (
          name "negative line totals",
          severity warning,
        );

        SELECT order_id, line_total
        FROM __ref("order_items")
        WHERE line_total < 0;

        AUDIT (
          name "missing order ids",
          severity warning,
        );

        SELECT order_id
        FROM __ref("order_items")
        WHERE order_id = 'ord_missing'
        """,
    )


def write_error_audit_project_files(project_dir: Path) -> None:
    from tests.unit.src.sqltide.compiler.audit_discovery.helpers import write_sql_audit_file

    write_multi_audit_project_files(project_dir)
    write_sql_audit_file(
        project_dir / "audits" / "singular" / "order_events" / "quality.sql",
        """
        AUDIT (
          name "broken column",
        );

        SELECT missing_order_id
        FROM __ref("order_items");

        AUDIT (
          name "negative line totals",
          severity warning,
        );

        SELECT order_id, line_total
        FROM __ref("order_items")
        WHERE line_total < 0
        """,
    )


AUDIT_PROJECT_WRITER_BY_NAME: Mapping[str, Callable[[Path], None]] = {
    "singular": write_audit_project_files,
    "generic": write_generic_audit_project_files,
    "multi": write_multi_audit_project_files,
    "error": write_error_audit_project_files,
}

NULLABLE_ORDER_ITEMS_COLUMNS: str = "order_id Nullable(String), line_total Nullable(Float64)"
KEYED_ORDER_ITEMS_COLUMNS: str = "order_id String, line_total Nullable(Float64)"
UNORDERED_ORDER_ITEMS_ORDER_BY: str = "tuple()"
KEYED_ORDER_ITEMS_ORDER_BY: str = "(order_id)"


def write_audit_project_for(*, project_writer_name: str, project_dir: Path) -> None:
    """Write the audit project fixture selected by a test case."""

    AUDIT_PROJECT_WRITER_BY_NAME[project_writer_name](project_dir)


def build_order_items_ddl(*, database: str, columns: str, order_by: str) -> str:
    """Build the order-items table DDL a live audit test case expects."""

    return (
        f"CREATE TABLE {database}.tbl__order_items ({columns}) "
        f"ENGINE = MergeTree() ORDER BY {order_by}"
    )


def ensure_backfill_metadata_tables(
    *, managed_client: AdapterConnection, clickhouse_client: Client, database: str
) -> None:
    """Create the metadata tables so absent rows mean no deployment was recorded."""

    statement: str
    for statement in managed_client.render_migrate_metadata_state(database):
        _ = clickhouse_client.command(statement)


def load_deployment_status_rows(
    *, clickhouse_client: Client, database: str
) -> tuple[tuple[str, ...], ...]:
    """Load recorded deployment statuses in deployment order."""

    query: str = (
        f"SELECT 'backfilling' FROM {database}._sqltide_virtual_deployments ORDER BY deployment_id"
    )
    return tuple(_stringify_row(row) for row in clickhouse_client.query(query).result_rows)


def _stringify_row(row: Sequence[object]) -> tuple[str, ...]:
    return tuple(str(value) for value in row)


def load_selected_root_names(*, clickhouse_client: Client, database: str) -> tuple[str, ...]:
    """Load the selected root names recorded against every deployment."""

    query: str = (
        "SELECT logical_object_name FROM "
        f"{database}._sqltide_virtual_object_state "
        "WHERE state_kind = 'deployment' AND is_selected_root ORDER BY logical_object_name"
    )
    return tuple(str(row[0]) for row in clickhouse_client.query(query).result_rows)


SINGLE_EXPECTED_SQL_TEST: str = """
    TEST ();

    WITH
    helper_orders AS (
      SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
    ),
    __source__orders AS (
      SELECT * FROM helper_orders
    ),
    expected_rows AS (
      SELECT 'ord_001' AS order_id, {expected_line_total} AS line_total
    ),
    __expected__order_items AS (
      SELECT * FROM expected_rows
    )
    SELECT 1
    """

MULTI_NAMED_SQL_TESTS: str = """
    TEST (name "line total computes correctly");

    WITH
    helper_orders AS (
      SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
    ),
    __source__orders AS (
      SELECT * FROM helper_orders
    ),
    __expected__order_items AS (
      SELECT 'ord_001' AS order_id, 20.0 AS line_total
    )
    SELECT 1;

    TEST (name "line total remains stable on repeat");

    WITH
    helper_orders AS (
      SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
    ),
    __source__orders AS (
      SELECT * FROM helper_orders
    ),
    __expected__order_items AS (
      SELECT 'ord_001' AS order_id, 20.0 AS line_total
    )
    SELECT 1
    """

MULTI_TARGET_FAILING_SQL_TEST: str = """
    TEST ();

    WITH
    helper_orders AS (
      SELECT 'ord_001' AS order_id, 2 AS quantity, 10.0 AS unit_price
    ),
    __source__orders AS (
      SELECT * FROM helper_orders
    ),
    __expected__order_items AS (
      SELECT 'ord_001' AS order_id, 25.0 AS line_total
    ),
    __expected__daily_revenue AS (
      SELECT 'ord_001' AS order_id, 30.0 AS line_total
    )
    SELECT 1
    """

DOWNSTREAM_REF_SQL_TEST: str = """
    TEST ();

    WITH
    __ref__order_items AS (
      SELECT 'ord_001' AS order_id, 20.0 AS line_total
    ),
    __expected__daily_revenue AS (
      SELECT 'ord_001' AS order_id, 20.0 AS line_total
    )
    SELECT 1
    """


SEMANTICS_MODEL_SQL_BY_NAME: tuple[tuple[str, str], ...] = (
    (
        "order_items",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(quantity * unit_price AS Nullable(Float64)) AS line_total
        FROM __source("orders")
        """,
    ),
    (
        "daily_revenue",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total AS Nullable(Float64)) AS line_total
        FROM __ref("order_items")
        """,
    ),
    (
        "revenue_report",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total AS Nullable(Float64)) AS reported_total
        FROM __ref("daily_revenue")
        """,
    ),
    (
        "order_tax",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(line_total * 0.1 AS Nullable(Float64)) AS tax_total
        FROM __ref("order_items")
        """,
    ),
    (
        "order_summary",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(daily.order_id AS String) AS order_id,
          CAST(daily.line_total + tax.tax_total AS Nullable(Float64)) AS total_with_tax
        FROM __ref("daily_revenue") AS daily
        JOIN __ref("order_tax", ref_type='reference') AS tax ON daily.order_id = tax.order_id
        """,
    ),
)


def write_sql_test_semantics_project(
    *, project_dir: Path, sql_test_content: str, macro_file_contents: str
) -> None:
    """Write one three-model project used to prove SQL-test comparison semantics."""

    write_managed_source_project(project_dir=project_dir)
    pipeline_dir: Path = project_dir / "pipelines" / "pl__order_events"
    pipeline_dir.mkdir(parents=True)
    model_name: str
    model_sql: str
    for model_name, model_sql in SEMANTICS_MODEL_SQL_BY_NAME:
        (pipeline_dir / f"{model_name}.sql").write_text(
            dedent(model_sql).strip() + "\n", encoding="utf-8"
        )
    test_file_path: Path = project_dir / "tests" / "order_events" / "test_semantics.sql"
    test_file_path.parent.mkdir(parents=True, exist_ok=True)
    test_file_path.write_text(dedent(sql_test_content).strip() + "\n", encoding="utf-8")
    macro_dir: Path = project_dir / "macros"
    macro_dir.mkdir(parents=True, exist_ok=True)
    (macro_dir / "helpers.py").write_text(
        dedent(macro_file_contents).strip() + "\n", encoding="utf-8"
    )


DIRECT_SCOPE_MODEL_RELATIONS: tuple[str, ...] = (
    "tbl__alpha",
    "mv__alpha",
    "tbl__beta",
    "mv__beta",
    "tbl__gamma",
    "mv__gamma",
    "tbl__delta",
    "mv__delta",
)
_RELATION_NAMES_BY_FLAG: dict[bool, tuple[str, ...]] = {
    False: (),
    True: DIRECT_SCOPE_MODEL_RELATIONS,
}


def settle_direct_scope_warehouse(
    *,
    connection: AdapterConnection,
    clickhouse_client: Client,
    database: str,
    create_relations: bool,
) -> None:
    """Create the scope project's warehouse relations when requested."""

    statement: str
    for statement in connection.render_migrate_metadata_state(database):
        _ = clickhouse_client.command(statement)
    relation_name: str
    for relation_name in _RELATION_NAMES_BY_FLAG[create_relations]:
        clickhouse_client.command(
            f"CREATE TABLE IF NOT EXISTS {database}.{relation_name} "
            "(order_id UInt64) ENGINE = MergeTree ORDER BY order_id"
        )


def run_direct_plan(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    selectors: tuple[str, ...] = (),
    start_time: str | None = None,
    changed: bool = False,
) -> int:
    """Run `sqt plan` in direct mode against a live warehouse connection."""

    return run_plan(
        options=PlanCommandOptions(
            pipelines_root=project_root / "pipelines",
            database=database,
            selectors=selectors,
            full_refresh=False,
            start_time=start_time,
            deployment_id=None,
            json_output=True,
            verbose=False,
            changed=changed,
        ),
        client=connection,
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )


def read_workflow_artifact(
    *, artifact_root: Path, is_template: bool = False
) -> tuple[bytes, bytes, tuple[str, ...], tuple[bytes, ...]]:
    """Read one complete exact workflow artifact without interpreting its contents."""

    step_pattern: str = {False: "*.sql", True: "*.sql.template"}[is_template]
    workflow_name: str = {
        False: "workflow.sql",
        True: "workflow.template.sql",
    }[is_template]
    step_paths: tuple[Path, ...] = tuple(sorted((artifact_root / "steps").glob(step_pattern)))
    return (
        (artifact_root / "plan.json").read_bytes(),
        (artifact_root / workflow_name).read_bytes(),
        tuple(path.name for path in step_paths),
        tuple(path.read_bytes() for path in step_paths),
    )


def plan_scope_names(*, plan_json: str) -> tuple[str, ...]:
    """Return the execution scope model names reported by one direct plan."""

    payload: dict[str, object] = json.loads(plan_json)
    scope: list[dict[str, str]] = cast(list[dict[str, str]], payload["execution_scope"])
    return tuple(key["name"] for key in scope)


def plan_replay_root_models(*, plan_json: str) -> tuple[str, ...]:
    """Return the replay root model names reported by one direct plan."""

    payload: dict[str, object] = json.loads(plan_json)
    roots: list[dict[str, object]] = cast(list[dict[str, object]], payload["replay_roots"])
    return tuple(cast(dict[str, str], root["model_key"])["name"] for root in roots)


def plan_relation_operations(*, plan_json: str) -> tuple[tuple[str, str], ...]:
    """Return every teardown and creation operation reported by one direct plan."""

    payload: dict[str, object] = json.loads(plan_json)
    operations: list[dict[str, object]] = [
        *cast(list[dict[str, object]], payload["teardown"]),
        *cast(list[dict[str, object]], payload["creation"]),
    ]
    return tuple(
        (str(operation["action"]), str(operation["relation_name"])) for operation in operations
    )


DIRECT_BUILD_MODEL_SQL: str = (
    "SELECT\n"
    "  kafka_key::String AS order_id,\n"
    "  _replay_partition::Int32 AS _replay_partition,\n"
    "  _replay_offset::Int64 AS _replay_offset\n"
    'FROM __source("orders")'
)
_DIRECT_BUILD_LANDED_AT_MODEL_SQL: str = (
    "SELECT\n"
    "  kafka_key::String AS order_id,\n"
    "  _replay_landed_at::DateTime64(3) AS _replay_landed_at\n"
    'FROM __source("orders")'
)
_DIRECT_BUILD_MODEL_SQL_BY_REPLAY_MODE: dict[str, str] = {
    "offsets": DIRECT_BUILD_MODEL_SQL,
    "landed_at": _DIRECT_BUILD_LANDED_AT_MODEL_SQL,
}
DIRECT_BUILD_MODEL_NAME: str = "orders_enriched"
DIRECT_BUILD_TARGET_TABLE_NAME: str = "tbl__orders_enriched"
DIRECT_BUILD_LANDING_TABLE_NAME: str = "raw__orders"
_DIRECT_BUILD_SOURCE_YML: str = (
    "sources:\n"
    "  - name: orders\n"
    "    kind: kafka\n"
    "    broker_list: {broker_list}\n"
    "    topic: {topic}\n"
    "    replay_boundary: {{mode: {replay_boundary_mode}}}\n"
)
_DIRECT_BUILD_SETTINGS_BY_MODE: dict[bool, str] = {
    False: "",
    True: '\n[defaults]\npipeline_mode = "virtual"\n',
}
_DIRECT_SELECTED_GRAPH_SQL_BY_NAME: tuple[tuple[str, str], ...] = (
    (
        "alpha",
        "SELECT kafka_key::String AS order_id, "
        "_replay_partition::Int32 AS _replay_partition, "
        "_replay_offset::Int64 AS _replay_offset "
        'FROM __source("orders")',
    ),
    (
        "beta",
        "SELECT order_id::String AS order_id, "
        "_replay_partition::Int32 AS _replay_partition, "
        "_replay_offset::Int64 AS _replay_offset "
        'FROM __ref("alpha")',
    ),
    (
        "gamma",
        "SELECT order_id::String AS order_id, "
        "concat(order_id, '-gamma')::String AS gamma_marker, "
        "_replay_partition::Int32 AS _replay_partition, "
        "_replay_offset::Int64 AS _replay_offset "
        'FROM __ref("beta")',
    ),
    (
        "delta",
        "SELECT a.order_id::String AS order_id, g.gamma_marker::String AS gamma_marker, "
        "a._replay_partition::Int32 AS _replay_partition, "
        "a._replay_offset::Int64 AS _replay_offset "
        'FROM __ref("alpha") AS a INNER JOIN '
        '__ref("gamma", ref_type="reference") AS g ON a.order_id = g.order_id',
    ),
)


def write_direct_build_project(
    *,
    project_root: Path,
    topic: str = "source.orders",
    broker_list: str = "kafka:9092",
    audit_sql_by_name: tuple[tuple[str, str], ...] = (),
    virtual_environments: bool = False,
    relation_name: str = DIRECT_BUILD_TARGET_TABLE_NAME,
    replay_boundary_mode: str = "offsets",
    source_settings: tuple[tuple[str, str], ...] = (),
) -> None:
    """Write a managed Kafka direct-mode project with one replayable model."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    audit_root: Path = project_root / "audits"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    audit_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_build"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n'
        f"{_DIRECT_BUILD_SETTINGS_BY_MODE[virtual_environments]}",
        encoding="utf-8",
    )
    (source_root / "orders.yml").write_text(
        _DIRECT_BUILD_SOURCE_YML.format(
            broker_list=broker_list,
            topic=topic,
            replay_boundary_mode=replay_boundary_mode,
        )
        + _direct_build_source_settings_yml(source_settings=source_settings),
        encoding="utf-8",
    )
    (pipeline_root / f"{DIRECT_BUILD_MODEL_NAME}.sql").write_text(
        f'MODEL (relation_name {relation_name}, order_by ["order_id"]);\n'
        f"{_DIRECT_BUILD_MODEL_SQL_BY_REPLAY_MODE[replay_boundary_mode]}\n",
        encoding="utf-8",
    )
    audit_name: str
    audit_sql: str
    for audit_name, audit_sql in audit_sql_by_name:
        (audit_root / audit_name).write_text(audit_sql, encoding="utf-8")


def _direct_build_source_settings_yml(*, source_settings: tuple[tuple[str, str], ...]) -> str:
    settings_lines: str = "".join(
        f"      {setting_name}: '{setting_value}'\n"
        for setting_name, setting_value in source_settings
    )
    return {True: f"    settings:\n{settings_lines}", False: ""}[bool(source_settings)]


def write_direct_postgres_refresh_project(
    *,
    project_root: Path,
    host: str,
    port: int,
    database: str,
    user: str,
    password_env: str,
    refresh: str,
) -> None:
    """Write one scheduled PostgreSQL refresh project for full direct execution."""

    pipeline_root: Path = project_root / "pipelines" / "pl__pg__course"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "postgres_refresh"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "inventory.yml").write_text(
        "sources:\n"
        "  - name: inventory__course\n"
        "    kind: postgres\n"
        f"    host: {host}\n"
        f"    port: {port}\n"
        f"    database: {database}\n"
        "    table: course\n"
        f"    user: {user}\n"
        f"    password_env: {password_env}\n"
        f"    refresh: {refresh}\n"
        "    append: true\n",
        encoding="utf-8",
    )
    (pipeline_root / "pg__course.sql").write_text(
        'MODEL (relation_name pg__course, engine "ReplacingMergeTree()", '
        'order_by ["course_key"]);\n'
        "SELECT CAST(course_key AS String) AS course_key, "
        "CAST(country_normalised AS String) AS country_normalised "
        'FROM __source("inventory__course")\n',
        encoding="utf-8",
    )


def write_direct_selected_graph_project(*, project_root: Path) -> None:
    """Write the selected-rebuild fan-in graph with replay lineage on every model."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_selected_graph"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "orders.yml").write_text(
        _DIRECT_BUILD_SOURCE_YML.format(
            broker_list="kafka:9092",
            topic="source.selected_orders",
            replay_boundary_mode="offsets",
        ),
        encoding="utf-8",
    )
    model_name: str
    model_sql: str
    for model_name, model_sql in _DIRECT_SELECTED_GRAPH_SQL_BY_NAME:
        (pipeline_root / f"{model_name}.sql").write_text(
            f'MODEL (order_by ["order_id"]);\n{model_sql}\n', encoding="utf-8"
        )


def write_virtual_fan_in_project(*, project_root: Path) -> None:
    """Write a virtual fan-in graph driven by one adopted replay source."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "virtual_fan_in"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "orders.yml").write_text(
        "sources:\n"
        "  - kind: stream_table\n"
        "    name: orders\n"
        "    table_name: fan_in_orders_input\n"
        "    replay_boundary:\n"
        "      mode: offsets\n"
        "      columns:\n"
        "        _replay_partition: event_partition\n"
        "        _replay_offset: event_offset\n"
        "        _replay_timestamp: event_timestamp\n",
        encoding="utf-8",
    )
    model_name: str
    model_sql: str
    for model_name, model_sql in _DIRECT_SELECTED_GRAPH_SQL_BY_NAME:
        authored_sql: str = model_sql.replace("kafka_key", "order_id")
        (pipeline_root / f"{model_name}.sql").write_text(
            f'MODEL (order_by ["order_id"]);\n{authored_sql}\n', encoding="utf-8"
        )


def write_direct_view_project(*, project_root: Path) -> None:
    """Write a terminal ordinary view over two adopted input relations."""

    pipeline_root: Path = project_root / "pipelines" / "pl__consumer"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_view"\ndefault_target = "test"\n\n[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "inputs.yml").write_text(
        "sources:\n"
        "  - name: orders\n"
        "    kind: stream_table\n"
        "    table_name: direct_orders_input\n"
        "    replay_boundary:\n"
        "      mode: timestamp\n"
        "      columns: {_replay_timestamp: event_timestamp}\n"
        "  - name: customers\n"
        "    kind: stream_table\n"
        "    table_name: direct_customers_input\n"
        "    replay_boundary:\n"
        "      mode: timestamp\n"
        "      columns: {_replay_timestamp: event_timestamp}\n",
        encoding="utf-8",
    )
    (pipeline_root / "customer_orders.sql").write_text(
        "MODEL (kind view, relation_name customer_orders);\n"
        "SELECT orders.order_id::String AS order_id, "
        "customers.customer_name::String AS customer_name\n"
        'FROM __source("orders") AS orders\n'
        'INNER JOIN __source("customers") AS customers\n'
        "ON orders.customer_id = customers.customer_id\n",
        encoding="utf-8",
    )


def write_virtual_environment_view_project(*, project_root: Path) -> None:
    """Write a custom table feeding a multi-upstream terminal view."""

    pipeline_root: Path = project_root / "pipelines" / "pl__consumer"
    source_root: Path = project_root / "sources"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "virtual_environment_view"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "inputs.yml").write_text(
        "sources:\n"
        "  - name: orders\n"
        "    kind: stream_table\n"
        "    table_name: vde_orders_input\n"
        "    replay_boundary:\n"
        "      mode: offsets\n"
        "      columns:\n"
        "        _replay_partition: event_partition\n"
        "        _replay_offset: event_offset\n"
        "        _replay_timestamp: event_timestamp\n"
        "  - name: customers\n"
        "    kind: stream_table\n"
        "    table_name: vde_customers_input\n"
        "    replay_boundary:\n"
        "      mode: timestamp\n"
        "      columns: {_replay_timestamp: event_timestamp}\n",
        encoding="utf-8",
    )
    write_virtual_environment_table_model(
        project_root=project_root,
        relation_name="order_facts",
    )
    write_virtual_environment_view_model(
        project_root=project_root,
        customer_name_expression="customers.customer_name::String",
    )


def write_virtual_environment_view_model(
    *,
    project_root: Path,
    customer_name_expression: str,
    relation_name: str = "customer_orders",
) -> None:
    """Write one authored query revision for the terminal view."""

    (project_root / "pipelines" / "pl__consumer" / "customer_orders.sql").write_text(
        f"MODEL (kind view, relation_name {relation_name});\n"
        "SELECT orders.order_id::String AS order_id, "
        f"{customer_name_expression} AS customer_name\n"
        'FROM __ref("orders_enriched") AS orders\n'
        'INNER JOIN __source("customers") AS customers\n'
        "ON orders.customer_id = customers.customer_id\n",
        encoding="utf-8",
    )


def write_virtual_environment_table_model(*, project_root: Path, relation_name: str) -> None:
    """Write one effective relation-name revision for the replayable table model."""

    (project_root / "pipelines" / "pl__consumer" / "orders_enriched.sql").write_text(
        f'MODEL (relation_name {relation_name}, engine "MergeTree()", '
        'order_by ["order_id"]);\n'
        "SELECT order_id::String AS order_id, customer_id::UInt64 AS customer_id, "
        "_replay_partition::Int32 AS _replay_partition, "
        "_replay_offset::Int64 AS _replay_offset "
        'FROM __source("orders")\n',
        encoding="utf-8",
    )


def write_direct_aggregate_project(*, project_root: Path) -> None:
    """Write an alpha-to-aggregate-beta direct rebuild project."""

    write_direct_selected_graph_project(project_root=project_root)
    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    (pipeline_root / "gamma.sql").unlink()
    (pipeline_root / "delta.sql").unlink()
    (pipeline_root / "beta.sql").write_text(
        'MODEL (order_by ["order_id"]);\n'
        "SELECT order_id::String AS order_id, count()::UInt64 AS order_count "
        'FROM __ref("alpha") GROUP BY order_id\n',
        encoding="utf-8",
    )


def write_direct_selected_graph_audits(
    *, project_root: Path, audit_sql_by_name: tuple[tuple[str, str], ...]
) -> None:
    """Write discovered audits for the selected-rebuild graph."""

    audit_root: Path = project_root / "audits"
    audit_root.mkdir(exist_ok=True)
    audit_name: str
    audit_sql: str
    for audit_name, audit_sql in audit_sql_by_name:
        (audit_root / audit_name).write_text(audit_sql, encoding="utf-8")


def write_direct_adopted_source_project(
    *, project_root: Path, source_yml: str, model_sql: str
) -> None:
    """Write a direct project driven by one adopted source relation."""

    pipeline_root: Path = project_root / "pipelines" / "pl__orders"
    source_root: Path = project_root / "sources"
    audit_root: Path = project_root / "audits"
    pipeline_root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    audit_root.mkdir(parents=True, exist_ok=True)
    (project_root / "sqltide_project.toml").write_text(
        'name = "direct_adopted_source"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    (source_root / "orders.yml").write_text(source_yml, encoding="utf-8")
    (pipeline_root / "orders_enriched.sql").write_text(model_sql, encoding="utf-8")
    (audit_root / "adopted_target.sql").write_text(
        'AUDIT (description "adopted target is live");\n'
        "SELECT 'adopted-audit-marker' AS marker "
        'FROM __ref("orders_enriched") WHERE 0\n',
        encoding="utf-8",
    )


def run_direct_build(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    selectors: tuple[str, ...] = (),
    json_output: bool = True,
    auto_approve: bool = True,
    start_time: str | None = None,
    metadata_database: str | None = None,
    changed: bool = False,
) -> int:
    """Run `sqt build` in direct mode against a live warehouse connection."""

    return run_build(
        options=BuildCommandOptions(
            pipelines_root=project_root / "pipelines",
            database=database,
            metadata_database=metadata_database or database,
            selectors=selectors,
            json_output=json_output,
            verbose=False,
            auto_approve=auto_approve,
            start_time=start_time,
            changed=changed,
        ),
        client=connection,
        observation_client=connection,
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )


def insert_landing_rows(
    *,
    clickhouse_client: Client,
    database: str,
    rows: tuple[tuple[str, int, int], ...],
) -> None:
    """Insert replayable landing rows directly into the preserved raw relation."""

    values: str = ", ".join(
        f"('{order_key}', '', '', {partition_value}, {offset_value}, now64(3), "
        f"{partition_value}, {offset_value}, now64(3), [], [], now64(3), now64(3))"
        for order_key, partition_value, offset_value in rows
    )
    clickhouse_client.command(
        f"INSERT INTO {database}.{DIRECT_BUILD_LANDING_TABLE_NAME} "
        "(kafka_key, kafka_value, kafka_topic, kafka_partition, kafka_offset, kafka_timestamp, "
        "_replay_partition, _replay_offset, _replay_timestamp, kafka_header_keys, "
        "kafka_header_values, kafka_landed_at, "
        f"_replay_landed_at) VALUES {values}"
    )


DIRECT_BUILD_KAFKA_TABLE_NAME: str = "kafka__orders"
DIRECT_BUILD_LANDING_VIEW_NAME: str = "mv__orders"
DIRECT_BUILD_STAGED_KAFKA_TABLE_NAME: str = "_sqltide_next__kafka__orders"
_SECOND_PRECISION_LANDING_VIEW_SQL: str = (
    "CREATE MATERIALIZED VIEW {database}.{view_name} TO {database}.{landing_table_name} AS\n"
    "SELECT\n"
    "    _key AS kafka_key,\n"
    "    message AS kafka_value,\n"
    "    _topic AS kafka_topic,\n"
    "    _partition AS kafka_partition,\n"
    "    _offset AS kafka_offset,\n"
    "    _timestamp AS kafka_timestamp,\n"
    "    _partition AS _replay_partition,\n"
    "    _offset AS _replay_offset,\n"
    "    _timestamp AS _replay_timestamp,\n"
    "    _headers.name AS kafka_header_keys,\n"
    "    _headers.value AS kafka_header_values,\n"
    "    now64(3) AS kafka_landed_at,\n"
    "    now64(3) AS _replay_landed_at\n"
    "FROM {database}.{kafka_table_name}"
)


def replace_landing_view_with_second_precision(*, clickhouse_client: Client, database: str) -> None:
    """Recreate the landing view as releases before millisecond landing created it."""

    clickhouse_client.command(
        f"DROP TABLE IF EXISTS {database}.{DIRECT_BUILD_LANDING_VIEW_NAME} SYNC"
    )
    clickhouse_client.command(
        _SECOND_PRECISION_LANDING_VIEW_SQL.format(
            database=database,
            view_name=DIRECT_BUILD_LANDING_VIEW_NAME,
            landing_table_name=DIRECT_BUILD_LANDING_TABLE_NAME,
            kafka_table_name=DIRECT_BUILD_KAFKA_TABLE_NAME,
        )
    )


def landing_relation_uuids(*, clickhouse_client: Client, database: str) -> tuple[str, str]:
    """Return the Kafka engine and raw landing table UUIDs, which change on recreation."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, toString(uuid) FROM system.tables "
        "WHERE database = {database:String} AND name IN ({names:Array(String)})",
        parameters={
            "database": database,
            "names": [DIRECT_BUILD_KAFKA_TABLE_NAME, DIRECT_BUILD_LANDING_TABLE_NAME],
        },
    ).result_rows
    uuid_by_name: dict[str, str] = {str(row[0]): str(row[1]) for row in rows}
    return (
        uuid_by_name[DIRECT_BUILD_KAFKA_TABLE_NAME],
        uuid_by_name[DIRECT_BUILD_LANDING_TABLE_NAME],
    )


def landing_view_timestamp_projection(*, clickhouse_client: Client, database: str) -> str:
    """Return the broker timestamp column the deployed landing view projects."""

    return str(
        clickhouse_client.query(
            "SELECT extract(create_table_query, '([_a-z0-9]+) AS kafka_timestamp') "
            "FROM system.tables WHERE database = {database:String} AND name = {name:String}",
            parameters={"database": database, "name": DIRECT_BUILD_LANDING_VIEW_NAME},
        ).result_rows[0][0]
    )


def run_direct_full_build(project_root: Path, database: str, connection: AdapterConnection) -> int:
    """Run an all-models direct build."""

    return run_direct_build(project_root=project_root, database=database, connection=connection)


def run_direct_changed_build(
    project_root: Path, database: str, connection: AdapterConnection
) -> int:
    """Run a changed-only direct build."""

    return run_direct_build(
        project_root=project_root, database=database, connection=connection, changed=True
    )


def run_virtual_build(project_root: Path, database: str, connection: AdapterConnection) -> int:
    """Run a virtual build."""

    return run_virtual_environment_build(
        project_root=project_root, database=database, connection=connection
    )


def insert_landing_rows_after_delay(
    *,
    connection_settings: ClickHouseConnectionSettings,
    database: str,
    rows: tuple[tuple[str, int, int], ...],
    delay_seconds: float,
) -> None:
    """Land rows on an independent connection while a build is stabilizing."""

    time.sleep(delay_seconds)
    clickhouse_client: Client = clickhouse_connect.get_client(
        host=connection_settings.host,
        port=connection_settings.port,
        username=connection_settings.username,
        password=connection_settings.password,
    )
    try:
        insert_landing_rows(clickhouse_client=clickhouse_client, database=database, rows=rows)
    finally:
        clickhouse_client.close()


def execute_direct_build_directly(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    stabilization_seconds: float,
    selectors: tuple[str, ...],
) -> DirectBuildResult:
    """Plan and execute one direct build with an explicit stabilization window."""

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_root / "pipelines",
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    preview: DirectBuildPreviewContext = build_direct_build_preview(
        options=WorkflowPreparationOptions(
            database=database,
            metadata_database=database,
            selectors=selectors,
            deployment_id=None,
            full_refresh=False,
            start_time=None,
            verbose=False,
        ),
        client=connection,
        analysis=analysis,
    )
    request: DirectBuildRequest = DirectBuildRequest(
        plan=preview.plan,
        realized_project=preview.analysis.realized_project,
        database=preview.database,
        metadata_database=preview.metadata_database,
        tool_version=SQLTIDE_TOOL_VERSION,
        stabilization_seconds=stabilization_seconds,
    )
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=preview.warehouse_snapshot,
        plan_json=render_direct_plan_json(plan=preview.plan, adapter_name=preview.adapter_name),
    )
    runtime_execution: DirectRuntimeExecution = execute_direct_build_workflow(
        workflow=workflow,
        connection=connection,
    )
    _ = publish_build_workflow(
        target_dir=project_root / "target",
        workflow=runtime_execution.workflow,
        execution_json=render_direct_execution_json(
            request=request,
            status="succeeded",
            captures=runtime_execution.captures,
            execution=runtime_execution.execution,
            failed_step_id=None,
            error_message=None,
        ),
    )
    _ = persist_direct_fingerprints(request=request, connection=connection)
    return build_direct_execution_result(
        request=request,
        execution=runtime_execution.execution,
        captures=runtime_execution.captures,
    ).build_result


def publish_direct_workflow(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    selectors: tuple[str, ...] = (),
    effective_start_time: str | None = None,
    stabilization_seconds: float = 0,
) -> PublishedBuildWorkflow:
    """Assemble and publish one direct workflow without executing it."""

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_root / "pipelines",
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    preview: DirectBuildPreviewContext = build_direct_build_preview(
        options=WorkflowPreparationOptions(
            database=database,
            metadata_database=database,
            selectors=selectors,
            deployment_id=None,
            full_refresh=False,
            start_time=effective_start_time,
            verbose=False,
        ),
        client=connection,
        analysis=analysis,
        effective_start_time=effective_start_time,
    )
    request: DirectBuildRequest = DirectBuildRequest(
        plan=preview.plan,
        realized_project=preview.analysis.realized_project,
        database=preview.database,
        metadata_database=preview.metadata_database,
        tool_version=SQLTIDE_TOOL_VERSION,
        stabilization_seconds=stabilization_seconds,
    )
    workflow: DirectBuildWorkflow = assemble_direct_build_workflow(
        request=request,
        client=connection,
        snapshot=preview.warehouse_snapshot,
        plan_json=render_direct_plan_json(plan=preview.plan, adapter_name=preview.adapter_name),
    )
    runtime_execution: DirectRuntimeExecution = execute_direct_build_workflow(
        workflow=workflow,
        connection=connection,
    )
    return publish_build_workflow(
        target_dir=project_root / "target",
        workflow=runtime_execution.workflow,
        execution_json=render_direct_execution_json(
            request=request,
            status="succeeded",
            captures=runtime_execution.captures,
            execution=runtime_execution.execution,
            failed_step_id=None,
            error_message=None,
        ),
    )


def publish_virtual_workflow(
    *, project_root: Path, database: str, deployment_id: str, connection: AdapterConnection
) -> PublishedBuildWorkflow:
    """Assemble and publish one fixed-identity virtual workflow without executing it."""

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_root / "pipelines",
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    options: WorkflowPreparationOptions = WorkflowPreparationOptions(
        database=database,
        metadata_database=database,
        selectors=(),
        deployment_id=deployment_id,
        full_refresh=False,
        start_time=None,
        verbose=False,
    )
    preview: VirtualBuildPreviewContext = build_virtual_build_preview(
        options=options,
        start_time_utc=None,
        client=connection,
        analysis=analysis,
    )
    plan_payload: dict[str, object] = json.loads(
        render_plan_result(
            plan=preview.plan,
            desired_state=preview.desired_state,
            database=preview.database,
            adapter_name=connection.adapter_identity.name,
            json_output=True,
            verbose=False,
        )
    )
    plan_payload["deployment_created_at"] = preview.created_at
    request: BackfillBootstrapRequest = BackfillBootstrapRequest(
        desired_state=preview.desired_state,
        default_database=preview.database,
        metadata_database=preview.metadata_database,
        replay_lineage_mode=preview.replay_lineage_mode,
        confirmed_plan=preview.plan,
        deployment_id=preview.deployment_id,
        full_refresh_keys=preview.full_refresh_keys,
        start_time_keys=preview.start_time_keys,
        start_time=preview.start_time,
        created_at=preview.created_at,
        confirmed_target_catalog=preview.target_catalog,
        confirmed_metadata_catalog=preview.metadata_catalog,
    )
    workflow: BuildWorkflow = assemble_virtual_build_workflow(
        request=request,
        client=connection,
        plan_json=json.dumps(plan_payload, indent=2),
    )
    return publish_build_workflow(target_dir=project_root / "target", workflow=workflow)


def execute_clickhouse_client_sql(
    *, settings: ClickHouseConnectionSettings, sql: str
) -> tuple[int, str]:
    """Execute emitted SQL through the container's manual multiquery client."""

    completed: subprocess.CompletedProcess[str] = subprocess.run(
        (
            "docker",
            "exec",
            settings.container_id,
            "clickhouse-client",
            "--user",
            settings.username,
            "--password",
            settings.password,
            "--multiquery",
            "--query",
            sql,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stderr


def execute_warehouse_statements(
    *,
    clickhouse_client: Client,
    database: str,
    statements: tuple[str, ...],
) -> None:
    """Run explicit warehouse statements while arranging one integration scenario."""

    statement: str
    for statement in statements:
        clickhouse_client.command(statement.format(database=database))


def run_virtual_environment_build(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    deployment_id: str | None = None,
) -> int:
    """Run `sqt build` in virtual mode against a live warehouse connection."""

    return run_build(
        options=BuildCommandOptions(
            pipelines_root=project_root / "pipelines",
            database=database,
            metadata_database=database,
            selectors=(),
            deployment_id=deployment_id,
            full_refresh=False,
            start_time=None,
            json_output=True,
            verbose=False,
            auto_approve=True,
        ),
        client=connection,
        observation_client=connection,
        loaded_project=load_project_input_for_path(path=project_root),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )


def run_new_virtual_environment_deployment(
    *,
    project_root: Path,
    database: str,
    connection: AdapterConnection,
    clickhouse_client: Client,
) -> AdapterDeploymentRecord:
    """Run backfill and return the deployment newly persisted by that invocation."""

    ensure_backfill_metadata_tables(
        managed_client=connection,
        clickhouse_client=clickhouse_client,
        database=database,
    )
    previous_inventory: AdapterDeploymentInventory = connection.load_deployment_inventory(database)
    previous_ids: frozenset[str] = frozenset(
        deployment.deployment_id for deployment in previous_inventory.deployments
    )
    exit_code: int = run_virtual_environment_build(
        project_root=project_root,
        database=database,
        connection=connection,
    )
    assert exit_code == 0
    inventory: AdapterDeploymentInventory = connection.load_deployment_inventory(database)
    deployment_by_id: dict[str, AdapterDeploymentRecord] = {
        deployment.deployment_id: deployment for deployment in inventory.deployments
    }
    new_deployment_ids: set[str] = set(deployment_by_id) - previous_ids
    new_deployment_id: str = new_deployment_ids.pop()
    return deployment_by_id[new_deployment_id]


def virtual_environment_view_rows(
    *, clickhouse_client: Client, database: str, view_name: str = "customer_orders"
) -> tuple[tuple[str, str], ...]:
    """Return stable terminal-view rows in deterministic order."""

    return tuple(
        (str(row[0]), str(row[1]))
        for row in clickhouse_client.query(
            f"SELECT order_id, customer_name FROM {database}.{view_name} ORDER BY order_id"
        ).result_rows
    )


def prepare_virtual_environment_view_sources(*, clickhouse_client: Client, database: str) -> None:
    """Create and seed the adopted inputs used by VDE terminal-view scenarios."""

    clickhouse_client.command(
        f"CREATE TABLE {database}.vde_orders_input "
        "(order_id String, customer_id UInt64, event_partition Int32, "
        "event_offset Int64, event_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY order_id"
    )
    clickhouse_client.command(
        f"CREATE TABLE {database}.vde_customers_input "
        "(customer_id UInt64, customer_name String, event_timestamp DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY customer_id"
    )
    clickhouse_client.command(
        f"INSERT INTO {database}.vde_orders_input VALUES "
        "('order-1', 1, 0, 1, '2026-07-31 00:00:01.000'), "
        "('order-2', 2, 0, 2, '2026-07-31 00:00:02.000')"
    )
    clickhouse_client.command(
        f"INSERT INTO {database}.vde_customers_input VALUES "
        "(1, 'Ada', '2026-07-31 00:00:01.000'), "
        "(2, 'Grace', '2026-07-31 00:00:02.000')"
    )


def deployment_watermark_count(
    *, connection: AdapterConnection, database: str, deployment_id: str
) -> int:
    """Return the number of persisted replay watermarks for one deployment."""

    return int(
        str(
            connection.query(
                f"SELECT count() FROM {database}.{METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME} "
                f"WHERE deployment_id = '{deployment_id}'"
            ).rows[0][0]
        )
    )


def managed_source_relation_state(
    *, clickhouse_client: Client, database: str
) -> dict[str, tuple[str, str]]:
    """Return each managed source relation's UUID and DDL, including any staged source."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT name, toString(uuid), create_table_query FROM system.tables "
        "WHERE database = {database:String} AND name IN ({names:Array(String)})",
        parameters={
            "database": database,
            "names": [
                DIRECT_BUILD_KAFKA_TABLE_NAME,
                DIRECT_BUILD_STAGED_KAFKA_TABLE_NAME,
                DIRECT_BUILD_LANDING_TABLE_NAME,
                DIRECT_BUILD_LANDING_VIEW_NAME,
            ],
        },
    ).result_rows
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def direct_build_order_ids(*, clickhouse_client: Client, database: str) -> tuple[str, ...]:
    """Return the order ids currently materialized in the direct build target."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {database}.{DIRECT_BUILD_TARGET_TABLE_NAME} ORDER BY order_id"
    ).result_rows
    return tuple(str(row[0]) for row in rows)


def warehouse_row_count(*, clickhouse_client: Client, database: str, statement: str) -> int:
    """Return one scalar count from a database-templated statement."""

    return int(clickhouse_client.query(statement.format(database=database)).result_rows[0][0])


def direct_fingerprinted_relation_names(
    *, clickhouse_client: Client, database: str
) -> tuple[str, ...]:
    """Return logical models recorded by successful direct fingerprints."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT DISTINCT logical_model_identity FROM {database}._sqltide_direct_fingerprints "
        "ORDER BY logical_model_identity"
    ).result_rows
    return tuple(str(row[0]).partition(".")[2] for row in rows)


def direct_replay_artifact_ranges(*, project_root: Path) -> tuple[tuple[str, str, str], ...]:
    """Return replay intervals from the latest exact direct execution artifact."""

    payload: dict[str, object] = json.loads(
        (project_root / "target/run/build/execution.json").read_text(encoding="utf-8")
    )
    captures: list[dict[str, object]] = cast(list[dict[str, object]], payload["captured_roots"])
    ranges: list[tuple[str, str, str]] = []
    capture: dict[str, object]
    for capture in captures:
        capture_ranges: list[dict[str, object]] = cast(list[dict[str, object]], capture["ranges"])
        replay_range: dict[str, object]
        for replay_range in capture_ranges:
            partition: object = replay_range["partition"]
            boundary_key: str = {
                True: f"_replay_partition={partition}",
                False: f"_replay_{capture['boundary_mode']}",
            }[partition is not None]
            ranges.append(
                (
                    boundary_key,
                    str(replay_range["lower"]),
                    str(replay_range["upper"]),
                )
            )
    return tuple(sorted(ranges))


def direct_graph_order_ids(
    *, clickhouse_client: Client, database: str, model_name: str
) -> tuple[str, ...]:
    """Return ordered identities from one selected-graph model target."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {database}.tbl__{model_name} ORDER BY order_id"
    ).result_rows
    return tuple(str(row[0]) for row in rows)


def direct_relation_order_ids(
    *, clickhouse_client: Client, database: str, relation_name: str
) -> tuple[str, ...]:
    """Return ordered identities from one exact direct relation name."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id FROM {database}.{relation_name} ORDER BY order_id"
    ).result_rows
    return tuple(str(row[0]) for row in rows)


def direct_graph_delta_rows(
    *, clickhouse_client: Client, database: str
) -> tuple[tuple[str, str], ...]:
    """Return the fan-in target's identity and side-reference marker rows."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id, gamma_marker FROM {database}.tbl__delta ORDER BY order_id"
    ).result_rows
    return tuple((str(row[0]), str(row[1])) for row in rows)


def stringify_warehouse_rows(*, rows: Sequence[Sequence[object]]) -> tuple[tuple[str, ...], ...]:
    """Convert driver row values to deterministic strings."""

    converted: list[tuple[str, ...]] = []
    row: Sequence[object]
    for row in rows:
        converted.append(tuple(map(str, row)))
    return tuple(converted)


def prepare_virtual_fan_in_source(*, clickhouse_client: Client, database: str) -> None:
    """Create and seed the adopted source for one virtual fan-in execution form."""

    clickhouse_client.command(
        f"CREATE TABLE {database}.fan_in_orders_input "
        "(order_id String, event_partition Int32, event_offset Int64, "
        "event_timestamp DateTime64(3)) ENGINE = MergeTree ORDER BY order_id"
    )
    clickhouse_client.command(
        f"INSERT INTO {database}.fan_in_orders_input VALUES "
        "('order-1', 0, 1, '2026-08-01 00:00:01.000'), "
        "('order-2', 0, 2, '2026-08-01 00:00:02.000')"
    )


def virtual_fan_in_delta_rows(
    *, clickhouse_client: Client, database: str, deployment_id: str
) -> tuple[tuple[str, str], ...]:
    """Return deterministic fan-in candidate rows."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        f"SELECT order_id, gamma_marker FROM {database}.tbl__delta__{deployment_id} "
        "ORDER BY order_id"
    ).result_rows
    return tuple((str(row[0]), str(row[1])) for row in rows)


def virtual_deployment_watermark_rows(
    *, clickhouse_client: Client, database: str, deployment_id: str
) -> tuple[tuple[str, str, str], ...]:
    """Return replay-root watermarks without the shared boundary-time row."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT root_object_name, if(boundary_kind = 'offsets', "
        "concat('_replay_partition=', partition_value), concat('_replay_', boundary_kind)), "
        "cutoff_value "
        f"FROM {database}._sqltide_virtual_replay_boundaries "
        f"WHERE deployment_id = '{deployment_id}' "
        "ORDER BY root_object_name, boundary_kind, partition_value"
    ).result_rows
    return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)


def confirm_with_conflicting_candidate(
    prompt: str, *, clickhouse_client: Client, database: str, relation_name: str
) -> str:
    """Create a post-preview candidate conflict while accepting confirmation."""

    del prompt
    clickhouse_client.command(
        f"CREATE TABLE {database}.{relation_name} "
        "(order_id String) ENGINE = MergeTree ORDER BY order_id"
    )
    return "y"


def virtual_deployment_metadata_row_count(
    *, clickhouse_client: Client, database: str, deployment_id: str
) -> int:
    """Count all lifecycle metadata rows written for one virtual deployment."""

    rows: Sequence[Sequence[object]] = clickhouse_client.query(
        "SELECT sum(row_count) FROM ("
        f"SELECT count() AS row_count FROM {database}._sqltide_virtual_deployments "
        f"WHERE deployment_id = '{deployment_id}' UNION ALL "
        f"SELECT count() AS row_count FROM {database}._sqltide_virtual_object_state "
        f"WHERE deployment_id = '{deployment_id}' UNION ALL "
        f"SELECT count() AS row_count FROM {database}._sqltide_virtual_replay_boundaries "
        f"WHERE deployment_id = '{deployment_id}' UNION ALL "
        f"SELECT count() AS row_count FROM {database}._sqltide_virtual_publications "
        f"WHERE deployment_id = '{deployment_id}')"
    ).result_rows
    return int(str(rows[0][0]))
