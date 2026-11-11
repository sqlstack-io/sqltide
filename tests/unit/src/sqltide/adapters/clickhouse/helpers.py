from collections.abc import Iterator, Mapping, Sequence
from typing import cast

from clickhouse_connect.driver.exceptions import OperationalError

from sqltide._native import sql_canonicalize
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterManifest,
    AdapterManifestResource,
    AdapterMutationResult,
    AdapterQueryResult,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.adapters.clickhouse.classes.clickhouse_connection import ClickHouseConnection
from sqltide.adapters.clickhouse.types import RawClickHouseClient
from sqltide.compiler.compile.models import (
    Column,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    KafkaSettings,
    KafkaTableSpec,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
)
from sqltide.compiler.planner.models import (
    DeploymentRecord,
    DeploymentWatermarkRecord,
    MetadataState,
    ObjectStateRecord,
    PreparedObjectMapping,
    PublishEventRecord,
)


def build_test_manifest() -> AdapterManifest:
    return AdapterManifest(
        manifest_id="manifest-'1",
        invocation_id="invocation-1",
        project_identity="/projects\\orders",
        target_name="uat",
        target_database="analytics",
        is_production=False,
        project_revision=None,
        manifest_fingerprint="fingerprint-1",
        manifest_version=1,
        pipelines=("orders", "quotes'live"),
        resources=(
            AdapterManifestResource(
                pipeline_name="orders",
                logical_type="model",
                logical_name="orders",
                resource_role="model_table",
                resource_database="analytics",
                resource_name="tbl__orders",
                resource_kind="table",
            ),
        ),
        tool_version="0.37.0",
        published_at="2026-08-28 12:00:00.123456",
    )


class FakeRawClickHouseQueryResult:
    """A raw driver result shape used to exercise adapter normalization."""

    def __init__(self, *, column_names: list[str], result_rows: list[list[object]]) -> None:
        self.column_names: Sequence[str] = column_names
        self.result_rows: Sequence[Sequence[object]] = result_rows


class StubRawClickHouseClient:
    """A raw driver client returning one prepared result for every query."""

    def __init__(self, result: FakeRawClickHouseQueryResult) -> None:
        self._result: FakeRawClickHouseQueryResult = result
        self.closed: bool = False
        self.query_settings: list[Mapping[str, str] | None] = []
        self.queries: list[str] = []

    def command(self, *, cmd: str, settings: Mapping[str, str] | None = None) -> None:
        del cmd
        self.query_settings.append(settings)

    def query(
        self, *, query: str, settings: Mapping[str, str] | None = None
    ) -> FakeRawClickHouseQueryResult:
        self.queries.append(query)
        self.query_settings.append(settings)
        return self._result

    def close(self) -> None:
        self.closed = True


