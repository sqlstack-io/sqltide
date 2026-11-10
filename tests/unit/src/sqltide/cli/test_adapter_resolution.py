from __future__ import annotations

import re
from pathlib import Path
from shutil import copytree
from typing import cast
from unittest.mock import Mock

import pytest

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry.main.main import _main_with_dependencies
from sqltide.cli.entry.models import CliConnectionOptions, CliEntrypointHandlers
from sqltide.compiler.discovery.models import LoadedProject
from tests.unit.src.sqltide.cli._test_types import (
    CliAdapterPlanExecutionTestCase,
    CliAdapterRejectionTestCase,
    CliCredentialRedactionTestCase,
    CliLazyConnectionTestCase,
    CliModeGateErrorTestCase,
    CliModeOverrideTestCase,
    CliProjectSecretRedactionTestCase,
    CliTargetSelectionTestCase,
)
from tests.unit.src.sqltide.cli.helpers import (
    AdapterConnectionProvider,
    FakeCliClickHouseClient,
    RecordingAdapterConnection,
    handlers_with_overrides,
    write_cli_compilation_project,
    write_cli_target_project,
)

_DIRECT_PROJECT_CONFIG: str = (
    'name = "mode_gate"\ndefault_target = "test"\n\n[targets.test]\ndatabase = "analytics"\n'
)
_VIRTUAL_ENVIRONMENT_PROJECT_CONFIG: str = (
    'name = "mode_gate"\ndefault_target = "test"\n\n'
    '[defaults]\npipeline_mode = "virtual"\n\n'
    '[targets.test]\ndatabase = "analytics"\n'
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliAdapterRejectionTestCase(
            description="rejects an unknown adapter before resolving credentials or connecting",
            project_file_contents=(
                'name = "selector_project"\nadapter = "duckdb"\n'
                'default_target = "test"\n[targets.test]\ndatabase = "analytics"\n'
            ),
            argv=("sqt", "plan"),
            expected_exit_code=1,
            expected_error_fragments=(
                "Unsupported adapter 'duckdb'",
                "Supported adapters: clickhouse.",
            ),
            expected_absent_error_fragments=("Missing host", "Traceback"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unknown_project_adapter_when_running_plan_then_it_fails_before_connecting(
    test_case: CliAdapterRejectionTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/selector_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        test_case.project_file_contents, encoding="utf-8"
    )
    handlers: CliEntrypointHandlers = handlers_with_overrides()

    exit_code: int = _main_with_dependencies(
        argv=test_case.argv,
        handlers=handlers,
        environment={},
        working_directory=project_dir,
    )
    captured_error: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    expected_fragment: str
    for expected_fragment in test_case.expected_error_fragments:
        assert expected_fragment in captured_error
    absent_fragment: str
    for absent_fragment in test_case.expected_absent_error_fragments:
        assert absent_fragment not in captured_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliAdapterPlanExecutionTestCase(
            description=("runs plan through the resolved adapter with mixed connection precedence"),
            project_file_contents="""
name = "selector_project"
adapter = "clickhouse"
default_target = "test"

[defaults]
pipeline_mode = \"virtual\"

[connection]
host = "project-host"
port = 8123
username = "project-user"
password = "project-secret"

[targets.test]
database = "analytics"

[targets.test.destruction]
max_table_size_to_drop = "100GiB"
""".lstrip(),
            argv=("sqt", "plan", "--host", "cli-host"),
            environment={
                "SQLTIDE_CLICKHOUSE_PORT": "9000",
                "SQLTIDE_CLICKHOUSE_USERNAME": "env-user",
            },
            expected_exit_code=0,
            expected_connection=("cli-host", 9000, "env-user", "project-secret"),
            expected_catalog_load_count=1,
            expected_query_count=1,
            expected_connection_closed=True,
            expected_stdout="""Plan Ready
Database: analytics
Subtrees to rebuild: 2
Planned steps: 12

Subtrees:
Subtree 1
[replay start] raw__orders
\u251c\u2500\u2500 [live target] tbl__orders_clean
\u2514\u2500\u2500 [live target] tbl__orders_enriched

Subtree 2
[replay start] raw__payments
\u2514\u2500\u2500 [live target] tbl__payments_enriched

New targets:
- tbl__orders_clean
- tbl__orders_enriched
- tbl__payments_enriched

Diffs:
- kafka__orders
- kafka__payments
- mv__orders
- tbl__orders_clean
- tbl__orders_enriched
- mv__payments
- tbl__payments_enriched
- raw__orders
- raw__payments
Run `sqt plan --verbose` to show full diffs

Warnings:
- none
""",
            expected_redacted_secret="project-secret",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_versioned_project_when_running_plan_then_adapter_preserves_exact_output_and_closes(
    test_case: CliAdapterPlanExecutionTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/selector_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        test_case.project_file_contents, encoding="utf-8"
    )
    connection: RecordingAdapterConnection = RecordingAdapterConnection()
    provider: AdapterConnectionProvider = AdapterConnectionProvider(connection)
    monkeypatch.setattr(ClickHouseAdapter, "connect", provider)
    handlers: CliEntrypointHandlers = handlers_with_overrides()

    exit_code: int = _main_with_dependencies(
        argv=test_case.argv,
        handlers=handlers,
        environment=test_case.environment,
        working_directory=project_dir,
    )
    captured_stdout: str = capsys.readouterr().out

    assert exit_code == test_case.expected_exit_code
    assert provider.config is not None
    assert (
        provider.config.host,
        provider.config.port,
        provider.config.username,
        provider.config.password,
    ) == test_case.expected_connection
    assert len(connection.catalog_databases) == test_case.expected_catalog_load_count
    assert len(connection.statements) == test_case.expected_query_count
    assert connection.closed is test_case.expected_connection_closed
    assert captured_stdout == test_case.expected_stdout
    assert test_case.expected_redacted_secret not in repr(provider.config)
    assert provider.config.settings == ()


@pytest.mark.parametrize(
    "test_case",
    [
        CliCredentialRedactionTestCase(
            description="does not expose resolved CLI credentials through repr",
            password="cli-secret",
            expected_absent_fragment="cli-secret",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cli_connection_options_when_rendering_then_credentials_are_not_exposed(
    test_case: CliCredentialRedactionTestCase,
) -> None:
    options: CliConnectionOptions = CliConnectionOptions(
        host="localhost",
        port=8123,
        username="sqltide",
        password=test_case.password,
        project_connection=AdapterConnectionConfig(
            host="localhost",
            port=8123,
            username="sqltide",
            password=test_case.password,
        ),
    )

    assert test_case.expected_absent_fragment not in repr(options)


@pytest.mark.parametrize(
    "test_case",
    [
        CliModeGateErrorTestCase(
            description="rejects deployment audit when virtual environments are omitted",
            argv=("sqt", "deployment", "audit", "deployment-id"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment=(
                "sqt deployment audit requires virtual environments to be enabled"
            ),
        ),
        CliModeGateErrorTestCase(
            description="rejects deployment promotion when virtual environments are omitted",
            argv=("sqt", "deployment", "promote", "deployment-id"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment=(
                "sqt deployment promote requires virtual environments to be enabled"
            ),
        ),
        CliModeGateErrorTestCase(
            description="rejects reconcile when virtual environments are omitted",
            argv=("sqt", "reconcile"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment="sqt reconcile requires virtual environments to be enabled",
        ),
        CliModeGateErrorTestCase(
            description="rejects janitor when virtual environments are omitted",
            argv=("sqt", "janitor"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment="sqt janitor requires virtual environments to be enabled",
        ),
        CliModeGateErrorTestCase(
            description="rejects doctor when virtual environments are omitted",
            argv=("sqt", "doctor"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment="sqt doctor requires virtual environments to be enabled",
        ),
        CliModeGateErrorTestCase(
            description="rejects repair when virtual environments are omitted",
            argv=(
                "sqt",
                "repair",
                "active-view",
                "--table",
                "tbl__orders",
                "--deployment-id",
                "dep_a",
            ),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            expected_error_fragment=(
                "sqt repair active-view requires virtual environments to be enabled"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_mode_specific_command_when_mode_conflicts_then_it_fails_before_connecting(
    test_case: CliModeGateErrorTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/basic_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        test_case.project_file_contents,
        encoding="utf-8",
    )
    connect: Mock = Mock(side_effect=AssertionError("connection IO occurred"))
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)

    exit_code: int = _main_with_dependencies(
        argv=test_case.argv,
        handlers=handlers_with_overrides(),
        environment={},
        working_directory=project_dir,
    )
    captured_error: str = capsys.readouterr().err

    assert exit_code == 1
    assert connect.call_count == 0
    assert test_case.expected_error_fragment in captured_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliModeOverrideTestCase(
            description="local override enables virtual environment lifecycle",
            argv=("sqt", "build"),
            project_file_contents=_DIRECT_PROJECT_CONFIG,
            local_file_contents='[defaults]\npipeline_mode = "virtual"\n',
            expected_handler_name="run_build",
            expected_exit_code=0,
            expected_handler_call_count=1,
        ),
        CliModeOverrideTestCase(
            description="local override enables direct build",
            argv=("sqt", "build"),
            project_file_contents=_VIRTUAL_ENVIRONMENT_PROJECT_CONFIG,
            local_file_contents='[defaults]\npipeline_mode = "direct"\n',
            expected_handler_name="run_build",
            expected_exit_code=0,
            expected_handler_call_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_local_mode_override_when_running_specific_command_then_effective_mode_routes_it(
    test_case: CliModeOverrideTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/basic_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        test_case.project_file_contents,
        encoding="utf-8",
    )
    (project_dir / "sqltide_local.toml").write_text(
        test_case.local_file_contents,
        encoding="utf-8",
    )
    runner: Mock = Mock(return_value=0)
    handlers: CliEntrypointHandlers = handlers_with_overrides(
        **{test_case.expected_handler_name: runner}
    )

    exit_code: int = _main_with_dependencies(
        argv=test_case.argv,
        handlers=handlers,
        environment={},
        working_directory=project_dir,
        adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    assert exit_code == test_case.expected_exit_code
    assert runner.call_count == test_case.expected_handler_call_count


@pytest.mark.parametrize(
    "test_case",
    [
        CliLazyConnectionTestCase(
            description="offline compile defers a missing password but plan rejects it before IO",
            project_vars_contents="",
            secret_template="${ENV:OFFLINE_PASSWORD}",
            expected_compile_exit_code=0,
            expected_plan_exit_code=1,
            expected_connect_count=0,
            expected_error_fragment="connection.password references missing environment variable",
            expected_absent_fragment="LOWER_PRIORITY_PASSWORD",
        ),
        CliLazyConnectionTestCase(
            description="offline compile defers a variable-indirected missing password",
            project_vars_contents=('[vars]\nwarehouse_password = "${ENV:OFFLINE_PASSWORD}"\n\n'),
            secret_template="${warehouse_password}",
            expected_compile_exit_code=0,
            expected_plan_exit_code=1,
            expected_connect_count=0,
            expected_error_fragment=(
                "connection.password vars.warehouse_password references missing "
                "environment variable"
            ),
            expected_absent_fragment="LOWER_PRIORITY_PASSWORD",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_lazy_connection_secret_when_compiling_then_only_connecting_command_expands_it(
    test_case: CliLazyConnectionTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = tmp_path / "artifacts"
    copytree(Path("tests/fixtures/selector_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "selector_project"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        f"{test_case.project_vars_contents}"
        '[connection]\npassword = "${ENV:LOWER_PRIORITY_PASSWORD}"\n\n'
        '[targets.test]\ndatabase = "analytics"\n\n'
        '[targets.test.connection]\nhost = "localhost"\nport = 8123\n'
        f'username = "sqltide"\npassword = "{test_case.secret_template}"\n',
        encoding="utf-8",
    )
    connect: Mock = Mock(side_effect=AssertionError("connection IO occurred"))
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)
    handlers: CliEntrypointHandlers = handlers_with_overrides()

    compile_exit_code: int = _main_with_dependencies(
        argv=("sqt", "compile", "--target-dir", str(target_dir)),
        handlers=handlers,
        environment={},
        working_directory=project_dir,
    )
    compile_output: str = capsys.readouterr().out
    plan_exit_code: int = _main_with_dependencies(
        argv=("sqt", "plan"),
        handlers=handlers,
        environment={},
        working_directory=project_dir,
    )
    plan_error: str = capsys.readouterr().err
    artifact_contents: tuple[str, ...] = tuple(
        path.read_text(encoding="utf-8") for path in sorted(target_dir.rglob("*.*"))
    )

    assert compile_exit_code == test_case.expected_compile_exit_code
    assert plan_exit_code == test_case.expected_plan_exit_code
    assert connect.call_count == test_case.expected_connect_count
    assert test_case.expected_error_fragment in plan_error
    assert test_case.expected_absent_fragment not in compile_output
    assert test_case.expected_absent_fragment not in plan_error
    assert all(test_case.expected_absent_fragment not in contents for contents in artifact_contents)


@pytest.mark.parametrize(
    "test_case",
    [
        CliProjectSecretRedactionTestCase(
            description="redacts a resolved project secret from repr errors and compile artifacts",
            secret="resolved-project-secret",
            expected_compile_exit_code=0,
            expected_plan_exit_code=1,
            expected_error_fragment="sqltide_project.toml.*connection.*warehouse",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_resolved_project_secret_when_rendering_surfaces_then_it_is_never_exposed(
    test_case: CliProjectSecretRedactionTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = tmp_path / "artifacts"
    write_cli_compilation_project(
        project_root=project_dir,
        model_sql="""
        MODEL (order_by ["order_id"]);
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
    )
    (project_dir / "sqltide_project.toml").write_text(
        'name = "redaction_project"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        f'[vars]\nwarehouse_password = "{test_case.secret}"\n\n'
        '[connection]\nhost = "localhost"\nport = 8123\n'
        'username = "sqltide"\npassword = "${warehouse_password}"\nwarehouse = "bad"\n\n'
        '[targets.test]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(path=project_dir)
    connect: Mock = Mock(side_effect=AssertionError("connection IO occurred"))
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)
    handlers: CliEntrypointHandlers = handlers_with_overrides()

    compile_exit_code: int = _main_with_dependencies(
        argv=("sqt", "compile", "--target-dir", str(target_dir)),
        handlers=handlers,
        environment={},
        working_directory=project_dir,
    )
    compile_output: str = capsys.readouterr().out
    plan_exit_code: int = _main_with_dependencies(
        argv=("sqt", "plan"),
        handlers=handlers,
        environment={},
        working_directory=project_dir,
    )
    plan_error: str = capsys.readouterr().err
    artifact_contents: tuple[str, ...] = tuple(
        path.read_text(encoding="utf-8") for path in sorted(target_dir.rglob("*.*"))
    )
    assert loaded_project is not None
    rendered_debug_values: tuple[str, ...] = (
        repr(loaded_project),
        repr(loaded_project.configuration),
        repr(loaded_project.effective_configuration),
    )

    assert compile_exit_code == test_case.expected_compile_exit_code
    assert plan_exit_code == test_case.expected_plan_exit_code
    assert connect.call_count == 0
    assert re.search(test_case.expected_error_fragment, plan_error) is not None
    assert test_case.secret not in compile_output
    assert test_case.secret not in plan_error
    assert all(test_case.secret not in contents for contents in artifact_contents)
    assert all(test_case.secret not in rendered for rendered in rendered_debug_values)


@pytest.mark.parametrize(
    "test_case",
    [
        CliTargetSelectionTestCase(
            description="nested compile uses the project default target",
            argv_suffix=(),
            local_contents="",
            expected_database_fragment='"resolved_database": "dev_database"',
        ),
        CliTargetSelectionTestCase(
            description="nested compile uses a local target and local variable",
            argv_suffix=(),
            local_contents="""
            target = "private"
            [targets.private.vars]
            database_name = "local_database"
            """,
            expected_database_fragment='"resolved_database": "local_database"',
        ),
        CliTargetSelectionTestCase(
            description="CLI target and vars override local and project selection",
            argv_suffix=(
                "--target",
                "private",
                "--vars",
                '{"database_name": "cli_database"}',
            ),
            local_contents="""
            target = "dev"
            [vars]
            database_name = "local_database"
            """,
            expected_database_fragment='"resolved_database": "cli_database"',
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_target_inputs_when_nested_compile_then_precedence_applies(
    test_case: CliTargetSelectionTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = tmp_path / "target"
    write_cli_target_project(
        project_root=project_dir,
        local_contents=test_case.local_contents,
    )

    exit_code: int = _main_with_dependencies(
        argv=("sqt", "compile", "--target-dir", str(target_dir), *test_case.argv_suffix),
        handlers=handlers_with_overrides(),
        environment={},
        working_directory=project_dir / "pipelines" / "pl__orders",
    )
    manifest: str = (target_dir / "manifest.json").read_text(encoding="utf-8")

    assert exit_code == 0
    assert test_case.expected_database_fragment in manifest
