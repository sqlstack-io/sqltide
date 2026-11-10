from dataclasses import replace

from sqltide._native import render_clickhouse_metadata_state
from sqltide.adapter.models import (
    AdapterDirectFingerprintRecord,
    AdapterMetadataState,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.compiler.compile.models import (
    Column,
    DesiredMaterializedView,
    DesiredState,
    DesiredTable,
    MaterializedViewSpec,
    ObjectKey,
    TableSpec,
    TableStorage,
)
from sqltide.compiler.planner.models import (
    ActualMaterializedView,
    ActualState,
    ActualTable,
)
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection


class ReconcileWorkflowAdapterConnection(RecordingAdapterConnection):
    def render_migrate_metadata_state(self, database: str) -> tuple[str, ...]:
        return (f"CREATE DATABASE IF NOT EXISTS {database};",)

    def render_persist_metadata_state(
        self, *, database: str, state: AdapterMetadataState
    ) -> tuple[str, ...]:
        return render_clickhouse_metadata_state(database=database, state=state)


class DirectReconcileWorkflowAdapterConnection(RecordingAdapterConnection):
    def __init__(self) -> None:
        super().__init__()
        self.direct_fingerprint_databases: list[str] = []
        self.direct_fingerprint_records: list[AdapterDirectFingerprintRecord] = []

    def render_direct_fingerprint_observations(
        self,
        *,
        database: str,
        fingerprints: tuple[AdapterDirectFingerprintRecord, ...],
    ) -> tuple[str, ...]:
        self.direct_fingerprint_databases.append(database)
        self.direct_fingerprint_records.extend(fingerprints)
        return ("INSERT_DIRECT_FINGERPRINTS",)


def build_matching_reconcile_states() -> tuple[DesiredState, ActualState]:
    desired_table: DesiredTable = _build_desired_table()
    desired_view: DesiredMaterializedView = _build_desired_view(desired_table.key)
    desired_state: DesiredState = _build_desired_state(desired_table, desired_view)
    actual_state: ActualState = ActualState(
        objects=(
            ActualTable(key=desired_table.key, spec=desired_table.spec),
            ActualMaterializedView(key=desired_view.key, spec=desired_view.spec),
        )
    )
    return desired_state, actual_state


def build_matching_direct_reconcile_state() -> tuple[DesiredState, CatalogSnapshot, ObjectKey]:
    desired_table: DesiredTable = replace(_build_desired_table(), logical_model_name="enriched")
    desired_view: DesiredMaterializedView = _build_desired_view(desired_table.key)
    return (
        _build_desired_state(desired_table, desired_view),
        CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=RecordingAdapterConnection().adapter_identity,
                database="analytics",
            ),
            warehouse_timezone="UTC",
            relations=(
                CatalogRelation(
                    name=desired_table.name,
                    engine="MergeTree",
                    columns=(CatalogColumn(name="order_id", type="String"),),
                    full_engine="MergeTree()",
                    order_by=("order_id",),
                ),
                CatalogRelation(
                    name=desired_view.name,
                    engine="MaterializedView",
                    columns=(CatalogColumn(name="order_id", type="String"),),
                    query_sql=desired_view.query,
                    source_relation_names=(desired_view.source_table_name,),
                    target_relation_name=desired_view.target_table_name,
                ),
            ),
        ),
        desired_table.key,
    )


def build_structurally_mismatched_reconcile_states() -> tuple[DesiredState, ActualState]:
    desired_table: DesiredTable = _build_desired_table()
    desired_view: DesiredMaterializedView = _build_desired_view(desired_table.key)
    desired_state: DesiredState = _build_desired_state(desired_table, desired_view)
    actual_state: ActualState = ActualState(
        objects=(
            ActualTable(
                key=desired_table.key,
                spec=TableSpec(
                    columns=desired_table.columns,
                    storage=TableStorage(
                        engine="ReplacingMergeTree()",
                        order_by=desired_table.order_by,
                    ),
                ),
            ),
        )
    )
    return desired_state, actual_state


def build_misdirected_reconcile_states() -> tuple[DesiredState, ActualState]:
    desired_table: DesiredTable = _build_desired_table()
    desired_view: DesiredMaterializedView = _build_desired_view(desired_table.key)
    desired_state: DesiredState = _build_desired_state(desired_table, desired_view)
    actual_state: ActualState = ActualState(
        objects=(
            ActualTable(key=desired_table.key, spec=desired_table.spec),
            ActualMaterializedView(
                key=desired_view.key,
                spec=MaterializedViewSpec(
                    source_table_name="raw__other",
                    target_table_name="tbl__other",
                    query=desired_view.query,
                ),
            ),
        )
    )
    return desired_state, actual_state


def _build_desired_table() -> DesiredTable:
    target_key: ObjectKey = ObjectKey(
        database=None,
        object_type="table",
        name="tbl__orders",
    )
    return DesiredTable(
        key=target_key,
        deps=(),
        spec=TableSpec(
            columns=(Column(name="order_id", type="String"),),
            storage=TableStorage(
                engine="MergeTree()",
                order_by=("order_id",),
            ),
        ),
    )


def _build_desired_view(target_key: ObjectKey) -> DesiredMaterializedView:
    return DesiredMaterializedView(
        key=ObjectKey(
            database=None,
            object_type="materialized_view",
            name="mv__orders",
        ),
        deps=(target_key,),
        spec=MaterializedViewSpec(
            source_table_name="raw__orders",
            target_table_name=target_key.name,
            query="SELECT order_id FROM raw__orders",
        ),
    )


def _build_desired_state(
    desired_table: DesiredTable,
    desired_view: DesiredMaterializedView,
) -> DesiredState:
    return DesiredState(
        objects=(desired_table, desired_view),
        replay_anchor_keys=frozenset(),
        mutable_ref_warning_keys=frozenset(),
    )
