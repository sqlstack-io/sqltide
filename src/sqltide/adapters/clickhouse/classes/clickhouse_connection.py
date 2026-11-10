"""ClickHouse-backed implementation of the neutral connection contract."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from hashlib import sha256

from clickhouse_connect.driver.exceptions import ClickHouseError, StreamFailureError
from clickhouse_connect.driver.summary import QuerySummary

from sqltide._native import (
    build_inspected_managed_table_state,
    clickhouse_catalog_resource_matches,
    load_clickhouse_catalog,
    load_clickhouse_deployment_inventory,
    load_clickhouse_direct_fingerprints,
    load_clickhouse_external_dependants,
    load_clickhouse_manifests,
    load_clickhouse_refresh_states,
    render_clickhouse_direct_fingerprint_observations,
    render_clickhouse_latest_node_status_query,
    render_clickhouse_manifest_publication,
    render_clickhouse_metadata_migration_workflow,
    render_clickhouse_metadata_state,
    render_clickhouse_relation_cleanup,
    render_clickhouse_run_event_inserts,
    render_clickhouse_run_statement_inserts,
    render_clickhouse_sensor_retention_cleanup,
    render_clickhouse_sensor_state_inserts,
    render_clickhouse_stable_binding_replacement,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import (
    METADATA_DEAD_LETTERS_TABLE_NAME,
    METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME,
    METADATA_DEPLOYMENTS_TABLE_NAME,
    METADATA_DIRECT_FINGERPRINTS_TABLE_NAME,
    METADATA_INVOCATIONS_TABLE_NAME,
    METADATA_MANIFESTS_TABLE_NAME,
    METADATA_NODE_RESULTS_TABLE_NAME,
    METADATA_OBJECT_STATE_TABLE_NAME,
    METADATA_PUBLISH_HISTORY_TABLE_NAME,
    METADATA_RUN_EVENTS_TABLE_NAME,
    METADATA_RUN_STATEMENTS_TABLE_NAME,
    METADATA_SCHEMA_VERSIONS_TABLE_NAME,
    METADATA_SENSOR_CHECKPOINTS_TABLE_NAME,
    METADATA_SENSOR_LEASES_TABLE_NAME,
    METADATA_SENSOR_OVERRIDES_TABLE_NAME,
    METADATA_SENSOR_STEPS_TABLE_NAME,
    METADATA_SENSOR_TICKS_TABLE_NAME,
    TARGET_MUTATION_LOCK_TABLE_NAME,
)
from sqltide.adapter.exceptions import (
    AdapterError,
    AdapterResultError,
    AdapterTargetMutationLockError,
    AdapterWarehouseError,
)
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterCapabilities,
    AdapterCapturedReplayRequest,
    AdapterCurrentQualityNode,
    AdapterDeploymentInventory,
    AdapterDeploymentReplayRequest,
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterIdentity,
    AdapterInvocationRecord,
    AdapterManagedSource,
    AdapterManifest,
    AdapterManifestSnapshot,
    AdapterMaterializedView,
    AdapterMetadataState,
    AdapterMutationResult,
    AdapterNodeResultRecord,
    AdapterQueryCancellation,
    AdapterQueryResult,
    AdapterReadinessRequest,
    AdapterReadinessRootObservation,
    AdapterRefreshState,
    AdapterRelationCleanupRequest,
    AdapterReplayCoverageRequest,
    AdapterReplayOffsetFrontier,
    AdapterReplayOffsetProgressRequest,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
    AdapterSensorState,
    AdapterStableView,
    AdapterStatementProgress,
    AdapterTable,
    AdapterTargetMutationLock,
    AdapterView,
    AdapterWarehouseHealth,
    CatalogRelation,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse._helpers.errors import translate_driver_error
from sqltide.adapters.clickhouse._helpers.readiness import compare_clickhouse_readiness
from sqltide.adapters.clickhouse._helpers.rendering import (
    render_clickhouse_ensure_database,
    render_clickhouse_resource,
)
from sqltide.adapters.clickhouse._helpers.replay import (
    render_clickhouse_replay_coverage_query,
    render_clickhouse_replay_from_capture,
    render_clickhouse_replay_from_deployment,
)
from sqltide.adapters.clickhouse.classes.warehouse_health_reader import (
    ClickHouseWarehouseHealthReader,
)
from sqltide.adapters.clickhouse.constants import (
    CLICKHOUSE_ADAPTER_NAME,
    CLICKHOUSE_DIRECT_REBUILD_SUPPORTED,
    CLICKHOUSE_GRAPH_ATOMIC_PUBLISH,
    CLICKHOUSE_HISTORY_PREFIX_SEED_SUPPORTED,
    CLICKHOUSE_MANAGED_SOURCE_KINDS,
    CLICKHOUSE_PER_RELATION_ATOMIC_REPLACE,
    CLICKHOUSE_REPLAY_BOUNDARY_MODES,
    CLICKHOUSE_SET_DIFFERENCE_COMPARISON_SUPPORTED,
    CLICKHOUSE_STABLE_LOGICAL_BINDINGS_SUPPORTED,
    CLICKHOUSE_VIRTUAL_ENVIRONMENTS_SUPPORTED,
    CLICKHOUSE_WRITTEN_ROWS_SUMMARY_KEY,
)
from sqltide.adapters.clickhouse.types import (
    RawClickHouseClient,
    RawClickHouseQueryResult,
)

_NODE_RESULT_REQUIRED_COLUMNS: frozenset[str] = frozenset(
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
_QUERY_ID_SETTING: str = "query_id"
_TARGET_MUTATION_LOCK_COMMENT_PREFIX: str = "sqltide-target-lock-v2:"
_METADATA_TABLE_NAME_PREFIX: str = "_sqltide_"
_STREAMBUILD_METADATA_TABLE_NAME_PREFIX: str = "_streambuild_"
_METADATA_TABLE_NAMES: tuple[str, ...] = (
    METADATA_SCHEMA_VERSIONS_TABLE_NAME,
    METADATA_OBJECT_STATE_TABLE_NAME,
    METADATA_DEPLOYMENTS_TABLE_NAME,
    METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME,
    METADATA_PUBLISH_HISTORY_TABLE_NAME,
    METADATA_DIRECT_FINGERPRINTS_TABLE_NAME,
    METADATA_DEAD_LETTERS_TABLE_NAME,
    METADATA_MANIFESTS_TABLE_NAME,
    METADATA_INVOCATIONS_TABLE_NAME,
    METADATA_NODE_RESULTS_TABLE_NAME,
    METADATA_RUN_EVENTS_TABLE_NAME,
    METADATA_RUN_STATEMENTS_TABLE_NAME,
    METADATA_SENSOR_CHECKPOINTS_TABLE_NAME,
    METADATA_SENSOR_TICKS_TABLE_NAME,
    METADATA_SENSOR_STEPS_TABLE_NAME,
    METADATA_SENSOR_OVERRIDES_TABLE_NAME,
    METADATA_SENSOR_LEASES_TABLE_NAME,
)
_MAX_TABLE_SIZE_TO_DROP_SETTING: str = "max_table_size_to_drop"
_REPLAY_FRONTIER_TTL_SECONDS: float = 5.0
_REPLAY_FRONTIER_RETENTION_SECONDS: float = 60.0
_MAX_REPLAY_FRONTIER_OBSERVATIONS: int = 128
_QUERY_CANCELLATION_CONFIRM_SECONDS: float = 5.0
_QUERY_CANCELLATION_POLL_SECONDS: float = 0.05


class ClickHouseConnection(AdapterConnection):
    """A neutral adapter connection backed by the ClickHouse driver."""

    def __init__(self, raw_client: RawClickHouseClient) -> None:
        self._raw_client: RawClickHouseClient = raw_client
        self._replay_frontier_lock: threading.Lock = threading.Lock()
        self._replay_frontier_observations: dict[
            tuple[str, AdapterReplayOffsetProgressRequest],
            tuple[float, tuple[AdapterReplayOffsetFrontier, ...]],
        ] = {}

    @property
    def adapter_identity(self) -> AdapterIdentity:
        """Return the built-in ClickHouse adapter identity."""

        return AdapterIdentity(name=CLICKHOUSE_ADAPTER_NAME)

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Return capabilities implemented by the ClickHouse adapter."""

        return AdapterCapabilities(
            virtual_environments=CLICKHOUSE_VIRTUAL_ENVIRONMENTS_SUPPORTED,
            managed_source_kinds=CLICKHOUSE_MANAGED_SOURCE_KINDS,
            replay_boundary_modes=CLICKHOUSE_REPLAY_BOUNDARY_MODES,
            history_prefix_seed=CLICKHOUSE_HISTORY_PREFIX_SEED_SUPPORTED,
            stable_logical_bindings=CLICKHOUSE_STABLE_LOGICAL_BINDINGS_SUPPORTED,
            per_relation_atomic_replace=CLICKHOUSE_PER_RELATION_ATOMIC_REPLACE,
            graph_atomic_publish=CLICKHOUSE_GRAPH_ATOMIC_PUBLISH,
            set_difference_comparison=CLICKHOUSE_SET_DIFFERENCE_COMPARISON_SUPPORTED,
            direct_rebuild=CLICKHOUSE_DIRECT_REBUILD_SUPPORTED,
        )

    def load_catalog(self, database: str) -> CatalogSnapshot:
        """Load a neutral catalog snapshot from ClickHouse system tables."""

        return load_clickhouse_catalog(
            connection=self,
            adapter_identity=self.adapter_identity,
            database=database,
        )

    def load_relation_drop_size_limit(self) -> int | None:
        """Return ClickHouse's server/session table DROP safety limit."""

        return self._load_relation_drop_size_setting(column="value")

    def load_relation_drop_size_server_default(self) -> int | None:
        """Return ClickHouse's configured default table DROP safety limit."""

        return self._load_relation_drop_size_setting(column="default")

    def _load_relation_drop_size_setting(self, *, column: str) -> int | None:
        result: AdapterQueryResult = self.query(
            f"SELECT {column} FROM system.settings WHERE name = '{_MAX_TABLE_SIZE_TO_DROP_SETTING}'"
        )
        if len(result.rows) != 1:
            raise AdapterResultError(
                f"Could not determine ClickHouse {_MAX_TABLE_SIZE_TO_DROP_SETTING}"
            )
        value: int = int(str(result.rows[0][0]))
        return None if value == 0 else value

    def load_external_dependants(
        self, *, database: str, relation_names: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return database-qualified unmanaged dependants across ClickHouse."""

        return load_clickhouse_external_dependants(
            connection=self,
            database=database,
            relation_names=relation_names,
        )

    def load_refresh_states(self, database: str) -> tuple[AdapterRefreshState, ...]:
        """Report ClickHouse refreshable view state so stale relations stay visible."""

        return load_clickhouse_refresh_states(connection=self, database=database)

    def replay_anchor_has_repeated_offsets(
        self, *, database: str, relation: str, partition_column: str, offset_column: str
    ) -> bool:
        """Return whether any partition holds more rows than its offset span."""

        partition: str = _quote_clickhouse_identifier(partition_column)
        offset: str = _quote_clickhouse_identifier(offset_column)
        return bool(
            self.query(
                "SELECT 1 FROM (SELECT count() AS row_count, "
                f"max({offset}) - min({offset}) + 1 AS offset_span "
                f"FROM {_quote_clickhouse_identifier(database)}."
                f"{_quote_clickhouse_identifier(relation)} GROUP BY {partition}) "
                "WHERE row_count > offset_span LIMIT 1"
            ).rows
        )

    def load_warehouse_health(
        self,
        *,
        database: str,
        managed_source_names: tuple[str, ...] = (),
        include_dead_letters: bool = False,
    ) -> AdapterWarehouseHealth:
        """Read bounded ClickHouse system-table diagnostics."""

        return ClickHouseWarehouseHealthReader(connection=self).read(
            database=database,
            managed_source_names=managed_source_names,
            include_dead_letters=include_dead_letters,
        )

    def metadata_columns(self, *, database: str, table: str) -> frozenset[str]:
        """Return available ClickHouse columns for one framework metadata table."""

        result: AdapterQueryResult = self.query(
            f"SELECT name FROM system.columns WHERE database = '{database}' AND table = '{table}'"
        )
        return frozenset(str(row[0]) for row in result.rows)

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        """Inspect ClickHouse stable bindings and deployment-specific tables."""

        return build_inspected_managed_table_state(client=self, database=database)

    def query(self, statement: str) -> AdapterQueryResult:
        """Execute a ClickHouse query and normalize the returned rows."""

        return self._query(statement=statement, query_id=None)

    def execute_workflow_query(self, *, statement: str, query_id: str | None) -> AdapterQueryResult:
        """Execute a workflow query with a ClickHouse query ID."""

        return self._query(statement=statement, query_id=query_id)

    def _query(self, *, statement: str, query_id: str | None) -> AdapterQueryResult:
        settings: dict[str, str] | None = (
            None if query_id is None else {_QUERY_ID_SETTING: query_id}
        )

        try:
            raw_result: RawClickHouseQueryResult = (
                self._raw_client.query(query=statement)
                if settings is None
                else self._raw_client.query(query=statement, settings=settings)
            )
        except (ClickHouseError, StreamFailureError) as error:
            raise translate_driver_error(error) from error
        result_rows: Sequence[Sequence[object]] = raw_result.result_rows
        return AdapterQueryResult(
            column_names=tuple(raw_result.column_names),
            rows=tuple(tuple(row) for row in result_rows),
        )

    def execute_workflow_sql(self, statement: str) -> AdapterMutationResult:
        """Execute exact workflow SQL and preserve ClickHouse mutation evidence."""

        return self._execute_workflow_sql(statement=statement, query_id=None)

    def execute_workflow_mutation(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        """Execute workflow SQL with a ClickHouse query ID."""

        return self._execute_workflow_sql(statement=statement, query_id=query_id)

    def _execute_workflow_sql(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        settings: dict[str, str] | None = (
            None if query_id is None else {_QUERY_ID_SETTING: query_id}
        )

        try:
            summary: object = (
                self._raw_client.command(cmd=statement)
                if settings is None
                else self._raw_client.command(cmd=statement, settings=settings)
            )
        except (ClickHouseError, StreamFailureError) as error:
            raise translate_driver_error(error) from error
        if (
            not isinstance(summary, QuerySummary)
            or CLICKHOUSE_WRITTEN_ROWS_SUMMARY_KEY not in summary.summary
        ):
            return AdapterMutationResult()
        return AdapterMutationResult(written_rows=summary.written_rows)

    def load_statement_progress(self, *, query_id: str) -> AdapterStatementProgress | None:
        """Read one active query snapshot from ClickHouse system.processes."""

        query_id_literal: str = query_id.replace("\\", "\\\\").replace("'", "\\'")
        result: AdapterQueryResult = self.query(
            "SELECT elapsed, read_rows, read_bytes, total_rows_approx, memory_usage, "
            "Settings AS settings FROM system.processes "
            f"WHERE query_id = '{query_id_literal}' LIMIT 1"
        )
        rows: tuple[Mapping[str, object], ...] = result.named_rows()
        if not rows:
            return None
        row: Mapping[str, object] = rows[0]
        raw_settings: object = row["settings"]
        settings: tuple[tuple[str, str], ...] = (
            tuple(sorted((str(name), str(value)) for name, value in raw_settings.items()))
            if isinstance(raw_settings, Mapping)
            else ()
        )
        return AdapterStatementProgress(
            elapsed_seconds=float(str(row["elapsed"])),
            read_rows=int(str(row["read_rows"])),
            read_bytes=int(str(row["read_bytes"])),
            total_rows_approx=int(str(row["total_rows_approx"])),
            memory_usage_bytes=int(str(row["memory_usage"])),
            settings=settings,
        )

    def cancel_workflow_query(self, *, query_id: str) -> AdapterQueryCancellation:
        """Kill one exact ClickHouse query and confirm it leaves system.processes."""

        result: AdapterQueryResult = self._query(
            statement=f"KILL QUERY WHERE query_id = {_quote_clickhouse_string(query_id)} SYNC",
            query_id=None,
        )
        query_found: bool = bool(result.rows)
        deadline: float = time.monotonic() + _QUERY_CANCELLATION_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if self.load_statement_progress(query_id=query_id) is None:
                return AdapterQueryCancellation(
                    query_id=query_id,
                    supported=True,
                    query_found=query_found,
                    termination_confirmed=True,
                )
            time.sleep(_QUERY_CANCELLATION_POLL_SECONDS)
        return AdapterQueryCancellation(
            query_id=query_id,
            supported=True,
            query_found=query_found,
            termination_confirmed=False,
            detail="ClickHouse query remained active after KILL QUERY SYNC",
        )

    def load_replay_offset_frontiers(
        self, *, query_id: str, request: AdapterReplayOffsetProgressRequest
    ) -> tuple[AdapterReplayOffsetFrontier, ...]:
        """Observe the greatest committed target offset for each captured partition."""

        key: tuple[str, AdapterReplayOffsetProgressRequest] = (query_id, request)
        now: float = time.monotonic()
        with self._replay_frontier_lock:
            cached: tuple[float, tuple[AdapterReplayOffsetFrontier, ...]] | None = (
                self._replay_frontier_observations.get(key)
            )
            if cached is not None and now - cached[0] < _REPLAY_FRONTIER_TTL_SECONDS:
                return cached[1]
            observed: tuple[AdapterReplayOffsetFrontier, ...] = self._read_replay_offset_frontiers(
                request=request
            )
            frontiers: tuple[AdapterReplayOffsetFrontier, ...] = _merge_replay_offset_frontiers(
                previous=() if cached is None else cached[1], observed=observed
            )
            retained: dict[
                tuple[str, AdapterReplayOffsetProgressRequest],
                tuple[float, tuple[AdapterReplayOffsetFrontier, ...]],
            ] = {
                observation_key: observation
                for observation_key, observation in self._replay_frontier_observations.items()
                if now - observation[0] <= _REPLAY_FRONTIER_RETENTION_SECONDS
            }
            retained[key] = (now, frontiers)
            self._replay_frontier_observations = dict(
                sorted(retained.items(), key=lambda item: item[1][0])[
                    -_MAX_REPLAY_FRONTIER_OBSERVATIONS:
                ]
            )
            return frontiers

    def _read_replay_offset_frontiers(
        self, *, request: AdapterReplayOffsetProgressRequest
    ) -> tuple[AdapterReplayOffsetFrontier, ...]:
        partitions: tuple[int, ...] = tuple(sorted({item.partition for item in request.ranges}))
        if not partitions:
            return ()
        database: str = _quote_clickhouse_identifier(request.database)
        relation: str = _quote_clickhouse_identifier(request.relation)
        partition_column: str = _quote_clickhouse_identifier(request.partition_column)
        offset_column: str = _quote_clickhouse_identifier(request.offset_column)
        rendered_partitions: str = ", ".join(str(partition) for partition in partitions)
        rows: tuple[Mapping[str, object], ...] = self.query(
            f"SELECT toInt64({partition_column}) AS partition, "
            f"toInt64(max({offset_column})) AS completed_offset "
            f"FROM {database}.{relation} "
            f"WHERE {partition_column} IN ({rendered_partitions}) "
            "GROUP BY partition ORDER BY partition"
        ).named_rows()
        return tuple(
            AdapterReplayOffsetFrontier(
                partition=int(str(row["partition"])),
                completed_offset=int(str(row["completed_offset"])),
            )
            for row in rows
        )

    def capture_warehouse_timestamp(self) -> str:
        """Capture ClickHouse server time as an exact UTC DateTime64(3) string."""

        result: AdapterQueryResult = self.query(
            "SELECT toString(now64(3, 'UTC'), 'UTC') AS warehouse_timestamp"
        )
        if not result.rows:
            raise AdapterResultError("ClickHouse returned no warehouse timestamp")
        return str(result.rows[0][0])

    def acquire_target_mutation_lock(
        self, *, database: str, owner_id: str
    ) -> AdapterTargetMutationLock:
        """Acquire one target-wide lock using ClickHouse's atomic DDL namespace."""

        table: str = _qualified_lock_table(database)
        owner_comment: str = f"{_TARGET_MUTATION_LOCK_COMMENT_PREFIX}{owner_id}"
        try:
            self._execute_workflow_sql(
                statement=(
                    f"CREATE TABLE {table} (guard UInt8) ENGINE = TinyLog "
                    f"COMMENT {_quote_clickhouse_string(owner_comment)};"
                ),
                query_id=None,
            )
        except AdapterError as error:
            exists: bool
            current_owner: str | None
            try:
                exists, current_owner = self._target_mutation_lock_state(database=database)
            except AdapterError:
                raise error from None
            if not exists:
                raise
            owner_suffix: str = "" if current_owner is None else f" by '{current_owner}'"
            raise AdapterTargetMutationLockError(
                f"Target database '{database}' is already mutation-locked{owner_suffix}"
            ) from error
        return AdapterTargetMutationLock(database=database, owner_id=owner_id)

    def release_target_mutation_lock(self, lock: AdapterTargetMutationLock) -> None:
        """Atomically exchange and verify the exact lock generation before release."""

        release_name: str = (
            f"{TARGET_MUTATION_LOCK_TABLE_NAME}_release_"
            f"{sha256(lock.owner_id.encode()).hexdigest()[:16]}"
        )
        release_table: str = (
            f"{_quote_clickhouse_identifier(lock.database)}."
            f"{_quote_clickhouse_identifier(release_name)}"
        )
        owner_comment: str = f"{_TARGET_MUTATION_LOCK_COMMENT_PREFIX}{lock.owner_id}"
        self._execute_workflow_sql(
            statement=(
                f"CREATE TABLE {release_table} (guard UInt8) ENGINE = TinyLog "
                f"COMMENT {_quote_clickhouse_string(owner_comment)};"
            ),
            query_id=None,
        )
        lock_table: str = _qualified_lock_table(lock.database)
        self._execute_workflow_sql(
            statement=f"EXCHANGE TABLES {lock_table} AND {release_table};",
            query_id=None,
        )
        marker_exists: bool
        current_owner: str | None
        marker_exists, current_owner = self._target_mutation_lock_record(
            database=lock.database,
            table_name=release_name,
        )
        if not marker_exists or current_owner != lock.owner_id:
            self._execute_workflow_sql(
                statement=f"EXCHANGE TABLES {lock_table} AND {release_table};",
                query_id=None,
            )
            self._execute_workflow_sql(
                statement=f"DROP TABLE {release_table} SYNC;",
                query_id=None,
            )
            raise AdapterTargetMutationLockError(
                f"Target mutation lock ownership changed for database '{lock.database}'"
            )
        self._execute_workflow_sql(
            statement=f"DROP TABLE {release_table} SYNC;",
            query_id=None,
        )
        self._execute_workflow_sql(
            statement=f"DROP TABLE {lock_table} SYNC;",
            query_id=None,
        )

    def _target_mutation_lock_state(self, *, database: str) -> tuple[bool, str | None]:
        return self._target_mutation_lock_record(
            database=database,
            table_name=TARGET_MUTATION_LOCK_TABLE_NAME,
        )

    def _target_mutation_lock_record(
        self, *, database: str, table_name: str
    ) -> tuple[bool, str | None]:
        result: AdapterQueryResult = self.query(
            "SELECT comment FROM system.tables "
            f"WHERE database = {_quote_clickhouse_string(database)} "
            f"AND name = {_quote_clickhouse_string(table_name)} LIMIT 1"
        )
        if not result.rows:
            return False, None
        comment: str = str(result.rows[0][0])
        return (
            True,
            comment.removeprefix(_TARGET_MUTATION_LOCK_COMMENT_PREFIX)
            if comment.startswith(_TARGET_MUTATION_LOCK_COMMENT_PREFIX)
            else None,
        )

    def render_ensure_database(self, database: str) -> str:
        """Render exact ClickHouse database creation SQL."""

        return render_clickhouse_ensure_database(database)

    def database_exists(self, database: str) -> bool:
        """Return whether ClickHouse currently exposes the database namespace."""

        result: AdapterQueryResult = self.query(
            "SELECT count() FROM system.databases "
            f"WHERE name = {_quote_clickhouse_string(database)}"
        )
        return bool(result.rows and int(str(result.rows[0][0])) == 1)

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
        """Render one neutral resource request as ClickHouse SQL."""

        return render_clickhouse_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )

    def rename_streambuild_metadata(self, database: str) -> None:
        """Rename StreamBuild metadata tables, tolerating a concurrent identical rename."""

        renames: dict[str, str] = {
            _streambuild_table_name(name): name for name in _METADATA_TABLE_NAMES
        }
        streambuild_lock: str = _streambuild_table_name(TARGET_MUTATION_LOCK_TABLE_NAME)
        candidates: tuple[str, ...] = (*renames, *renames.values(), streambuild_lock)
        existing: frozenset[str] = self._existing_table_names(database=database, names=candidates)
        pending: tuple[str, ...] = tuple(name for name in renames if name in existing)
        if not pending:
            return
        if streambuild_lock in existing:
            raise AdapterError(
                f"Target database '{database}' is mutation-locked by a StreamBuild run. Wait "
                "for it to finish, or drop the stale "
                f"{database}.{streambuild_lock} table after confirming no run is active, "
                "then retry"
            )
        conflicts: tuple[str, ...] = tuple(
            renames[name] for name in pending if renames[name] in existing
        )
        if conflicts:
            raise AdapterError(
                f"Target database '{database}' contains both StreamBuild and SQLTide metadata "
                f"({', '.join(conflicts)}). A StreamBuild version wrote to this target after "
                "it was renamed; stop using StreamBuild here and drop the outdated "
                f"{_STREAMBUILD_METADATA_TABLE_NAME_PREFIX}* tables, then retry"
            )
        quoted_database: str = _quote_clickhouse_identifier(database)
        rename_clauses: str = ", ".join(
            f"{quoted_database}.{_quote_clickhouse_identifier(name)} TO "
            f"{quoted_database}.{_quote_clickhouse_identifier(renames[name])}"
            for name in pending
        )
        try:
            self._execute_workflow_sql(statement=f"RENAME TABLE {rename_clauses};", query_id=None)
        except AdapterError:
            if self._existing_table_names(database=database, names=pending):
                raise

    def _existing_table_names(self, *, database: str, names: Sequence[str]) -> frozenset[str]:
        result: AdapterQueryResult = self.query(
            "SELECT name FROM system.tables "
            f"WHERE database = {_quote_clickhouse_string(database)} "
            f"AND name IN ({', '.join(_quote_clickhouse_string(name) for name in names)})"
        )
        return frozenset(str(row[0]) for row in result.rows)

    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        """Render the exact idempotent ClickHouse metadata migration."""

        self.validate_metadata_state(database)
        return render_clickhouse_metadata_migration_workflow(database)

    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]:
        """Render exact ClickHouse metadata persistence SQL."""

        self.validate_metadata_state(database)
        return render_clickhouse_metadata_state(database=database, state=state)

    def render_terminal_observations(
        self,
        *,
        database: str,
        invocation: AdapterInvocationRecord,
        node_results: tuple[AdapterNodeResultRecord, ...],
    ) -> tuple[str, ...]:
        """Render terminal UI observations without exposing them to lifecycle readers."""

        self.validate_metadata_state(database)
        state: AdapterMetadataState = AdapterMetadataState(
            object_states=(),
            deployments=(),
            deployment_watermarks=(),
            publish_events=(),
            invocations=(invocation,),
            node_results=node_results,
        )
        return (
            *render_clickhouse_metadata_migration_workflow(database),
            *render_clickhouse_metadata_state(database=database, state=state),
        )

    def render_latest_node_status_query(
        self,
        *,
        database: str,
        project_identity: str,
        target_identity: str,
        nodes: tuple[AdapterCurrentQualityNode, ...],
    ) -> str:
        """Render current, stale, and never-run Quality UI states."""

        self.validate_metadata_state(database)
        return render_clickhouse_latest_node_status_query(
            database=database,
            project_identity=project_identity,
            target_identity=target_identity,
            nodes=nodes,
        )

    def validate_metadata_state(self, database: str) -> None:
        """Reject the intentionally incompatible pre-0.11 quality result schema."""

        columns: frozenset[str] = self.metadata_columns(
            database=database,
            table=METADATA_NODE_RESULTS_TABLE_NAME,
        )
        if not columns or _NODE_RESULT_REQUIRED_COLUMNS <= columns:
            return
        raise AdapterWarehouseError(
            f"{database}.{METADATA_NODE_RESULTS_TABLE_NAME} uses an incompatible pre-0.11 "
            "schema; drop this pre-production metadata table and retry so SQLTide can "
            "recreate it"
        )

    def render_run_events(
        self,
        *,
        database: str,
        events: tuple[AdapterRunEventRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        """Render incremental run-event inserts for the live run timeline."""

        return render_clickhouse_run_event_inserts(
            database=database, events=events, include_migration=include_migration
        )

    def render_run_statements(
        self,
        *,
        database: str,
        statements: tuple[AdapterRunStatementRecord, ...],
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        """Render the durable SQL statement set for one run."""

        return render_clickhouse_run_statement_inserts(
            database=database,
            statements=statements,
            include_migration=include_migration,
        )

    def render_sensor_state(
        self,
        *,
        database: str,
        state: AdapterSensorState,
        include_migration: bool = False,
    ) -> tuple[str, ...]:
        """Render incremental sensor-state inserts for durable automation."""

        return render_clickhouse_sensor_state_inserts(
            database=database, state=state, include_migration=include_migration
        )

    def render_sensor_retention_cleanup(
        self, *, database: str, retention_days: int
    ) -> tuple[str, ...]:
        """Render bounded ClickHouse deletes for aged sensor ticks and steps."""

        return render_clickhouse_sensor_retention_cleanup(
            database=database, retention_days=retention_days
        )

    def load_direct_fingerprints(
        self, *, database: str, logical_model_identities: tuple[str, ...]
    ) -> AdapterDirectFingerprintSnapshot:
        """Load optional logical SQL baselines from ClickHouse metadata."""

        return load_clickhouse_direct_fingerprints(
            connection=self,
            database=database,
            logical_model_identities=logical_model_identities,
        )

    def render_direct_fingerprint_observations(
        self,
        *,
        database: str,
        fingerprints: tuple[AdapterDirectFingerprintRecord, ...],
    ) -> tuple[str, ...]:
        """Render terminal best-effort logical direct SQL baselines."""

        return render_clickhouse_direct_fingerprint_observations(
            database=database,
            fingerprints=fingerprints,
        )

    def load_manifests(
        self,
        *,
        database: str,
        project_identity: str,
        target_name: str,
        target_database: str,
    ) -> AdapterManifestSnapshot:
        """Load complete manifest history for one project target."""

        return load_clickhouse_manifests(
            connection=self,
            database=database,
            project_identity=project_identity,
            target_name=target_name,
            target_database=target_database,
        )

    def render_manifest_publication(
        self, *, database: str, manifest: AdapterManifest
    ) -> tuple[str, ...]:
        """Render one complete manifest append."""

        self.validate_metadata_state(database)
        return render_clickhouse_manifest_publication(database=database, manifest=manifest)

    def catalog_resource_matches(
        self,
        *,
        resource: AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView,
        relation: CatalogRelation,
        database: str,
    ) -> bool:
        """Compare a ClickHouse relation with its compiled desired resource."""

        return clickhouse_catalog_resource_matches(
            resource=resource,
            relation=relation,
            database=database,
        )

    def load_deployment_inventory(self, database: str) -> AdapterDeploymentInventory:
        """Load persisted ClickHouse deployments and publish events."""

        return load_clickhouse_deployment_inventory(connection=self, database=database)

    def render_replay_coverage_query(self, request: AdapterReplayCoverageRequest) -> str:
        """Render retained coverage selected by the replay window."""

        return render_clickhouse_replay_coverage_query(request)

    def render_replay_from_capture(self, request: AdapterCapturedReplayRequest) -> str:
        """Render replay SQL from boundaries captured by this process."""

        return render_clickhouse_replay_from_capture(request)

    def render_replay_from_deployment(
        self, request: AdapterDeploymentReplayRequest
    ) -> tuple[str, ...]:
        """Render fixed-cardinality replay against deployment-stored cutoffs."""

        return render_clickhouse_replay_from_deployment(request)

    def compare_readiness(
        self, request: AdapterReadinessRequest
    ) -> tuple[AdapterReadinessRootObservation, ...]:
        """Compare active and staged ClickHouse relations."""

        return compare_clickhouse_readiness(connection=self, request=request)

    def render_replace_stable_bindings(
        self, request: AdapterBindingReplacementRequest
    ) -> tuple[str, ...]:
        """Render exact ClickHouse stable binding SQL."""

        return render_clickhouse_stable_binding_replacement(connection=self, request=request)

    def render_restore_materialized_view(self, definition_sql: str) -> str:
        """Render the captured view definition so an existing view is left in place."""

        return definition_sql.replace(
            "CREATE MATERIALIZED VIEW ", "CREATE MATERIALIZED VIEW IF NOT EXISTS ", 1
        )

    def render_cleanup_relations(self, request: AdapterRelationCleanupRequest) -> tuple[str, ...]:
        """Render guarded exact ClickHouse relation cleanup SQL."""

        return render_clickhouse_relation_cleanup(connection=self, request=request)

    def close(self) -> None:
        """Close the underlying ClickHouse connection."""

        try:
            self._raw_client.close()
        except (ClickHouseError, StreamFailureError) as error:
            raise translate_driver_error(error) from error


def _streambuild_table_name(name: str) -> str:
    return _STREAMBUILD_METADATA_TABLE_NAME_PREFIX + name.removeprefix(_METADATA_TABLE_NAME_PREFIX)


def _qualified_lock_table(database: str) -> str:
    return f"{_quote_clickhouse_identifier(database)}.`{TARGET_MUTATION_LOCK_TABLE_NAME}`"


def _merge_replay_offset_frontiers(
    *,
    previous: tuple[AdapterReplayOffsetFrontier, ...],
    observed: tuple[AdapterReplayOffsetFrontier, ...],
) -> tuple[AdapterReplayOffsetFrontier, ...]:
    completed_by_partition: dict[int, int] = {
        item.partition: item.completed_offset for item in previous
    }
    for item in observed:
        completed_by_partition[item.partition] = max(
            item.completed_offset,
            completed_by_partition.get(item.partition, item.completed_offset),
        )
    return tuple(
        AdapterReplayOffsetFrontier(
            partition=partition,
            completed_offset=completed_by_partition[partition],
        )
        for partition in sorted(completed_by_partition)
    )


def _quote_clickhouse_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def _quote_clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
