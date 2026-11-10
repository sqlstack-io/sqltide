from __future__ import annotations

import builtins
import shutil
from functools import partial
from pathlib import Path

import pytest
from _pytest.capture import CaptureResult
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from sqltide._native import build_compiler_adapter_profile, run_build
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.build.models import BuildCommandOptions
from sqltide.executor.workflow.models import PublishedBuildWorkflow
from tests.integration.src.sqltide.cli._test_types import (
    CliVirtualBuildIntegrationTestCase,
    CliVirtualConfirmationRaceIntegrationTestCase,
    CliVirtualManualFanInIntegrationTestCase,
)
from tests.integration.src.sqltide.cli.helpers import (
    BACKFILL_PIPELINES_ROOT,
    SELECTOR_PIPELINES_ROOT,
    build_managed_clickhouse_client,
    confirm_with_conflicting_candidate,
    ensure_backfill_metadata_tables,
    execute_clickhouse_client_sql,
    load_deployment_status_rows,
    load_selected_root_names,
    prepare_virtual_fan_in_source,
    publish_virtual_workflow,
    run_virtual_environment_build,
    virtual_deployment_metadata_row_count,
    virtual_deployment_watermark_rows,
    virtual_fan_in_delta_rows,
    write_virtual_fan_in_project,
)
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliVirtualBuildIntegrationTestCase(
            description="rejects json virtual build without auto approve",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=(),
            full_refresh=False,
            start_time=None,
            json_output=True,
            verbose=False,
            auto_approve=False,
            prompt_response="",
            expected_exit_code=1,
            expected_output_fragments=(),
            expected_error_fragments=("--json requires --auto-approve for build",),
            expected_deployment_status_rows=(),
            expected_absent_output_fragments=("Plan Ready",),
        ),
        CliVirtualBuildIntegrationTestCase(
            description="cancels interactive virtual build when prompt is declined",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=(),
            full_refresh=False,
            start_time=None,
            json_output=False,
            verbose=False,
            auto_approve=False,
            prompt_response="n",
            expected_exit_code=1,
            expected_output_fragments=("Plan Ready", "Build cancelled."),
            expected_error_fragments=(),
            expected_deployment_status_rows=(),
        ),
        CliVirtualBuildIntegrationTestCase(
            description=(
                "executes json virtual build with auto approve without printing plan preview"
            ),
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=(),
            full_refresh=False,
            start_time=None,
            json_output=True,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=0,
            expected_output_fragments=(
                '"deployment_id":',
                '"boundary_time":',
                '"root_reports":',
                '"state_kind": "greenfield"',
                '"replay_strategy": "create_from_scratch"',
            ),
            expected_error_fragments=(),
            expected_deployment_status_rows=(("backfilling",),),
            expected_selected_root_names=("tbl__orders_enriched",),
            expected_absent_output_fragments=("Plan Ready",),
        ),
        CliVirtualBuildIntegrationTestCase(
            description="rejects full refresh without selectors",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=(),
            full_refresh=True,
            start_time=None,
            json_output=False,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=1,
            expected_output_fragments=(),
            expected_error_fragments=(
                "--full-refresh requires --changed or at least one --select",
            ),
            expected_deployment_status_rows=(),
        ),
        CliVirtualBuildIntegrationTestCase(
            description="rejects start time for roots without an active published view",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=("orders_enriched",),
            full_refresh=False,
            start_time="2026-04-01T00:00:00Z",
            json_output=False,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=1,
            expected_output_fragments=(),
            expected_error_fragments=("--start-time requires an active published root",),
            expected_deployment_status_rows=(),
        ),
        CliVirtualBuildIntegrationTestCase(
            description="rejects combining full refresh with start time",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=("orders_enriched",),
            full_refresh=True,
            start_time="2026-04-01T00:00:00Z",
            json_output=False,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=1,
            expected_output_fragments=(),
            expected_error_fragments=("--full-refresh cannot be combined with --start-time",),
            expected_deployment_status_rows=(),
        ),
        CliVirtualBuildIntegrationTestCase(
            description="rejects an invalid user deployment id before confirmation",
            pipelines_root=BACKFILL_PIPELINES_ROOT,
            selectors=(),
            full_refresh=False,
            start_time=None,
            json_output=False,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=1,
            expected_output_fragments=(),
            expected_error_fragments=(
                "Deployment ID must match YYYYMMDDTHHMMSSZ_<alphanumeric-suffix>",
            ),
            expected_deployment_status_rows=(),
            deployment_id="release_2026_08",
        ),
        CliVirtualBuildIntegrationTestCase(
            description="executes selected full refresh for one authored model subtree",
            pipelines_root=SELECTOR_PIPELINES_ROOT,
            selectors=("orders_clean",),
            full_refresh=True,
            start_time=None,
            json_output=True,
            verbose=False,
            auto_approve=True,
            prompt_response="",
            expected_exit_code=0,
            expected_output_fragments=(
                '"deployment_id":',
                '"root_reports":',
            ),
            expected_error_fragments=(),
            expected_deployment_status_rows=(("backfilling",),),
            expected_selected_root_names=("tbl__orders_clean",),
            expected_absent_output_fragments=("Plan Ready",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_build_command_when_running_then_it_behaves_as_expected(
    test_case: CliVirtualBuildIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root: Path = tmp_path / "project"
    _ = shutil.copytree(test_case.pipelines_root.parent, project_root)
    pipelines_root: Path = project_root / "pipelines"
    managed_client: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    ensure_backfill_metadata_tables(
        managed_client=managed_client,
        clickhouse_client=clickhouse_client,
        database=clickhouse_database,
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: test_case.prompt_response)

    try:
        exit_code: int = run_build(
            options=BuildCommandOptions(
                pipelines_root=pipelines_root,
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                selectors=test_case.selectors,
                deployment_id=test_case.deployment_id,
                full_refresh=test_case.full_refresh,
                start_time=test_case.start_time,
                json_output=test_case.json_output,
                verbose=test_case.verbose,
                auto_approve=test_case.auto_approve,
            ),
            client=managed_client,
            observation_client=managed_client,
            loaded_project=_native.load_project_input_for_path(path=project_root),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
    finally:
        managed_client.close()

    captured: CaptureResult[str] = capsys.readouterr()
    deployment_status_rows: tuple[tuple[str, ...], ...] = load_deployment_status_rows(
        clickhouse_client=clickhouse_client, database=clickhouse_database
    )
    selected_root_names: tuple[str, ...] = load_selected_root_names(
        clickhouse_client=clickhouse_client, database=clickhouse_database
    )

    assert exit_code == test_case.expected_exit_code
    expected_output_fragment: str
    for expected_output_fragment in test_case.expected_output_fragments:
        assert expected_output_fragment in captured.out
    expected_error_fragment: str
    for expected_error_fragment in test_case.expected_error_fragments:
        assert expected_error_fragment in captured.err
    assert deployment_status_rows == test_case.expected_deployment_status_rows
    assert selected_root_names == test_case.expected_selected_root_names
    absent_output_fragment: str
    for absent_output_fragment in test_case.expected_absent_output_fragments:
        assert absent_output_fragment not in captured.out


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliVirtualManualFanInIntegrationTestCase(
            description=(
                "virtual fan-in command numbered steps and combined workflow preserve rows and "
                "watermarks"
            ),
            deployment_ids=(
                "20260802T120000Z_fanincommand",
                "20260802T120100Z_faninsteps",
                "20260802T120200Z_fanincombined",
            ),
            expected_exit_code=0,
            expected_delta_rows=(
                ("order-1", "order-1-gamma"),
                ("order-2", "order-2-gamma"),
            ),
            expected_watermark_rows=(
                ("tbl__alpha", "_replay_partition=0", "2"),
                ("tbl__delta", "_replay_partition=0", "2"),
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_fan_in_when_executing_artifacts_then_all_forms_match(
    test_case: CliVirtualManualFanInIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    numbered_database: str = f"{clickhouse_database}_virtual_fanin_steps"
    combined_database: str = f"{clickhouse_database}_virtual_fanin_combined"
    databases: tuple[str, ...] = (
        clickhouse_database,
        numbered_database,
        combined_database,
    )
    write_virtual_fan_in_project(project_root=tmp_path)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    try:
        clickhouse_client.command(f"CREATE DATABASE {numbered_database}")
        clickhouse_client.command(f"CREATE DATABASE {combined_database}")
        database: str
        for database in databases:
            prepare_virtual_fan_in_source(clickhouse_client=clickhouse_client, database=database)
        command_exit_code: int = run_virtual_environment_build(
            project_root=tmp_path,
            database=databases[0],
            deployment_id=test_case.deployment_ids[0],
            connection=connection,
        )
        command_capture: CaptureResult[str] = capsys.readouterr()
        numbered: PublishedBuildWorkflow = publish_virtual_workflow(
            project_root=tmp_path,
            database=databases[1],
            deployment_id=test_case.deployment_ids[1],
            connection=connection,
        )
        numbered_results: tuple[tuple[int, str], ...] = tuple(
            execute_clickhouse_client_sql(
                settings=clickhouse_connection_settings,
                sql=path.read_text(encoding="utf-8"),
            )
            for path in sorted((numbered.artifact_root / "steps").iterdir())
        )
        combined: PublishedBuildWorkflow = publish_virtual_workflow(
            project_root=tmp_path,
            database=databases[2],
            deployment_id=test_case.deployment_ids[2],
            connection=connection,
        )
        combined_sql: str = (combined.artifact_root / "workflow.sql").read_text(encoding="utf-8")
        combined_result: tuple[int, str] = execute_clickhouse_client_sql(
            settings=clickhouse_connection_settings,
            sql=combined_sql,
        )
        delta_rows: tuple[tuple[tuple[str, str], ...], ...] = tuple(
            virtual_fan_in_delta_rows(
                clickhouse_client=clickhouse_client,
                database=database,
                deployment_id=deployment_id,
            )
            for database, deployment_id in zip(databases, test_case.deployment_ids, strict=True)
        )
        watermark_rows: tuple[tuple[tuple[str, str, str], ...], ...] = tuple(
            virtual_deployment_watermark_rows(
                clickhouse_client=clickhouse_client,
                database=database,
                deployment_id=deployment_id,
            )
            for database, deployment_id in zip(databases, test_case.deployment_ids, strict=True)
        )
    finally:
        clickhouse_client.command(f"DROP DATABASE IF EXISTS {numbered_database} SYNC")
        clickhouse_client.command(f"DROP DATABASE IF EXISTS {combined_database} SYNC")
        connection.close()

    assert command_exit_code == test_case.expected_exit_code, command_capture.err
    assert tuple(result[0] for result in numbered_results) == tuple(
        test_case.expected_exit_code for _result in numbered_results
    )
    assert combined_result[0] == test_case.expected_exit_code
    assert combined_sql == "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((combined.artifact_root / "steps").iterdir())
    )
    assert delta_rows == tuple(test_case.expected_delta_rows for _database in databases)
    assert watermark_rows == tuple(test_case.expected_watermark_rows for _database in databases)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        CliVirtualConfirmationRaceIntegrationTestCase(
            description="candidate relation appearing during confirmation aborts before metadata",
            deployment_id="20260802T121000Z_confirmationrace",
            expected_exit_code=1,
            expected_error_fragment="Candidate relation appeared after virtual confirmation",
            expected_metadata_row_count=0,
            expected_absent_error_fragment="Traceback",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_candidate_relation_created_during_confirmation_when_building_then_it_aborts_cleanly(
    test_case: CliVirtualConfirmationRaceIntegrationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root: Path = tmp_path / "project"
    _ = shutil.copytree(BACKFILL_PIPELINES_ROOT.parent, project_root)
    connection: AdapterConnection = build_managed_clickhouse_client(
        clickhouse_connection_settings,
        database=clickhouse_database,
    )
    ensure_backfill_metadata_tables(
        managed_client=connection,
        clickhouse_client=clickhouse_client,
        database=clickhouse_database,
    )
    candidate_relation_name: str = f"tbl__orders_enriched__{test_case.deployment_id}"
    monkeypatch.setattr(
        builtins,
        "input",
        partial(
            confirm_with_conflicting_candidate,
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            relation_name=candidate_relation_name,
        ),
    )

    try:
        exit_code: int = run_build(
            options=BuildCommandOptions(
                pipelines_root=project_root / "pipelines",
                database=clickhouse_database,
                metadata_database=clickhouse_database,
                selectors=(),
                deployment_id=test_case.deployment_id,
                full_refresh=False,
                start_time=None,
                json_output=False,
                verbose=False,
                auto_approve=False,
            ),
            client=connection,
            observation_client=connection,
            loaded_project=_native.load_project_input_for_path(path=project_root),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )
        captured: CaptureResult[str] = capsys.readouterr()
        metadata_row_count: int = virtual_deployment_metadata_row_count(
            clickhouse_client=clickhouse_client,
            database=clickhouse_database,
            deployment_id=test_case.deployment_id,
        )
    finally:
        connection.close()

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_error_fragment in captured.err
    assert metadata_row_count == test_case.expected_metadata_row_count
    assert test_case.expected_absent_error_fragment not in captured.err
