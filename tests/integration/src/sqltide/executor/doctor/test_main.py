import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import execute_doctor
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.executor.doctor.models import ActiveViewStatus, DoctorRequest, DoctorResult
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.executor.doctor._test_types import (
    ExecuteDoctorIntegrationTestCase,
)
from tests.integration.src.sqltide.executor.doctor.helpers import prepare_doctor_state


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteDoctorIntegrationTestCase(
            description="reports active view healthy when one binding exists",
            setup_kind="active",
            active_view_target_deployment_id="20260731T120000Z_depaaa",
            candidate_deployment_ids=(
                "20260731T120000Z_depaaa",
                "20260731T130000Z_depbbb",
            ),
            invalid_active_view_target_name=None,
            expected_state_kind="active_view_present",
            expected_active_deployment_id="20260731T120000Z_depaaa",
            expected_candidate_deployment_ids=(
                "20260731T120000Z_depaaa",
                "20260731T130000Z_depbbb",
            ),
            expected_status_count=1,
        ),
        ExecuteDoctorIntegrationTestCase(
            description="reports recoverable missing logical view when one candidate exists",
            setup_kind="missing",
            active_view_target_deployment_id=None,
            candidate_deployment_ids=("20260731T120000Z_depaaa",),
            invalid_active_view_target_name=None,
            expected_state_kind="logical_view_missing",
            expected_active_deployment_id=None,
            expected_candidate_deployment_ids=("20260731T120000Z_depaaa",),
            expected_status_count=1,
        ),
        ExecuteDoctorIntegrationTestCase(
            description="reports ambiguous missing logical view when many candidates exist",
            setup_kind="missing",
            active_view_target_deployment_id=None,
            candidate_deployment_ids=(
                "20260731T120000Z_depaaa",
                "20260731T130000Z_depbbb",
            ),
            invalid_active_view_target_name=None,
            expected_state_kind="logical_view_missing",
            expected_active_deployment_id=None,
            expected_candidate_deployment_ids=(
                "20260731T120000Z_depaaa",
                "20260731T130000Z_depbbb",
            ),
            expected_status_count=1,
        ),
        ExecuteDoctorIntegrationTestCase(
            description=(
                "reports invalid active view when stable view points to a non-deployment table"
            ),
            setup_kind="invalid",
            active_view_target_deployment_id=None,
            candidate_deployment_ids=(),
            invalid_active_view_target_name="tbl__orders_enriched_manual",
            expected_state_kind="invalid_active_view",
            expected_active_deployment_id=None,
            expected_candidate_deployment_ids=(),
            expected_status_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_state_when_doctoring_then_it_reports_expected_active_view_status(
    test_case: ExecuteDoctorIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    prepare_doctor_state(
        setup_kind=test_case.setup_kind,
        clickhouse_client=clickhouse_client,
        clickhouse_database=clickhouse_database,
        candidate_deployment_ids=test_case.candidate_deployment_ids,
        active_view_target_deployment_id=test_case.active_view_target_deployment_id,
        invalid_active_view_target_name=test_case.invalid_active_view_target_name,
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
        result: DoctorResult = execute_doctor(
            request=DoctorRequest(default_database=clickhouse_database),
            client=managed_client,
        )
    finally:
        managed_client.close()

    assert len(result.active_views) == test_case.expected_status_count
    status: ActiveViewStatus = result.active_views[0]
    assert status.table_name == "tbl__orders_enriched"
    assert status.state_kind == test_case.expected_state_kind
    assert status.active_deployment_id == test_case.expected_active_deployment_id
    assert status.candidate_deployment_ids == test_case.expected_candidate_deployment_ids
