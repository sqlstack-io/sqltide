import pytest

from sqltide._native import execute_publish
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterMetadataObjectKey,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    CatalogRelation,
    InspectedActiveTableBinding,
    InspectedManagedTableState,
)
from sqltide.executor.promotion.models import PublishedView, PublishRequest, PublishResult
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.promotion.main._test_types import (
    PublishCapabilityRejectionTestCase,
    PublishWorkflowTestCase,
)
from tests.unit.src.sqltide.executor.promotion.main.helpers import (
    PublishWorkflowAdapterConnection,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PublishWorkflowTestCase(
            description="executes replacement removal migration and publish event SQL in order",
            request=PublishRequest(
                deployment_id="20260726T190000Z_ab12cd",
                metadata_database="metadata",
                default_database="analytics",
            ),
            managed_table_state=InspectedManagedTableState(
                active_bindings=(
                    InspectedActiveTableBinding(
                        database="analytics",
                        logical_name="tbl__orders_legacy",
                        physical_name="tbl__orders_legacy__20260720T190000Z_ef34gh",
                    ),
                ),
                physical_candidates=(),
            ),
            deployment_inventory=AdapterDeploymentInventory(
                deployments=(
                    AdapterDeploymentRecord(
                        deployment_id="20260720T190000Z_ef34gh",
                        created_at="2026-07-20 19:00:00.000",
                        status="published",
                        replay_lineage_mode="offsets",
                        selected_root_keys=(),
                        warning_codes=(),
                        prepared_object_mappings=(
                            AdapterPreparedObjectMapping(
                                logical_key=AdapterMetadataObjectKey(
                                    database=None,
                                    object_type="table",
                                    name="tbl__orders_legacy",
                                ),
                                physical_name=("tbl__orders_legacy__20260720T190000Z_ef34gh"),
                                logical_model_name="orders",
                            ),
                        ),
                    ),
                    AdapterDeploymentRecord(
                        deployment_id="20260726T190000Z_ab12cd",
                        created_at="2026-07-26 19:00:00.000",
                        status="backfilling",
                        replay_lineage_mode="offsets",
                        selected_root_keys=(),
                        warning_codes=(),
                        prepared_object_mappings=(
                            AdapterPreparedObjectMapping(
                                logical_key=AdapterMetadataObjectKey(
                                    database=None,
                                    object_type="table",
                                    name="tbl__orders_enriched",
                                ),
                                physical_name=("tbl__orders_enriched__20260726T190000Z_ab12cd"),
                                logical_model_name="orders",
                            ),
                        ),
                    ),
                ),
                publish_events=(
                    AdapterPublishEventRecord(
                        deployment_id="20260720T190000Z_ef34gh",
                        published_at="2026-07-20 19:05:00.000",
                        logical_view_names=("tbl__orders_legacy",),
                    ),
                ),
            ),
            relations=(
                CatalogRelation(
                    name="tbl__orders_enriched__20260726T190000Z_ab12cd",
                    engine="MergeTree",
                    columns=(),
                ),
            ),
            expected_statements=(
                "CREATE OR REPLACE VIEW analytics.tbl__orders_enriched AS\n"
                "SELECT * FROM analytics.tbl__orders_enriched__20260726T190000Z_ab12cd;",
                "DROP VIEW IF EXISTS analytics.tbl__orders_legacy SYNC;",
                "CREATE DATABASE IF NOT EXISTS metadata;",
                "INSERT INTO metadata._sqltide_virtual_publications "
                "(publication_id, deployment_id, operation, previous_deployment_id, "
                "logical_database_name, logical_view_name, physical_database_name, "
                "physical_relation_name, published_at) VALUES\n"
                "('00000000000000000001_"
                "52a0bd798d318e48aa2d4e4de7c9439f2aadb94fbc0d5ae521a015797a60eb77', "
                "'20260726T190000Z_ab12cd', 'promote', NULL, 'analytics', "
                "'tbl__orders_enriched', "
                "'analytics', 'tbl__orders_enriched__20260726T190000Z_ab12cd', "
                "'2026-07-31 12:00:00.000');",
            ),
            expected_result=PublishResult(
                deployment_id="20260726T190000Z_ab12cd",
                published_views=(
                    PublishedView(
                        view_name="tbl__orders_enriched",
                        target_table_name=("tbl__orders_enriched__20260726T190000Z_ab12cd"),
                    ),
                ),
                per_relation_atomic_replace=True,
                graph_atomic_publish=False,
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_staged_deployment_when_publishing_then_exact_workflow_sql_reaches_gateway(
    test_case: PublishWorkflowTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "time.time_ns",
        lambda: 1,
    )
    connection: PublishWorkflowAdapterConnection = PublishWorkflowAdapterConnection(
        managed_table_state=test_case.managed_table_state,
        deployment_inventory=test_case.deployment_inventory,
        relations=test_case.relations,
    )

    result: PublishResult = execute_publish(request=test_case.request, client=connection)

    assert tuple(connection.statements) == test_case.expected_statements
    assert tuple(event[:2] for event in connection.target_mutation_lock_events) == (
        ("acquire", test_case.request.default_database),
        ("release", test_case.request.default_database),
    )
    assert connection.operation_events[0] == f"acquire:{test_case.request.default_database}"
    assert connection.operation_events[-1] == f"release:{test_case.request.default_database}"
    assert result == test_case.expected_result


@pytest.mark.parametrize(
    "test_case",
    [
        PublishCapabilityRejectionTestCase(
            description="rejects publish before inspection when stable bindings are unsupported",
            expected_error_fragment=(
                "Adapter 'clickhouse' does not support stable logical bindings"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adapter_without_stable_bindings_when_publishing_then_it_fails_before_writes(
    test_case: PublishCapabilityRejectionTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        stable_logical_bindings=False
    )

    with pytest.raises(AdapterCapabilityError, match=test_case.expected_error_fragment):
        execute_publish(
            request=PublishRequest(
                deployment_id="20260726T190000Z_ab12cd",
                metadata_database="analytics",
                default_database="analytics",
            ),
            client=connection,
        )

    assert connection.statements == []
