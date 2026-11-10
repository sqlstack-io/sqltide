import pytest

from sqltide._native import (
    load_direct_warehouse_snapshot,
    load_planning_warehouse_snapshot,
)
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import AdapterQueryResult, CatalogRelation, CatalogSnapshot
from sqltide.compiler.planner.models import (
    DirectWarehouseSnapshot,
    PlanningWarehouseSnapshot,
)
from tests.unit.src.sqltide.compiler.planner._test_types import (
    PlanningSnapshotAssemblyTestCase,
    PlanningSnapshotCapabilityTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    SnapshotRecordingConnection,
    build_snapshot_catalog,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningSnapshotAssemblyTestCase(
            description="captures the catalog and metadata exactly once",
            expected_catalog_load_count=1,
            expected_query_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_warehouse_state_when_loading_snapshot_then_it_reads_each_source_once(
    test_case: PlanningSnapshotAssemblyTestCase,
) -> None:
    catalog: CatalogSnapshot = build_snapshot_catalog()
    connection: SnapshotRecordingConnection = SnapshotRecordingConnection(
        catalog=catalog,
        metadata_result=AdapterQueryResult(
            column_names=(
                "deployment_id",
                "observation_id",
                "state_kind",
                "database_name",
                "object_type",
                "object_name",
                "normalized_fingerprint",
                "normalized_query",
                "recorded_at",
            ),
            rows=(
                (
                    "dep_a",
                    "observation-a",
                    "deployment",
                    None,
                    "table",
                    "tbl__orders",
                    "fingerprint",
                    None,
                    "2026-07-26",
                ),
            ),
        ),
        virtual_environments=True,
    )

    snapshot: PlanningWarehouseSnapshot = load_planning_warehouse_snapshot(
        client=connection,
        database="analytics",
    )
    relation: CatalogRelation | None = snapshot.catalog.relation("tbl__orders")

    assert snapshot.catalog is catalog
    assert relation is not None
    assert relation.columns[0].name == "order_id"
    assert snapshot.object_state_records[0].deployment_id == "dep_a"
    assert snapshot.object_state_records[0].key.name == "tbl__orders"
    assert connection.catalog_load_count == test_case.expected_catalog_load_count
    assert connection.query_count == test_case.expected_query_count


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningSnapshotCapabilityTestCase(
            description="rejects unsupported virtual environments before warehouse reads",
            expected_error_message=("Adapter 'clickhouse' does not support virtual environments"),
            expected_catalog_load_count=0,
            expected_query_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_capability_when_loading_snapshot_then_it_rejects_before_reads(
    test_case: PlanningSnapshotCapabilityTestCase,
) -> None:
    connection: SnapshotRecordingConnection = SnapshotRecordingConnection(
        catalog=build_snapshot_catalog(),
        metadata_result=AdapterQueryResult(rows=()),
        virtual_environments=False,
    )

    with pytest.raises(
        AdapterCapabilityError,
        match=test_case.expected_error_message,
    ):
        load_planning_warehouse_snapshot(client=connection, database="analytics")

    assert connection.catalog_load_count == test_case.expected_catalog_load_count
    assert connection.query_count == test_case.expected_query_count


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningSnapshotAssemblyTestCase(
            description="reads the direct catalog exactly once",
            expected_catalog_load_count=1,
            expected_query_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_capable_adapter_when_loading_direct_snapshot_then_it_reads_each_source_once(
    test_case: PlanningSnapshotAssemblyTestCase,
) -> None:
    catalog: CatalogSnapshot = build_snapshot_catalog()
    connection: SnapshotRecordingConnection = SnapshotRecordingConnection(
        catalog=catalog,
        metadata_result=AdapterQueryResult(rows=()),
        virtual_environments=False,
    )

    snapshot: DirectWarehouseSnapshot = load_direct_warehouse_snapshot(
        client=connection,
        database="analytics",
    )

    assert snapshot.catalog is catalog
    assert connection.catalog_load_count == test_case.expected_catalog_load_count
    assert connection.query_count == test_case.expected_query_count


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningSnapshotCapabilityTestCase(
            description="rejects adapters without direct rebuild support before warehouse reads",
            expected_error_message="Adapter 'clickhouse' does not support direct rebuilds",
            expected_catalog_load_count=0,
            expected_query_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_incapable_adapter_when_loading_direct_snapshot_then_it_rejects_before_reads(
    test_case: PlanningSnapshotCapabilityTestCase,
) -> None:
    connection: SnapshotRecordingConnection = SnapshotRecordingConnection(
        catalog=build_snapshot_catalog(),
        metadata_result=AdapterQueryResult(rows=()),
        virtual_environments=False,
        direct_rebuild=False,
    )

    with pytest.raises(AdapterCapabilityError, match=test_case.expected_error_message):
        load_direct_warehouse_snapshot(client=connection, database="analytics")

    assert connection.catalog_load_count == test_case.expected_catalog_load_count
    assert connection.query_count == test_case.expected_query_count
