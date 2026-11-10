import pytest

from sqltide._native import execute_deployment_diff
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterMetadataObjectKey,
    AdapterPreparedObjectMapping,
    CatalogColumn,
    CatalogRelation,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.executor.deployment.exceptions import DeploymentDiffError
from sqltide.executor.deployment.models import (
    DeploymentDiffColumn,
    DeploymentDiffRelation,
    DeploymentDiffRequest,
    DeploymentDiffResult,
)
from sqltide.executor.deployment.types import DeploymentDiffStatus
from tests.unit.src.sqltide.executor.deployment.main._test_types import (
    DeploymentDiffEndpointTestCase,
    DeploymentDiffErrorTestCase,
    DeploymentDiffResolvedStatusTestCase,
    DeploymentDiffSuccessTestCase,
)
from tests.unit.src.sqltide.executor.deployment.main.helpers import (
    DeploymentDiffRecordingAdapterConnection,
)

_TARGET_DEPLOYMENT_ID: str = "20260808T120000Z_target1"
_INVENTORY: AdapterDeploymentInventory = AdapterDeploymentInventory(
    deployments=(
        AdapterDeploymentRecord(
            deployment_id=_TARGET_DEPLOYMENT_ID,
            created_at="2026-08-08 12:00:00.000",
            status="staged",
            replay_lineage_mode="offsets",
            selected_root_keys=(),
            warning_codes=(),
            prepared_object_mappings=(
                AdapterPreparedObjectMapping(
                    logical_key=AdapterMetadataObjectKey(None, "table", "orders"),
                    physical_name=f"orders__{_TARGET_DEPLOYMENT_ID}",
                    logical_model_name="orders",
                ),
                AdapterPreparedObjectMapping(
                    logical_key=AdapterMetadataObjectKey(None, "table", "customers"),
                    physical_name=f"customers__{_TARGET_DEPLOYMENT_ID}",
                    logical_model_name="customers",
                ),
            ),
        ),
    ),
    publish_events=(),
)
_MANAGED_STATE: InspectedManagedTableState = InspectedManagedTableState(
    active_bindings=(
        InspectedActiveTableBinding(
            database="analytics",
            logical_name="orders",
            physical_name="orders__20260808T110000Z_active1",
        ),
    ),
    physical_candidates=(),
)
_RELATIONS: tuple[CatalogRelation, ...] = (
    CatalogRelation(
        name="orders__20260808T110000Z_active1",
        engine="MergeTree",
        columns=(CatalogColumn(name="order_id", type="String"),),
    ),
    CatalogRelation(
        name=f"orders__{_TARGET_DEPLOYMENT_ID}",
        engine="MergeTree",
        columns=(CatalogColumn(name="order_id", type="UInt64"),),
    ),
    CatalogRelation(
        name=f"customers__{_TARGET_DEPLOYMENT_ID}",
        engine="MergeTree",
        columns=(CatalogColumn(name="customer_id", type="String"),),
    ),
)
_ROW_COUNTS: dict[str, int] = {
    "SELECT count() AS row_count FROM `analytics`.`orders__20260808T110000Z_active1`": 2,
    f"SELECT count() AS row_count FROM `analytics`.`orders__{_TARGET_DEPLOYMENT_ID}`": 3,
    f"SELECT count() AS row_count FROM `analytics`.`customers__{_TARGET_DEPLOYMENT_ID}`": 1,
}


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffSuccessTestCase(
            description="single deployment uses active baseline and reports schema counts",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison=_TARGET_DEPLOYMENT_ID,
            ),
            expected_result=DeploymentDiffResult(
                database="analytics",
                from_endpoint="active",
                to_endpoint=_TARGET_DEPLOYMENT_ID,
                relations=(
                    DeploymentDiffRelation(
                        database="analytics",
                        logical_name="customers",
                        status=DeploymentDiffStatus.ADDED,
                        from_physical_name=None,
                        to_physical_name=f"customers__{_TARGET_DEPLOYMENT_ID}",
                        from_columns=(),
                        to_columns=(
                            DeploymentDiffColumn(
                                name="customer_id",
                                type="String",
                                default_expression=None,
                            ),
                        ),
                        from_row_count=None,
                        to_row_count=1,
                    ),
                    DeploymentDiffRelation(
                        database="analytics",
                        logical_name="orders",
                        status=DeploymentDiffStatus.CHANGED,
                        from_physical_name="orders__20260808T110000Z_active1",
                        to_physical_name=f"orders__{_TARGET_DEPLOYMENT_ID}",
                        from_columns=(
                            DeploymentDiffColumn(
                                name="order_id",
                                type="String",
                                default_expression=None,
                            ),
                        ),
                        to_columns=(
                            DeploymentDiffColumn(
                                name="order_id",
                                type="UInt64",
                                default_expression=None,
                            ),
                        ),
                        from_row_count=2,
                        to_row_count=3,
                    ),
                ),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_resolvable_endpoints_when_diffing_then_returns_schema_and_count_changes(
    test_case: DeploymentDiffSuccessTestCase,
) -> None:
    connection: DeploymentDiffRecordingAdapterConnection = DeploymentDiffRecordingAdapterConnection(
        relations=_RELATIONS,
        managed_table_state=_MANAGED_STATE,
        deployment_inventory=_INVENTORY,
        row_counts_by_statement=_ROW_COUNTS,
    )

    result: DeploymentDiffResult = execute_deployment_diff(
        request=test_case.request,
        client=connection,
    )

    assert result == test_case.expected_result


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffEndpointTestCase(
            description="explicit active endpoint preserves the authored range",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison=f"active:{_TARGET_DEPLOYMENT_ID}",
            ),
            expected_from_endpoint="active",
            expected_to_endpoint=_TARGET_DEPLOYMENT_ID,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_active_endpoint_when_diffing_then_uses_explicit_range(
    test_case: DeploymentDiffEndpointTestCase,
) -> None:
    connection: DeploymentDiffRecordingAdapterConnection = DeploymentDiffRecordingAdapterConnection(
        relations=_RELATIONS,
        managed_table_state=_MANAGED_STATE,
        deployment_inventory=_INVENTORY,
        row_counts_by_statement=_ROW_COUNTS,
    )

    result: DeploymentDiffResult = execute_deployment_diff(
        request=test_case.request,
        client=connection,
    )

    assert result.from_endpoint == test_case.expected_from_endpoint
    assert result.to_endpoint == test_case.expected_to_endpoint


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffErrorTestCase(
            description="rejects malformed range",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison="active:",
            ),
            expected_error_fragment="expects DEPLOYMENT or FROM:TO",
        ),
        DeploymentDiffErrorTestCase(
            description="rejects identical endpoints",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison=f"{_TARGET_DEPLOYMENT_ID}:{_TARGET_DEPLOYMENT_ID}",
            ),
            expected_error_fragment="endpoints must be different",
        ),
        DeploymentDiffErrorTestCase(
            description="rejects unknown deployment",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison="unknown",
            ),
            expected_error_fragment="Unknown deployment diff endpoint 'unknown'",
        ),
        DeploymentDiffErrorTestCase(
            description="rejects database names unsafe for generated count SQL",
            request=DeploymentDiffRequest(
                database="analytics\\` DROP TABLE orders",
                metadata_database="metadata",
                comparison=_TARGET_DEPLOYMENT_ID,
            ),
            expected_error_fragment="cannot query invalid identifier",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_endpoint_when_diffing_then_raises_clear_error(
    test_case: DeploymentDiffErrorTestCase,
) -> None:
    connection: DeploymentDiffRecordingAdapterConnection = DeploymentDiffRecordingAdapterConnection(
        relations=_RELATIONS,
        managed_table_state=_MANAGED_STATE,
        deployment_inventory=_INVENTORY,
        row_counts_by_statement=_ROW_COUNTS,
    )

    with pytest.raises(DeploymentDiffError, match=test_case.expected_error_fragment):
        execute_deployment_diff(request=test_case.request, client=connection)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffResolvedStatusTestCase(
            description="one-sided missing relation reports physical missing",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison=_TARGET_DEPLOYMENT_ID,
            ),
            inventory=_INVENTORY,
            managed_state=_MANAGED_STATE,
            relations=(_RELATIONS[0], _RELATIONS[1]),
            row_counts_by_statement=_ROW_COUNTS,
            expected_statuses=(
                DeploymentDiffStatus.PHYSICAL_MISSING,
                DeploymentDiffStatus.CHANGED,
            ),
            expected_statements=(
                "SELECT count() AS row_count FROM `analytics`.`orders__20260808T110000Z_active1`",
                f"SELECT count() AS row_count FROM `analytics`.`orders__{_TARGET_DEPLOYMENT_ID}`",
            ),
        ),
        DeploymentDiffResolvedStatusTestCase(
            description="different endpoint labels sharing one physical relation count once",
            request=DeploymentDiffRequest(
                database="analytics",
                metadata_database="metadata",
                comparison="active:20260808T110000Z_active1",
            ),
            inventory=AdapterDeploymentInventory(
                deployments=(
                    AdapterDeploymentRecord(
                        deployment_id="20260808T110000Z_active1",
                        created_at="2026-08-08 11:00:00.000",
                        status="published",
                        replay_lineage_mode="offsets",
                        selected_root_keys=(),
                        warning_codes=(),
                        prepared_object_mappings=(
                            AdapterPreparedObjectMapping(
                                logical_key=AdapterMetadataObjectKey(None, "table", "orders"),
                                physical_name="orders__20260808T110000Z_active1",
                                logical_model_name="orders",
                            ),
                        ),
                    ),
                ),
                publish_events=(),
            ),
            managed_state=_MANAGED_STATE,
            relations=(_RELATIONS[0],),
            row_counts_by_statement=_ROW_COUNTS,
            expected_statuses=(DeploymentDiffStatus.UNCHANGED,),
            expected_statements=(
                "SELECT count() AS row_count FROM `analytics`.`orders__20260808T110000Z_active1`",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_physical_edge_case_when_diffing_then_reports_stable_status(
    test_case: DeploymentDiffResolvedStatusTestCase,
) -> None:
    connection: DeploymentDiffRecordingAdapterConnection = DeploymentDiffRecordingAdapterConnection(
        relations=test_case.relations,
        managed_table_state=test_case.managed_state,
        deployment_inventory=test_case.inventory,
        row_counts_by_statement=test_case.row_counts_by_statement,
    )

    result: DeploymentDiffResult = execute_deployment_diff(
        request=test_case.request,
        client=connection,
    )

    assert tuple(relation.status for relation in result.relations) == test_case.expected_statuses
    assert tuple(connection.statements) == test_case.expected_statements


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
