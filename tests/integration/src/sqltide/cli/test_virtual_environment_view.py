from pathlib import Path
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_janitor
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterDeploymentRecord
from sqltide.executor.janitor.models import JanitorApplyResult, JanitorRequest
from sqltide.executor.promotion.main.execute_deployment_promotion import execute_publish
from sqltide.executor.promotion.models import PublishRequest
from tests.integration.src.sqltide.cli._test_types import (
    CliVirtualEnvironmentRenameIntegrationTestCase,
    CliVirtualEnvironmentViewIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    build_managed_clickhouse_client,
    deployment_watermark_count,
    prepare_virtual_environment_view_sources,
    run_new_virtual_environment_deployment,
    virtual_environment_view_rows,
    write_virtual_environment_table_model,
    write_virtual_environment_view_model,
    write_virtual_environment_view_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliVirtualEnvironmentViewIntegrationTestCase(
            description="custom table and terminal view deploy without view replay state",
            expected_initial_rows=(("order-1", "Ada"), ("order-2", "Grace")),
            expected_revised_rows=(("order-1", "Ada!"), ("order-2", "Grace!")),
            expected_final_rows=(("order-1", "Ada!!"), ("order-2", "Grace!!")),
            expected_initial_mapping_types=("materialized_view", "table", "view"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_terminal_view_when_running_vde_lifecycle_then_publish_and_cleanup_are_safe(
    test_case: CliVirtualEnvironmentViewIntegrationTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_virtual_environment_view_project(project_root=tmp_path)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        prepare_virtual_environment_view_sources(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
        )

        initial_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        initial_rows: tuple[tuple[str, str], ...] = virtual_environment_view_rows(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        initial_watermark_count: int = deployment_watermark_count(
            connection=connection,
            database=clickhouse_database,
            deployment_id=initial_deployment.deployment_id,
        )

        settled_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        settled_watermark_count: int = deployment_watermark_count(
            connection=connection,
            database=clickhouse_database,
            deployment_id=settled_deployment.deployment_id,
        )

        write_virtual_environment_view_model(
            project_root=tmp_path,
            customer_name_expression="CAST(concat(customers.customer_name, '!') AS String)",
        )
        revised_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=revised_deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        revised_rows: tuple[tuple[str, str], ...] = virtual_environment_view_rows(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )

        write_virtual_environment_view_model(
            project_root=tmp_path,
            customer_name_expression="CAST(concat(customers.customer_name, '!!') AS String)",
        )
        final_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=final_deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        final_rows: tuple[tuple[str, str], ...] = virtual_environment_view_rows(
            clickhouse_client=clickhouse_client, database=clickhouse_database
        )
        revised_physical_name: str = revised_deployment.prepared_object_mappings[0].physical_name
        janitor_result: JanitorApplyResult = cast(
            JanitorApplyResult,
            execute_janitor(
                request=JanitorRequest(
                    database=clickhouse_database,
                    metadata_database=clickhouse_database,
                    retention_days=0,
                    apply=True,
                    minimum_rollback_deployments=0,
                ),
                client=connection,
            ),
        )
        remaining_relation_names: frozenset[str] = frozenset(
            relation.name for relation in connection.load_catalog(clickhouse_database).relations
        )
        _ = capsys.readouterr()
    finally:
        connection.close()

    assert initial_rows == test_case.expected_initial_rows
    assert revised_rows == test_case.expected_revised_rows
    assert final_rows == test_case.expected_final_rows
    assert (
        tuple(
            sorted(
                mapping.logical_key.object_type
                for mapping in initial_deployment.prepared_object_mappings
            )
        )
        == test_case.expected_initial_mapping_types
    )
    assert initial_watermark_count == 1
    assert settled_deployment.prepared_object_mappings == ()
    assert settled_watermark_count == 0
    assert revised_deployment.selected_root_keys[0].object_type == "view"
    assert revised_deployment.deployment_id in janitor_result.deleted_deployment_ids
    assert revised_physical_name in janitor_result.deleted_object_names
    assert revised_physical_name not in remaining_relation_names
    assert final_deployment.deployment_id not in janitor_result.deleted_deployment_ids


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliVirtualEnvironmentRenameIntegrationTestCase(
            description="VDE relation rename removes aliases and leaves history for janitor",
            initial_table_name="order_facts",
            initial_view_name="customer_orders",
            renamed_table_name="renamed_order_facts",
            renamed_view_name="renamed_customer_orders",
            expected_rows=(("order-1", "Ada"), ("order-2", "Grace")),
            expected_logical_model_names=(
                "customer_orders",
                "orders_enriched",
                "orders_enriched",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_vde_models_when_relation_names_change_then_aliases_are_removed(
    test_case: CliVirtualEnvironmentRenameIntegrationTestCase,
    tmp_path: Path,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    write_virtual_environment_view_project(project_root=tmp_path)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        prepare_virtual_environment_view_sources(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
        )
        initial_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=initial_deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        write_virtual_environment_table_model(
            project_root=tmp_path,
            relation_name=test_case.renamed_table_name,
        )
        write_virtual_environment_view_model(
            project_root=tmp_path,
            customer_name_expression="customers.customer_name::String",
            relation_name=test_case.renamed_view_name,
        )
        renamed_deployment: AdapterDeploymentRecord = run_new_virtual_environment_deployment(
            project_root=tmp_path,
            database=clickhouse_database,
            connection=connection,
            clickhouse_client=clickhouse_client,
        )
        execute_publish(
            request=PublishRequest(
                deployment_id=renamed_deployment.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=connection,
        )
        renamed_rows: tuple[tuple[str, str], ...] = virtual_environment_view_rows(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            view_name=test_case.renamed_view_name,
        )
        relation_names_before_janitor: frozenset[str] = connection.load_catalog(
            clickhouse_database
        ).relation_names()
        initial_physical_names: frozenset[str] = frozenset(
            mapping.physical_name for mapping in initial_deployment.prepared_object_mappings
        )
        renamed_physical_names: frozenset[str] = frozenset(
            mapping.physical_name for mapping in renamed_deployment.prepared_object_mappings
        )
        janitor_result: JanitorApplyResult = cast(
            JanitorApplyResult,
            execute_janitor(
                request=JanitorRequest(
                    database=clickhouse_database,
                    metadata_database=clickhouse_database,
                    retention_days=0,
                    apply=True,
                    minimum_rollback_deployments=0,
                ),
                client=connection,
            ),
        )
        relation_names_after_janitor: frozenset[str] = connection.load_catalog(
            clickhouse_database
        ).relation_names()
    finally:
        connection.close()

    assert renamed_rows == test_case.expected_rows
    assert test_case.initial_table_name not in relation_names_before_janitor
    assert test_case.initial_view_name not in relation_names_before_janitor
    assert test_case.renamed_table_name in relation_names_after_janitor
    assert test_case.renamed_view_name in relation_names_after_janitor
    assert initial_physical_names.issubset(relation_names_before_janitor)
    assert initial_physical_names.isdisjoint(relation_names_after_janitor)
    assert renamed_physical_names.issubset(relation_names_after_janitor)
    assert initial_deployment.deployment_id in janitor_result.deleted_deployment_ids
    assert (
        tuple(
            sorted(
                mapping.logical_model_name
                for mapping in renamed_deployment.prepared_object_mappings
            )
        )
        == test_case.expected_logical_model_names
    )