class RecordingTargetMutationLockConnection(ClickHouseConnection):
    def __init__(self, *, owner_rows: tuple[tuple[object, ...], ...] = ()) -> None:
        raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
            FakeRawClickHouseQueryResult(column_names=[], result_rows=[])
        )
        super().__init__(cast(RawClickHouseClient, raw_client))
        self._owner_rows: tuple[tuple[object, ...], ...] = owner_rows
        self.statements: list[str] = []

    def _execute_workflow_sql(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        del query_id
        self.statements.append(statement)
        return AdapterMutationResult()

    def query(self, statement: str) -> AdapterQueryResult:
        self.statements.append(statement)
        return AdapterQueryResult(rows=self._owner_rows, column_names=("comment",))


class ConflictingTargetMutationLockConnection(RecordingTargetMutationLockConnection):
    def _execute_workflow_sql(
        self, *, statement: str, query_id: str | None
    ) -> AdapterMutationResult:
        del query_id
        self.statements.append(statement)
        raise AdapterWarehouseError("table already exists")


class SequencedRawClickHouseClient:
    """A raw client returning a prepared result sequence while recording queries."""

    def __init__(self, results: tuple[FakeRawClickHouseQueryResult, ...]) -> None:
        self._results: Iterator[FakeRawClickHouseQueryResult] = iter(results)
        self.statements: list[str] = []

    def command(self, *, cmd: str) -> None:
        del cmd

    def query(self, *, query: str) -> FakeRawClickHouseQueryResult:
        self.statements.append(query)
        return next(self._results)

    def close(self) -> None:
        return None


class GuardedRenderingClickHouseConnection(ClickHouseConnection):
    """A ClickHouse renderer with prepared read state for cleanup guards."""

    def __init__(
        self, *, catalog: CatalogSnapshot, managed_table_state: InspectedManagedTableState
    ) -> None:
        raw_client: StubRawClickHouseClient = StubRawClickHouseClient(
            FakeRawClickHouseQueryResult(column_names=[], result_rows=[])
        )
        super().__init__(cast(RawClickHouseClient, raw_client))
        self._catalog: CatalogSnapshot = catalog
        self._managed_table_state: InspectedManagedTableState = managed_table_state
        self.inspection_count: int = 0

    def load_catalog(self, database: str) -> CatalogSnapshot:
        del database
        return self._catalog

    def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
        del database
        self.inspection_count += 1
        return self._managed_table_state


class FailingRawClickHouseClient:
    """A raw driver client that always raises one prepared driver error."""

    def __init__(self, error: Exception) -> None:
        self._error: Exception = error

    def command(self, *, cmd: str) -> None:
        del cmd
        raise self._error

    def query(self, *, query: str) -> FakeRawClickHouseQueryResult:
        del query
        raise self._error

    def close(self) -> None:
        raise self._error


def build_catalog_relation_result(*, create_table_query: str) -> FakeRawClickHouseQueryResult:
    return FakeRawClickHouseQueryResult(
        column_names=[
            "name",
            "engine",
            "sorting_key",
            "partition_key",
            "create_table_query",
            "as_select",
            "uuid",
        ],
        result_rows=[
            [
                "tbl__orders",
                "MergeTree",
                "order_id",
                "",
                create_table_query,
                "",
                "00000000-0000-0000-0000-000000000001",
            ]
        ],
    )


def build_catalog_timezone_result() -> FakeRawClickHouseQueryResult:
    return FakeRawClickHouseQueryResult(column_names=["timezone()"], result_rows=[["UTC"]])


def build_catalog_column_result() -> FakeRawClickHouseQueryResult:
    return FakeRawClickHouseQueryResult(
        column_names=["table", "name", "type", "default_expression"],
        result_rows=[["tbl__orders", "order_id", "UInt64", ""]],
    )


def build_kafka_table(extra_settings: dict[str, str] | None = None) -> DesiredKafkaTable:
    return DesiredKafkaTable(
        key=ObjectKey(database=None, object_type="kafka_table", name="kafka__orders"),
        deps=(),
        spec=KafkaTableSpec(
            columns=(Column(name="message", type="String"),),
            kafka=KafkaSettings(
                broker_list="kafka:9092",
                topic="source.orders.created",
                consumer_group="sqltide_orders_orders",
                format="JSONAsString",
                settings=extra_settings,
            ),
        ),
    )


def build_table(
    *, partition_by: str | None, ttl: str | None, settings: dict[str, str] | None
) -> DesiredTable:
    return DesiredTable(
        key=ObjectKey(database=None, object_type="table", name="tbl__orders_enriched"),
        deps=(),
        spec=TableSpec(
            columns=(
                Column(name="order_id", type="String"),
                Column(name="_replay_landed_at", type="DateTime64(3)", default="now64(3)"),
            ),
            storage=TableStorage(
                engine="ReplacingMergeTree(_replay_landed_at)",
                order_by=("order_id", "_replay_landed_at"),
                partition_by=partition_by,
                ttl=ttl,
                settings=settings,
            ),
        ),
    )


def build_materialized_view(*, query: str, database_template: str) -> DesiredMaterializedView:
    return DesiredMaterializedView(
        key=ObjectKey(
            database=None,
            object_type="materialized_view",
            name="mv__orders_enriched",
        ),
        deps=(),
        spec=MaterializedViewSpec(
            source_table_name="raw__orders",
            target_table_name="tbl__orders_enriched",
            query=query,
            database_template=database_template,
        ),
    )


def build_metadata_state() -> MetadataState:
    root_key: ObjectKey = ObjectKey(database=None, object_type="table", name="raw__orders")
    transform_key: ObjectKey = ObjectKey(
        database=None,
        object_type="table",
        name="tbl__orders_enriched",
    )
    return MetadataState(
        object_states=(
            ObjectStateRecord(
                deployment_id="20260408T130000Z_ab12cd",
                key=transform_key,
                normalized_fingerprint="fingerprint_transform",
                normalized_query="SELECT * FROM raw__orders",
                recorded_at="2026-04-08T13:00:00Z",
            ),
        ),
        deployments=(
            DeploymentRecord(
                deployment_id="20260408T130000Z_ab12cd",
                created_at="2026-04-08T13:00:00Z",
                status="backfilling",
                replay_lineage_mode="offsets",
                selected_root_keys=(root_key,),
                warning_codes=("mutable_ref_replay_not_guaranteed",),
                prepared_object_mappings=(
                    PreparedObjectMapping(
                        logical_key=transform_key,
                        physical_name="tbl__orders_enriched__20260408T130000Z_ab12cd",
                        logical_model_name="orders_enriched",
                    ),
                ),
                workflow_fingerprint="workflow-fingerprint",
                boundary_time="2026-04-08T13:00:05Z",
                tool_version="1.2.3",
            ),
        ),
        deployment_watermarks=(
            DeploymentWatermarkRecord(
                deployment_id="20260408T130000Z_ab12cd",
                root_key=transform_key,
                anchor_key=root_key,
                boundary_key="_replay_partition=0",
                cutoff_value="12345",
            ),
        ),
        publish_events=(
            PublishEventRecord(
                deployment_id="20260408T130000Z_ab12cd",
                published_at="2026-04-08T13:30:00Z",
                logical_view_names=("tbl__orders_enriched",),
                database="analytics",
                physical_relation_names=("tbl__orders_enriched__20260408T130000Z_ab12cd",),
            ),
        ),
    )


def normalize_clickhouse_sql(sql: str) -> str:
    return sql_canonicalize(sql, "clickhouse")


def connection_failure(
    message: str, *, cause: BaseException, link: str, suppress_context: bool = False
) -> OperationalError:
    """A driver error whose `link` (`__cause__` or `__context__`) leads to `cause` one level down,
    as the HTTP client chains them."""
    transport_error: OSError = OSError("Failed to establish a new connection")
    transport_error.__cause__ = cause
    driver_error: OperationalError = OperationalError(message)
    setattr(driver_error, link, transport_error)
    driver_error.__suppress_context__ = suppress_context
    return driver_error
