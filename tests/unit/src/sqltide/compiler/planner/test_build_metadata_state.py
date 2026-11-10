import pytest

from sqltide._native import build_metadata_state
from sqltide.compiler.planner.models import DeploymentRecord, MetadataState
from tests.unit.src.sqltide.compiler.planner._test_types import (
    BuildMetadataStateTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import build_metadata_records


@pytest.mark.parametrize(
    "test_case",
    [
        BuildMetadataStateTestCase(
            description="builds deterministically ordered metadata state records",
            expected_object_state_keys=(
                (None, "materialized_view", "mv__orders_enriched"),
                (None, "table", "tbl__orders_enriched"),
            ),
            expected_deployment_ids=("20260408T120000Z_ab12cd", "20260408T130000Z_cd34ef"),
            expected_first_deployment_root_keys=((None, "table", "raw__orders"),),
            expected_first_deployment_warning_codes=(),
            expected_first_deployment_mapping_names=(),
            expected_watermark_boundary_keys=("partition:0", "partition:1"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unsorted_metadata_records_when_building_then_it_returns_normalized_state(
    test_case: BuildMetadataStateTestCase,
) -> None:
    (
        object_states,
        deployments,
        deployment_watermarks,
        publish_events,
    ) = build_metadata_records()

    metadata_state: MetadataState = build_metadata_state(
        object_states=object_states,
        deployments=deployments,
        deployment_watermarks=deployment_watermarks,
        publish_events=publish_events,
    )
    first_deployment: DeploymentRecord = metadata_state.deployments[0]
    second_deployment: DeploymentRecord = metadata_state.deployments[1]

    assert (
        tuple(
            (record.key.database, record.key.object_type, record.key.name)
            for record in metadata_state.object_states
        )
        == test_case.expected_object_state_keys
    )
    assert (
        tuple(record.deployment_id for record in metadata_state.deployments)
        == test_case.expected_deployment_ids
    )
    assert (
        tuple(
            (key.database, key.object_type, key.name) for key in first_deployment.selected_root_keys
        )
        == test_case.expected_first_deployment_root_keys
    )
    assert first_deployment.warning_codes == test_case.expected_first_deployment_warning_codes
    assert (
        tuple(mapping.logical_key.name for mapping in first_deployment.prepared_object_mappings)
        == test_case.expected_first_deployment_mapping_names
    )
    assert (
        tuple(watermark.boundary_key for watermark in metadata_state.deployment_watermarks)
        == test_case.expected_watermark_boundary_keys
    )
    assert second_deployment.warning_codes == ("a_warning", "z_warning")
    assert tuple(
        mapping.logical_key.name for mapping in second_deployment.prepared_object_mappings
    ) == ("mv__orders_enriched", "tbl__orders_enriched")
