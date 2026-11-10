import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_deployment_audit as execute_audit_backfill
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.executor.readiness.models import (
    DeploymentAuditRequest as AuditBackfillRequest,
)
from sqltide.executor.readiness.models import (
    DeploymentAuditResult as AuditBackfillResult,
)
from sqltide.executor.readiness.models import (
    RootAuditResult,
)
from sqltide.executor.readiness.types import AuditAssessment
from tests.integration.src.sqltide.adapters.clickhouse.helpers import (
    render_create_kafka_table_ddl,
    render_create_materialized_view_ddl,
    render_create_table_ddl,
    render_create_view_ddl,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.backfill.helpers import (
    STAGED_ROW_FILTERS,
    build_offset_replay_compiled_pipeline,
    build_offset_target_insert_select_sql,
    build_raw_orders_row,
    build_replay_compiled_pipeline,
    build_scalar_replay_compiled_pipeline,
    build_scalar_replay_request,
    build_scalar_target_insert_select_sql,
    build_target_insert_select_sql,
    execute_backfill,
    require_managed_source,
    require_model_resources,
)
from tests.integration.src.sqltide.executor.readiness._test_types import (
    AuditAfterDeletedActiveViewIntegrationTestCase,
    AuditWithoutMetadataIntegrationTestCase,
    DanglingActiveViewAuditIntegrationTestCase,
    ExecuteAuditBackfillIntegrationTestCase,
    MissingAuditDeploymentIntegrationTestCase,
    OffsetAuditDegradedStateIntegrationTestCase,
    ResolveAuditDeploymentIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.readiness.helpers import (
    prepare_audit_resolution_scenario,
    prepare_audit_staged_materialized_view,
    prepare_degraded_offset_scenario,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MissingAuditDeploymentIntegrationTestCase(
            description="reports no staged deployment when inventory is empty",
            expected_error_fragment="No staged deployment candidates are available for audit",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_inventory_when_resolving_audit_without_deployment_id_then_it_raises(
    test_case: MissingAuditDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    try:
        with pytest.raises(ValueError, match=test_case.expected_error_fragment):
            execute_audit_backfill(
                request=AuditBackfillRequest(
                    deployment_id=None,
                    metadata_database=clickhouse_database,
                    default_database=clickhouse_database,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteAuditBackfillIntegrationTestCase(
            description="audits scalar timestamp replay deployment against active table",
            replay_lineage_mode="timestamp",
            deployment_id="20260409T160000Z_ab12cd",
            active_deployment_id="20260409T155500Z_prev01",
            created_at="2026-04-09 16:00:00.123",
            boundary_time="2026-04-09 16:00:00.000",
            staged_includes_live_row=True,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:00:01.000",
            expected_assessment=AuditAssessment.READY,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T160000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=2,
            expected_staged_row_count=2,
            expected_warning_codes=(),
        ),
        ExecuteAuditBackfillIntegrationTestCase(
            description="audits offset replay deployment against active table",
            replay_lineage_mode="offsets",
            expected_catchup_kind="offset",
            expected_partitions_compared=1,
            deployment_id="20260409T170000Z_ab12cd",
            active_deployment_id="20260409T165500Z_prev02",
            created_at="2026-04-09 17:00:00.123",
            boundary_time="2026-04-09 17:00:00.000",
            staged_includes_live_row=True,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:00:01.000",
            expected_assessment=AuditAssessment.READY,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T170000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=2,
            expected_staged_row_count=2,
            expected_warning_codes=(),
        ),
        ExecuteAuditBackfillIntegrationTestCase(
            description=(
                "reports scalar replay deployment as not ready when staged data lags active"
            ),
            replay_lineage_mode="timestamp",
            deployment_id="20260409T171000Z_ab12cd",
            active_deployment_id="20260409T170500Z_prev03",
            created_at="2026-04-09 17:10:00.123",
            boundary_time="2026-04-09 17:10:00.000",
            staged_includes_live_row=False,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:10:01.000",
            expected_assessment=AuditAssessment.NOT_READY,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T171000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=2,
            expected_staged_row_count=1,
            expected_warning_codes=(),
        ),
        ExecuteAuditBackfillIntegrationTestCase(
            description=(
                "reports offset replay deployment as not ready when staged source freshness "
                "lags raw"
            ),
            replay_lineage_mode="offsets",
            expected_catchup_kind="offset",
            expected_partitions_compared=1,
            deployment_id="20260409T172000Z_ab12cd",
            active_deployment_id="20260409T171500Z_prev04",
            created_at="2026-04-09 17:20:00.123",
            boundary_time="2026-04-09 17:20:00.000",
            staged_includes_live_row=False,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:10:01.000",
            expected_assessment=AuditAssessment.NOT_READY,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T172000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=2,
            expected_staged_row_count=1,
            expected_warning_codes=(),
        ),
        ExecuteAuditBackfillIntegrationTestCase(
            description=(
                "reports deployment as not ready when active target has rows "
                "but staged table is empty"
            ),
            replay_lineage_mode="timestamp",
            deployment_id="20260409T173000Z_ab12cd",
            active_deployment_id="20260409T172500Z_prev05",
            created_at="2026-04-09 17:30:00.123",
            boundary_time="2026-04-09 17:30:00.000",
            staged_includes_live_row=False,
            staged_is_empty=True,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:10:01.000",
            expected_assessment=AuditAssessment.NOT_READY,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T173000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=2,
            expected_staged_row_count=0,
            expected_warning_codes=(),
            expected_root_warnings=(
                "staged row count is far below active row count for tbl__orders_enriched",
            ),
        ),
        ExecuteAuditBackfillIntegrationTestCase(
            description="reports deployment as caution when active target far exceeds staged rows",
            replay_lineage_mode="timestamp",
            deployment_id="20260409T174000Z_ab12cd",
            active_deployment_id="20260409T173500Z_prev06",
            created_at="2026-04-09 17:40:00.123",
            boundary_time="2026-04-09 17:40:00.000",
            staged_includes_live_row=True,
            historical_row_time="2026-04-09 15:59:59.000",
            live_row_time="2026-04-09 16:10:01.000",
            expected_assessment=AuditAssessment.CAUTION,
            expected_root_name="tbl__orders_enriched",
            expected_staged_physical_name="tbl__orders_enriched__20260409T174000Z_ab12cd",
            expected_active_exists=True,
            expected_active_row_count=5,
            expected_staged_row_count=2,
            expected_warning_codes=(),
            expected_root_warnings=(
                "staged row count is far below active row count for tbl__orders_enriched",
            ),
            extra_active_only_rows=3,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_staged_backfill_when_auditing_then_it_returns_expected_comparison_signals(
    test_case: ExecuteAuditBackfillIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_replay_compiled_pipeline(
        replay_lineage_mode=test_case.replay_lineage_mode
    )

    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    active_physical_name: str = f"tbl__orders_enriched__{test_case.active_deployment_id}"
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.{active_physical_name}",
            1,
        )
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name=require_model_resources(compiled_pipeline).target_table.name,
            target_table_name=active_physical_name,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp=test_case.historical_row_time,
                _replay_landed_at=test_case.historical_row_time,
            ),
            build_raw_orders_row(
                kafka_key="live-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp=test_case.live_row_time,
                _replay_landed_at=test_case.live_row_time,
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.{active_physical_name} "
        + build_target_insert_select_sql(
            replay_lineage_mode=test_case.replay_lineage_mode,
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    extra_row_index: int
    for extra_row_index in range(test_case.extra_active_only_rows):
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_database}.{active_physical_name} "
            "(order_id, _replay_timestamp) VALUES "
            f"('legacy-order-{extra_row_index}', "
            f"toDateTime64('2026-04-09 15:00:0{extra_row_index}.000', 3))"
        )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )
    staged_physical_name: str = f"tbl__orders_enriched__{test_case.deployment_id}"
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.{staged_physical_name}",
            1,
        )
    )
    prepare_audit_staged_materialized_view(
        replay_lineage_mode=test_case.replay_lineage_mode,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
        deployment_id=test_case.deployment_id,
        staged_physical_name=staged_physical_name,
    )

    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.{staged_physical_name} "
        + build_target_insert_select_sql(
            replay_lineage_mode=test_case.replay_lineage_mode,
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
        + STAGED_ROW_FILTERS[(test_case.staged_is_empty, test_case.staged_includes_live_row)]
    )

    try:
        audit_result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    root_result: RootAuditResult = audit_result.root_results[0]

    assert audit_result.deployment_id == test_case.deployment_id
    assert audit_result.assessment == test_case.expected_assessment
    assert audit_result.warning_codes == test_case.expected_warning_codes
    assert root_result.root_key.name == test_case.expected_root_name
    assert root_result.staged_physical_name == test_case.expected_staged_physical_name
    assert root_result.staged_exists is True
    assert root_result.active_exists is test_case.expected_active_exists
    assert root_result.active_row_count == test_case.expected_active_row_count
    assert root_result.staged_row_count == test_case.expected_staged_row_count
    assert root_result.warnings == test_case.expected_root_warnings
    assert root_result.assessment == test_case.expected_assessment
    catchup_summaries: dict[str, object | None] = {
        "offset": root_result.offset_catchup_summary,
        "scalar": root_result.scalar_catchup_summary,
    }
    assert catchup_summaries[test_case.expected_catchup_kind] is not None
    assert (
        getattr(root_result.offset_catchup_summary, "partitions_compared", None)
        == test_case.expected_partitions_compared
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ResolveAuditDeploymentIntegrationTestCase(
            description="auto resolves latest staged deployment newer than active view target",
            create_active_view=True,
            first_deployment_id="20260409T210000Z_ab12cd",
            second_deployment_id="20260409T210500Z_cd34ef",
            expected_resolved_deployment_id="20260409T210500Z_cd34ef",
            expected_error_fragment=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_request_without_deployment_id_when_active_view_exists_then_it_resolves_latest(
    test_case: ResolveAuditDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = prepare_audit_resolution_scenario(
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        first_deployment_id=test_case.first_deployment_id,
        second_deployment_id=test_case.second_deployment_id,
        create_active_view=test_case.create_active_view,
    )

    try:
        result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=None,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    assert result.deployment_id == test_case.expected_resolved_deployment_id


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ResolveAuditDeploymentIntegrationTestCase(
            description="requires explicit choice with no active view and many staged deployments",
            create_active_view=False,
            first_deployment_id="20260409T200000Z_ab12cd",
            second_deployment_id="20260409T200500Z_cd34ef",
            expected_resolved_deployment_id=None,
            expected_error_fragment="Audit deployment resolution is ambiguous",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_audit_request_without_deployment_id_when_resolution_is_ambiguous_then_it_raises(
    test_case: ResolveAuditDeploymentIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    managed_client: AdapterConnection = prepare_audit_resolution_scenario(
        connection_settings=clickhouse_connection_settings,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        first_deployment_id=test_case.first_deployment_id,
        second_deployment_id=test_case.second_deployment_id,
        create_active_view=test_case.create_active_view,
    )
    try:
        with pytest.raises(ValueError, match=test_case.expected_error_fragment):
            execute_audit_backfill(
                request=AuditBackfillRequest(
                    deployment_id=None,
                    metadata_database=clickhouse_database,
                    default_database=clickhouse_database,
                ),
                client=managed_client,
            )
    finally:
        managed_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        AuditWithoutMetadataIntegrationTestCase(
            description="audits staged deployment after metadata deletion using live state only",
            deployment_id="20260409T190000Z_ab12cd",
            expected_assessment=AuditAssessment.READY,
            expected_active_exists=False,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_audit_metadata_when_auditing_then_it_uses_live_clickhouse_state(
    test_case: AuditWithoutMetadataIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 18:59:59.000",
                _replay_landed_at="2026-04-09 18:59:59.000",
            ),
            build_raw_orders_row(
                kafka_key="live-order",
                _replay_partition=0,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 19:00:01.000",
                _replay_landed_at="2026-04-09 19:00:01.000",
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        execute_backfill(
            request=build_scalar_replay_request(
                database=clickhouse_database,
                deployment_id=test_case.deployment_id,
                created_at="2026-04-09 19:00:00.123",
                boundary_time="2026-04-09 19:00:00.000",
                replay_lineage_mode="timestamp",
            ),
            client=managed_client,
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_replay_boundaries"
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_deployments"
        )
        clickhouse_client.command(
            f"DROP TABLE IF EXISTS {clickhouse_database}._sqltide_virtual_object_state"
        )
        audit_result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    root_result: RootAuditResult = audit_result.root_results[0]

    assert audit_result.deployment_id == test_case.deployment_id
    assert audit_result.assessment == test_case.expected_assessment
    assert root_result.staged_exists is True
    assert root_result.active_exists is test_case.expected_active_exists
    assert root_result.active_row_count is None
    assert root_result.staged_row_count == 1
    assert root_result.assessment == test_case.expected_assessment


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        DanglingActiveViewAuditIntegrationTestCase(
            description="returns caution when the active stable view points to a missing table",
            deployment_id="20260409T191000Z_ab12cd",
            active_deployment_id="20260409T190500Z_prev04",
            expected_assessment=AuditAssessment.CAUTION,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dangling_active_view_when_auditing_then_it_returns_caution(
    test_case: DanglingActiveViewAuditIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 19:09:59.000",
                _replay_landed_at="2026-04-09 19:09:59.000",
            )
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id}",
            1,
        )
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name="tbl__orders_enriched",
            target_table_name=(f"tbl__orders_enriched__{test_case.active_deployment_id}"),
        )
    )
    clickhouse_client.command(
        f"DROP TABLE {clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id}"
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id}",
            1,
        )
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id} "
        + build_scalar_target_insert_select_sql(
            replay_lineage_mode="timestamp",
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        audit_result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    root_result: RootAuditResult = audit_result.root_results[0]

    assert audit_result.assessment == test_case.expected_assessment
    assert root_result.active_exists is True
    assert root_result.active_row_count is None
    assert root_result.staged_exists is True
    assert root_result.assessment == test_case.expected_assessment


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        AuditAfterDeletedActiveViewIntegrationTestCase(
            description="audits a staged deployment after the active stable view was deleted",
            deployment_id="20260409T192000Z_ab12cd",
            active_deployment_id="20260409T191500Z_prev05",
            expected_assessment=AuditAssessment.READY,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deleted_active_view_when_auditing_then_it_uses_live_staged_state(
    test_case: AuditAfterDeletedActiveViewIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_scalar_replay_compiled_pipeline("timestamp")
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            (f"{clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id}"),
            1,
        )
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name="tbl__orders_enriched",
            target_table_name=(f"tbl__orders_enriched__{test_case.active_deployment_id}"),
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="historical-order",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 19:19:59.000",
                _replay_landed_at="2026-04-09 19:19:59.000",
            )
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    clickhouse_client.command(
        "INSERT INTO "
        f"{clickhouse_database}.tbl__orders_enriched__{test_case.active_deployment_id} "
        + build_scalar_target_insert_select_sql(
            replay_lineage_mode="timestamp",
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id}",
            1,
        )
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.tbl__orders_enriched__{test_case.deployment_id} "
        + build_scalar_target_insert_select_sql(
            replay_lineage_mode="timestamp",
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    clickhouse_client.command(f"DROP VIEW {clickhouse_database}.tbl__orders_enriched")
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        audit_result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    root_result: RootAuditResult = audit_result.root_results[0]

    assert audit_result.assessment == test_case.expected_assessment
    assert root_result.active_exists is False
    assert root_result.active_row_count is None
    assert root_result.staged_exists is True
    assert root_result.staged_row_count == 1
    assert root_result.assessment == test_case.expected_assessment


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        OffsetAuditDegradedStateIntegrationTestCase(
            description=(
                "returns caution when the staged offset deployment is missing an active partition"
            ),
            deployment_id="20260409T193000Z_ab12cd",
            active_deployment_id="20260409T192500Z_prev06",
            scenario_kind="missing_staged_partition",
            expected_assessment=AuditAssessment.CAUTION,
        ),
        OffsetAuditDegradedStateIntegrationTestCase(
            description=(
                "returns caution when the staged offset deployment has no source lookup "
                "materialized view"
            ),
            deployment_id="20260409T194000Z_ab12cd",
            active_deployment_id="20260409T193500Z_prev07",
            scenario_kind="missing_source_lookup",
            expected_assessment=AuditAssessment.CAUTION,
        ),
        OffsetAuditDegradedStateIntegrationTestCase(
            description="returns caution when raw landing rows are missing for staged max offsets",
            deployment_id="20260409T195000Z_ab12cd",
            active_deployment_id="20260409T194500Z_prev08",
            scenario_kind="missing_raw_rows_for_staged_offsets",
            expected_assessment=AuditAssessment.CAUTION,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_degraded_offset_state_when_auditing_then_it_returns_caution(
    test_case: OffsetAuditDegradedStateIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    compiled_pipeline: CompiledPipeline = build_offset_replay_compiled_pipeline()
    active_physical_name: str = f"tbl__orders_enriched__{test_case.active_deployment_id}"
    staged_physical_name: str = f"tbl__orders_enriched__{test_case.deployment_id}"
    clickhouse_client.command(
        render_create_kafka_table_ddl(
            table=require_managed_source(compiled_pipeline).kafka_table,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_managed_source(compiled_pipeline).raw_table, database=clickhouse_database
        )
    )
    clickhouse_client.command(
        render_create_materialized_view_ddl(
            materialized_view=require_managed_source(compiled_pipeline).materialized_view,
            database=clickhouse_database,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.{active_physical_name}",
            1,
        )
    )
    clickhouse_client.command(
        render_create_table_ddl(
            table=require_model_resources(compiled_pipeline).target_table,
            database=clickhouse_database,
        ).replace(
            f"{clickhouse_database}.{require_model_resources(compiled_pipeline).target_table.name}",
            f"{clickhouse_database}.{staged_physical_name}",
            1,
        )
    )
    clickhouse_client.command(
        render_create_view_ddl(
            database=clickhouse_database,
            view_name="tbl__orders_enriched",
            target_table_name=active_physical_name,
        )
    )
    clickhouse_client.insert(
        table=f"{clickhouse_database}.{require_managed_source(compiled_pipeline).raw_table.name}",
        data=[
            build_raw_orders_row(
                kafka_key="order-p0-historical",
                _replay_partition=0,
                _replay_offset=1,
                _replay_timestamp="2026-04-09 19:29:59.000",
                _replay_landed_at="2026-04-09 19:29:59.000",
            ),
            build_raw_orders_row(
                kafka_key="order-p1-live",
                _replay_partition=1,
                _replay_offset=2,
                _replay_timestamp="2026-04-09 19:30:01.000",
                _replay_landed_at="2026-04-09 19:30:01.000",
            ),
        ],
        column_names=[
            "kafka_key",
            "kafka_value",
            "kafka_topic",
            "_replay_partition",
            "_replay_offset",
            "_replay_timestamp",
            "kafka_header_keys",
            "kafka_header_values",
            "_replay_landed_at",
        ],
    )
    clickhouse_client.command(
        f"INSERT INTO {clickhouse_database}.{active_physical_name} "
        + build_offset_target_insert_select_sql(
            database=clickhouse_database,
            source_table_name=require_managed_source(compiled_pipeline).raw_table.name,
        )
    )
    prepare_degraded_offset_scenario(
        scenario_kind=test_case.scenario_kind,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        compiled_pipeline=compiled_pipeline,
        deployment_id=test_case.deployment_id,
        staged_physical_name=staged_physical_name,
    )
    managed_client: AdapterConnection = ClickHouseAdapter().connect(
        AdapterConnectionConfig(
            host=clickhouse_connection_settings.host,
            port=clickhouse_connection_settings.port,
            username=clickhouse_connection_settings.username,
            password=clickhouse_connection_settings.password,
            database=clickhouse_database,
        )
    )

    try:
        audit_result: AuditBackfillResult = execute_audit_backfill(
            request=AuditBackfillRequest(
                deployment_id=test_case.deployment_id,
                metadata_database=clickhouse_database,
                default_database=clickhouse_database,
            ),
            client=managed_client,
        )
    finally:
        managed_client.close()

    root_result: RootAuditResult = audit_result.root_results[0]

    assert audit_result.assessment == test_case.expected_assessment
    assert root_result.assessment == test_case.expected_assessment
    assert root_result.offset_catchup_summary is not None
