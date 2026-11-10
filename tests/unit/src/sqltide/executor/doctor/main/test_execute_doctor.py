import pytest

from sqltide._native import execute_doctor
from sqltide.adapter.models import CatalogRelation
from sqltide.executor.doctor.models import ActiveViewStatus, DoctorRequest, DoctorResult
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.doctor.main._test_types import (
    DoctorCatalogInspectionTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DoctorCatalogInspectionTestCase(
            description="classifies managed bindings from one neutral catalog snapshot",
            relations=(
                CatalogRelation(
                    name="tbl__orders_enriched",
                    engine="View",
                    columns=(),
                    stable_binding_name=("tbl__orders_enriched__20260731T120000Z_depaaa"),
                ),
                CatalogRelation(
                    name="tbl__orders_enriched__20260731T120000Z_depaaa",
                    engine="MergeTree",
                    columns=(),
                ),
                CatalogRelation(
                    name="tbl__orders_enriched__20260731T130000Z_depbbb",
                    engine="MergeTree",
                    columns=(),
                ),
            ),
            expected_catalog_databases=("analytics",),
            expected_result=DoctorResult(
                active_views=(
                    ActiveViewStatus(
                        table_name="tbl__orders_enriched",
                        state_kind="active_view_present",
                        active_deployment_id="20260731T120000Z_depaaa",
                        candidate_deployment_ids=(
                            "20260731T120000Z_depaaa",
                            "20260731T130000Z_depbbb",
                        ),
                    ),
                )
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_catalog_state_when_doctoring_then_it_classifies_without_extra_inspection(
    test_case: DoctorCatalogInspectionTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        relations=test_case.relations
    )

    result: DoctorResult = execute_doctor(
        request=DoctorRequest(default_database="analytics"),
        client=connection,
    )

    assert tuple(connection.catalog_databases) == test_case.expected_catalog_databases
    assert result == test_case.expected_result
