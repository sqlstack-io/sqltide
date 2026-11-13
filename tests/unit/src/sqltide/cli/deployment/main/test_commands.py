from pathlib import Path

import pytest

from sqltide._native import run_cli, run_deployment_list, run_deployment_show
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterMetadataObjectKey,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    CatalogRelation,
)
from tests.unit.src.sqltide.cli.deployment.main._test_types import (
    DeploymentPromoteLatestTestCase,
    DeploymentReadCommandTestCase,
)
from tests.unit.src.sqltide.cli.helpers import (
    RecordingAdapterConnection,
    handlers_with_overrides,
    write_cli_compilation_project,
)

_OLD: tuple[str, str] = ("20260806T000100Z_old", "2026-08-06 00:01:00.000")
_NEW: tuple[str, str] = ("20260806T000200Z_new", "2026-08-06 00:02:00.000")


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentReadCommandTestCase(
            description="lists authoritative inventory without warehouse mutations",
            expected_exit_code=0,
            expected_output_fragment='"deployments": []',
        )
    ],
    ids=lambda case: case.description,
)
def test_given_inventory_when_listing_deployments_then_command_is_read_only(
    test_case: DeploymentReadCommandTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client: RecordingAdapterConnection = RecordingAdapterConnection()

    exit_code: int = run_deployment_list(
        database="analytics",
        metadata_database="metadata",
        json_output=True,
        client=client,
    )

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_output_fragment in capsys.readouterr().out
    assert client.workflow_mutation_statements == []


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentReadCommandTestCase(
            description="shows authoritative deployment without warehouse mutations",
            expected_exit_code=0,
            expected_output_fragment='"deployment_id": "20260806T000100Z_staged"',
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_when_showing_deployment_then_command_is_read_only(
    test_case: DeploymentReadCommandTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client: RecordingAdapterConnection = RecordingAdapterConnection(
        deployment_inventory=AdapterDeploymentInventory(
            deployments=(
                AdapterDeploymentRecord(
                    deployment_id="20260806T000100Z_staged",
                    created_at="2026-08-06 00:01:00.000",
                    status="staged",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=(),
                ),
            ),
            publish_events=(),
        )
    )

    exit_code: int = run_deployment_show(
        deployment_id="20260806T000100Z_staged",
        database="analytics",
        metadata_database="metadata",
        json_output=True,
        client=client,
    )

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_output_fragment in capsys.readouterr().out
    assert client.workflow_mutation_statements == []


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentPromoteLatestTestCase(
            description="refuses when the newest deployment is already published",
            deployments=(_OLD, _NEW),
            published_deployment_ids=(_NEW[0],),
            expected_exit_code=1,
            expected_error_fragments=("20260806T000200Z_new", "already published"),
            expected_promoted_deployment_ids=(),
        ),
        DeploymentPromoteLatestTestCase(
            description="refuses and lists candidates when two are unpublished",
            deployments=(_OLD, _NEW),
            published_deployment_ids=(),
            expected_exit_code=1,
            expected_error_fragments=("20260806T000100Z_old", "20260806T000200Z_new"),
            expected_promoted_deployment_ids=(),
        ),
        DeploymentPromoteLatestTestCase(
            description="promotes the only unpublished deployment and names it",
            deployments=(_OLD, _NEW),
            published_deployment_ids=(_OLD[0],),
            expected_exit_code=0,
            expected_error_fragments=("Promoting deployment 20260806T000200Z_new",),
            expected_promoted_deployment_ids=(_NEW[0],),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_inventory_when_promoting_latest_then_only_a_single_new_candidate_is_promoted(
    test_case: DeploymentPromoteLatestTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_cli_compilation_project(
        project_root=tmp_path, model_sql='SELECT 1 AS id FROM __source("orders")'
    )
    client: RecordingAdapterConnection = RecordingAdapterConnection(
        relations=tuple(
            CatalogRelation(name=f"tbl__orders__{deployment_id}", engine="MergeTree", columns=())
            for deployment_id, _ in test_case.deployments
        ),
        deployment_inventory=AdapterDeploymentInventory(
            deployments=tuple(
                AdapterDeploymentRecord(
                    deployment_id=deployment_id,
                    created_at=created_at,
                    status="staged",
                    replay_lineage_mode="offsets",
                    selected_root_keys=(),
                    warning_codes=(),
                    prepared_object_mappings=(
                        AdapterPreparedObjectMapping(
                            logical_key=AdapterMetadataObjectKey(None, "table", "tbl__orders"),
                            physical_name=f"tbl__orders__{deployment_id}",
                            logical_model_name="orders",
                        ),
                    ),
                )
                for deployment_id, created_at in test_case.deployments
            ),
            publish_events=tuple(
                AdapterPublishEventRecord(
                    deployment_id=deployment_id,
                    published_at="2026-08-06 00:03:00.000",
                    logical_view_names=(),
                )
                for deployment_id in test_case.published_deployment_ids
            ),
        ),
    )
    promoted: list[object] = []

    def record_promotion(**options: object) -> int:
        promoted.append(options["deployment_id"])
        return 0

    exit_code: int = run_cli(
        ("sqt", "deployment", "promote", "--latest", "--project-dir", str(tmp_path)),
        handlers=handlers_with_overrides(run_deployment_promote=record_promotion),
        environment={},
        adapter_connection=client,
    )

    error: str = capsys.readouterr().err
    assert exit_code == test_case.expected_exit_code
    assert all(fragment in error for fragment in test_case.expected_error_fragments)
    assert tuple(promoted) == test_case.expected_promoted_deployment_ids
