import pytest

from sqltide._native import load_deployments
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterMetadataObjectKey,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    CatalogRelation,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
    InspectedPhysicalTableCandidate,
)
from sqltide.executor.deployment.models import DeploymentInventory
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.deployment._test_types import (
    DeploymentInventoryTestCase,
    PartialDeploymentInventoryTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentInventoryTestCase(
            description="classifies complete lifecycle and drift evidence",
            deployment_statuses=(
                ("20260806T000500Z_missing", "staged"),
                ("20260806T000400Z_incomplete", "incomplete"),
                ("20260806T000300Z_staged", "staged"),
                ("20260806T000200Z_superseded", "staged"),
                ("20260806T000100Z_active", "staged"),
            ),
            existing_deployment_ids=(
                "20260806T000400Z_incomplete",
                "20260806T000300Z_staged",
                "20260806T000200Z_superseded",
                "20260806T000100Z_active",
            ),
            expected_states=(
                ("20260806T000600Z_catalog", "metadata_missing"),
                ("20260806T000500Z_missing", "physical_missing"),
                ("20260806T000400Z_incomplete", "incomplete"),
                ("20260806T000300Z_staged", "staged"),
                ("20260806T000200Z_superseded", "superseded"),
                ("20260806T000100Z_active", "active"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_authoritative_evidence_when_loading_inventory_then_classifies_deployments(
    test_case: DeploymentInventoryTestCase,
) -> None:
    records: tuple[AdapterDeploymentRecord, ...] = tuple(
        AdapterDeploymentRecord(
            deployment_id=deployment_id,
            created_at=deployment_id[:15],
            status=status,
            replay_lineage_mode="offsets",
            selected_root_keys=(AdapterMetadataObjectKey(None, "table", "tbl__orders"),),
            warning_codes=(),
            prepared_object_mappings=(
                AdapterPreparedObjectMapping(
                    logical_key=AdapterMetadataObjectKey(None, "table", "tbl__orders"),
                    physical_name=f"tbl__orders__{deployment_id}",
                    logical_model_name="orders",
                ),
            ),
        )
        for deployment_id, status in test_case.deployment_statuses
    )
    client: RecordingAdapterConnection = RecordingAdapterConnection(
        relations=tuple(
            CatalogRelation(
                name=f"tbl__orders__{deployment_id}",
                engine="MergeTree",
                columns=(),
            )
            for deployment_id in test_case.existing_deployment_ids
        ),
        managed_table_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="tbl__orders",
                    physical_name="tbl__orders__20260806T000100Z_active",
                ),
            ),
            physical_candidates=tuple(
                InspectedPhysicalTableCandidate(
                    database="analytics",
                    logical_name="tbl__orders",
                    physical_name=f"tbl__orders__{deployment_id}",
                )
                for deployment_id in (
                    *test_case.existing_deployment_ids,
                    "20260806T000600Z_catalog",
                )
            ),
        ),
        deployment_inventory=AdapterDeploymentInventory(
            deployments=records,
            publish_events=(
                AdapterPublishEventRecord(
                    deployment_id="20260806T000100Z_active",
                    published_at="2026-08-06 00:10:00.000",
                    logical_view_names=("tbl__orders",),
                ),
                AdapterPublishEventRecord(
                    deployment_id="20260806T000200Z_superseded",
                    published_at="2026-08-06 00:05:00.000",
                    logical_view_names=("tbl__orders",),
                ),
            ),
        ),
    )

    inventory: DeploymentInventory = load_deployments(
        client=client,
        metadata_database="metadata",
        default_database="analytics",
    )

    assert (
        tuple(
            (deployment.deployment_id, deployment.state.value)
            for deployment in inventory.deployments
        )
        == test_case.expected_states
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PartialDeploymentInventoryTestCase(
            description="active deployment with one deleted mapping is invalid",
            expected_state="physical_missing",
            expected_missing_logical_name="orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_partial_deployment_when_loading_inventory_then_it_is_physical_missing(
    test_case: PartialDeploymentInventoryTestCase,
) -> None:
    deployment_id = "20260806T000100Z_partial"
    existing_name = f"customers__{deployment_id}"
    missing_name = f"orders__{deployment_id}"
    client: RecordingAdapterConnection = RecordingAdapterConnection(
        relations=(CatalogRelation(name=existing_name, engine="MergeTree", columns=()),),
        managed_table_state=InspectedManagedTableState(
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="prices",
                    physical_name=existing_name,
                ),
            ),
            physical_candidates=(
                InspectedPhysicalTableCandidate(
                    database="analytics",
                    logical_name="prices",
                    physical_name=existing_name,
                ),
            ),
        ),
        deployment_inventory=AdapterDeploymentInventory(
            deployments=(
                AdapterDeploymentRecord(
                    deployment_id=deployment_id,
                    created_at="2026-08-06 00:01:00.000",
                    status="published",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=tuple(
                        AdapterPreparedObjectMapping(
                            logical_key=AdapterMetadataObjectKey(None, "table", logical_name),
                            physical_name=physical_name,
                            logical_model_name=logical_name,
                        )
                        for logical_name, physical_name in (
                            ("orders", missing_name),
                            ("prices", existing_name),
                        )
                    ),
                ),
            ),
            publish_events=(
                AdapterPublishEventRecord(
                    deployment_id=deployment_id,
                    published_at="2026-08-06 00:02:00.000",
                    logical_view_names=("orders", "prices"),
                ),
            ),
        ),
    )

    inventory: DeploymentInventory = load_deployments(
        client=client,
        metadata_database="metadata",
        default_database="analytics",
    )

    assert inventory.deployments[0].state.value == test_case.expected_state
    assert inventory.deployments[0].missing_physical_relation_names == (missing_name,)
    assert test_case.expected_missing_logical_name in missing_name
