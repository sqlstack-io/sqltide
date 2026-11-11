from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from shutil import copytree
from typing import cast
from unittest.mock import MagicMock

import pytest

from sqltide._native import (
    parse_cli_arguments,
    run_cli,
    run_dev,
    translate_clickhouse_driver_error,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterAuthenticationError, AdapterTimeoutError
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.build.models import BuildCommandOptions
from sqltide.cli.dev.models import DevCommandOptions
from sqltide.cli.entry.constants import DEV_CLI_VARIABLES_ENV_VAR
from sqltide.cli.entry.models import CliEntrypointHandlers
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.models import DevExecutionContext
from tests.unit.src.sqltide.cli._test_types import (
    CliAuditBackfillProjectContextTestCase,
    CliBuildObservationConnectionTestCase,
    CliBuildObservationFailureTestCase,
    CliCompileArtifactsTestCase,
    CliDevRefactorTestCase,
    CliJanitorApplyFlagTestCase,
    CliMainEnvResolutionTestCase,
    CliMainErrorTestCase,
    CliMainIntegrationTestCase,
    CliMainJsonFlagTestCase,
    CliMainJsonTestCase,
    CliNestedAuditOptionsTestCase,
    CliProjectConnectionResolutionTestCase,
    CliProjectDefaultsTestCase,
    CliReconcileForwardingTestCase,
    CliRequiredDeploymentIdTestCase,
    CliSelectorForwardingTestCase,
    CliUserErrorPresentationTestCase,
)
from tests.unit.src.sqltide.cli.helpers import (
    CLI_COMMAND_ARGV,
    CLI_COMMAND_HANDLER_NAMES,
    OUTPUT_NORMALIZERS,
    FakeCliClickHouseClient,
    handlers_with_overrides,
    normalize_json_output,
    refused_driver_error,
    write_cli_compilation_project,
    write_cli_target_project,
)


class PrintingCommandRunner:
    def __init__(self, output: str) -> None:
        self.output: str = output

    def __call__(self, *_args: object, **_kwargs: object) -> int:
        print(self.output)
        return 0


class RecordingCommandRunner:
    def __init__(self) -> None:
        self.pipelines_root: Path | None = None
        self.kwargs: dict[str, object] = {}

    def __call__(self, *_args: object, pipelines_root: Path | None = None, **kwargs: object) -> int:
        self.pipelines_root = pipelines_root
        self.kwargs.update(kwargs)
        return 0


class RecordingAuditBackfillCommandRunner:
    def __init__(self) -> None:
        self.pipelines_root: Path | None = None
        self.kwargs: dict[str, object] = {}

    def __call__(self, pipelines_root: Path | None, **kwargs: object) -> int:
        self.pipelines_root = pipelines_root
        self.kwargs.update(kwargs)
        return 0


class FailingCommandRunner:
    def __init__(self, error: Exception) -> None:
        self.error: Exception = error

    def __call__(self, *_args: object, **_kwargs: object) -> int:
        raise self.error


class PlanCommandRunner:
    def __call__(self, *_args: object, options: PlanCommandOptions, **_kwargs: object) -> int:
        if options.json_output:
            print(
                "{\n"
                '  "steps": ['
                '{"phase": "create", "target_key": {"name": "tbl__orders_enriched"}}],\n'
                '  "rebuild_subtrees": '
                '[{"upstream_boundary_key": {"name": "raw__orders"}}],\n'
                '  "warnings": []\n}'
            )
            return 0
        print(
            "Plan Ready\n"
            "Database: analytics\n"
            "Subtrees to rebuild: 1\n"
            "Planned steps: 1\n\n"
            "Subtrees:\n"
            "Subtree 1\n"
            "[replay start] raw__orders\n"
            "└── [live target] tbl__orders_enriched\n\n"
            "Prepared staged objects\n"
            "- table: tbl__orders_enriched\n\n"
            "Workflow\n"
            "- prepare staged objects for subtree rooted at raw__orders\n"
            "- backfill from raw__orders\n"
            "- audit staged tbl__orders_enriched\n"
            "- publish tbl__orders_enriched\n\n"
            "Warnings:\n"
            "- none"
        )
        return 0


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainJsonTestCase(
            description="prints discovered pipeline names as json",
            argv=("sqt", "discover", "--project-dir", "tests/fixtures/basic_project"),
            expected_exit_code=0,
            expected_output_fragments=("orders",),
        ),
        CliMainJsonTestCase(
            description="prints virtual build payload as json",
            argv=(
                "sqt",
                "build",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"deployment_id": "20260410T000000Z_ab12cd"',
                '"replay_strategy": "create_from_scratch"',
            ),
            handler_name="run_build",
            handler_output="{\n"
            '  "deployment_id": "20260410T000000Z_ab12cd",\n'
            '  "boundary_time": "2026-04-10 00:00:00.000",\n'
            '  "root_reports": [{"name": "tbl__orders_enriched", '
            '"replay_strategy": "create_from_scratch"}]\n'
            "}",
        ),
        CliMainJsonTestCase(
            description="prints deployment list payload as json",
            argv=(
                "sqt",
                "deployment",
                "list",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--database",
                "analytics",
                "--json",
            ),
            expected_exit_code=0,
            expected_output_fragments=('"deployments": []',),
            handler_name="run_deployment_list",
            handler_output='{"database": "analytics", "deployments": []}',
        ),
        CliMainJsonTestCase(
            description="prints deployment show payload as json",
            argv=(
                "sqt",
                "deployment",
                "show",
                "20260410T000000Z_ab12cd",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--database",
                "analytics",
                "--json",
            ),
            expected_exit_code=0,
            expected_output_fragments=('"deployment_id": "20260410T000000Z_ab12cd"',),
            handler_name="run_deployment_show",
            handler_output='{"deployment_id": "20260410T000000Z_ab12cd"}',
        ),
        CliMainJsonTestCase(
            description="prints deployment audit payload as json",
            argv=(
                "sqt",
                "deployment",
                "audit",
                "20260410T000000Z_ab12cd",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"deployment_status": "backfilling"',
                '"assessment": "ready"',
                '"name": "tbl__orders_enriched"',
            ),
            handler_name="run_deployment_audit",
            handler_output="{\n"
            '  "deployment_id": "20260410T000000Z_ab12cd",\n'
            '  "deployment_status": "backfilling",\n'
            '  "assessment": "ready",\n'
            '  "warning_codes": [],\n'
            '  "root_results": [{"name": "tbl__orders_enriched"}]\n'
            "}",
        ),
        CliMainJsonTestCase(
            description="prints live audit payload as json",
            argv=(
                "sqt",
                "audit",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"error_failure_count": 0',
                '"warning_failure_count": 1',
                '"severity": "warning"',
            ),
            handler_name="run_audit",
            handler_output="{\n"
            '  "error_failure_count": 0,\n'
            '  "warning_failure_count": 1,\n'
            '  "audit_results": [{"severity": "warning"}]\n'
            "}",
        ),
        CliMainJsonTestCase(
            description="prints publish payload as json",
            argv=(
                "sqt",
                "deployment",
                "promote",
                "20260410T000000Z_ab12cd",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"deployment_id": "20260410T000000Z_ab12cd"',
                '"view_name": "tbl__orders_enriched"',
                '"target_table_name": "tbl__orders_enriched__20260410T000000Z_ab12cd"',
            ),
            handler_name="run_deployment_promote",
            handler_output="{\n"
            '  "deployment_id": "20260410T000000Z_ab12cd",\n'
            '  "published_views": [{"view_name": "tbl__orders_enriched", '
            '"target_table_name": "tbl__orders_enriched__20260410T000000Z_ab12cd"}]\n'
            "}",
        ),
        CliMainJsonTestCase(
            description="prints doctor payload as json",
            argv=(
                "sqt",
                "doctor",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"table_name": "tbl__orders_enriched"',
                '"state_kind": "logical_view_missing"',
            ),
            handler_name="run_doctor",
            handler_output="{\n"
            '  "active_views": [{"table_name": "tbl__orders_enriched", '
            '"state_kind": "logical_view_missing"}]\n'
            "}",
        ),
        CliMainJsonTestCase(
            description="prints repair active-view payload as json",
            argv=(
                "sqt",
                "repair",
                "active-view",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
                "--table",
                "tbl__orders_enriched",
                "--deployment-id",
                "20260410T000000Z_ab12cd",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"table_name": "tbl__orders_enriched"',
                '"target_table_name": "tbl__orders_enriched__20260410T000000Z_ab12cd"',
            ),
            handler_name="run_repair_active_view",
            handler_output="{\n"
            '  "table_name": "tbl__orders_enriched",\n'
            '  "target_table_name": "tbl__orders_enriched__20260410T000000Z_ab12cd"\n'
            "}",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_cli_args_when_running_main_then_it_prints_expected_json(
    test_case: CliMainJsonTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    override_candidates: dict[str | None, PrintingCommandRunner] = {
        test_case.handler_name: PrintingCommandRunner(test_case.handler_output)
    }
    _ = override_candidates.pop(None, None)
    overrides: dict[str, PrintingCommandRunner] = cast(
        dict[str, PrintingCommandRunner], override_candidates
    )
    handlers: CliEntrypointHandlers = handlers_with_overrides(**overrides)

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )
    captured_output: str = capsys.readouterr().out
    normalized_output: str = normalize_json_output(captured_output)

    assert exit_code == test_case.expected_exit_code
    for expected_fragment in test_case.expected_output_fragments:
        assert expected_fragment in normalized_output


@pytest.mark.parametrize(
    "test_case",
    [
        CliUserErrorPresentationTestCase(
            description="missing project renders a styled error and corrective hint",
            expected_error_fragments=(
                "\033[31m\033[1mError\033[0m",
                "No SQLTide project found from the current directory.",
                "\033[33mHint\033[0m: Run this command inside a project or pass --project-dir.",
            ),
            expected_exit_code=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_project_when_running_dev_then_error_and_hint_are_styled(
    test_case: CliUserErrorPresentationTestCase,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")

    exit_code: int = run_cli(
        argv=("sqt", "dev"),
        handlers=handlers_with_overrides(),
        environment={},
        working_directory=tmp_path,
    )

    captured_error: str = capsys.readouterr().err
    assert exit_code == test_case.expected_exit_code
    for expected_fragment in test_case.expected_error_fragments:
        assert expected_fragment in captured_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliDevRefactorTestCase(
            description="dev parser retains target and variable overrides",
            expected_value=("local", {"region": "eu", "batch_size": 50}),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dev_compilation_overrides_when_parsing_then_they_are_retained(
    test_case: CliDevRefactorTestCase,
) -> None:
    args: argparse.Namespace = parse_cli_arguments(
        ["dev", "--target", "local", "--vars", '{"region":"eu","batch_size":50}']
    )

    assert (args.target, args.vars) == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        CliDevRefactorTestCase(
            description="child CLI values override inherited dev variables",
            expected_value={"warehouse_password": "secret", "batch_size": 100},
        )
    ],
    ids=lambda case: case.description,
)
def test_given_internal_dev_variables_when_resolving_child_then_cli_values_override_them(
    test_case: CliDevRefactorTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_cli_target_project(project_root=project_dir, local_contents="")
    captured: dict[str, object] = {}

    def capture_compile(*, loaded_project: LoadedProject, **_kwargs: object) -> int:
        assert loaded_project.effective_configuration is not None
        captured.update(loaded_project.effective_configuration.variables)
        return 0

    exit_code: int = run_cli(
        argv=("sqt", "compile", "--vars", '{"batch_size": 100}'),
        handlers=handlers_with_overrides(run_compile=capture_compile),
        environment={DEV_CLI_VARIABLES_ENV_VAR: '{"warehouse_password":"secret","batch_size":50}'},
        working_directory=project_dir,
    )

    assert exit_code == 0
    expected: dict[str, object] = cast(dict[str, object], test_case.expected_value)
    assert {name: captured[name] for name in expected} == expected


@pytest.mark.parametrize(
    "test_case",
    [
        CliDevRefactorTestCase(
            description="effective project target is pinned for dev",
            expected_value=("private", "private_database"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_default_target_when_starting_dev_then_effective_target_is_pinned(
    test_case: CliDevRefactorTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_cli_target_project(project_root=project_dir, local_contents='target = "private"')
    captured: dict[str, object] = {}

    def capture_dev_options(*, options: DevCommandOptions, **_kwargs: object) -> int:
        captured["options"] = options
        return 0

    exit_code: int = run_cli(
        argv=("sqt", "dev", "--project-dir", str(project_dir)),
        handlers=handlers_with_overrides(run_dev=capture_dev_options),
        adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    options: DevCommandOptions = cast(DevCommandOptions, captured["options"])
    assert exit_code == 0
    assert (options.selected_target, options.database) == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        CliDevRefactorTestCase(
            description="dev defers warehouse connections until the server lifecycle starts",
            expected_value=(True, True),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unopened_warehouse_when_starting_dev_then_connection_factories_are_deferred(
    test_case: CliDevRefactorTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_cli_target_project(project_root=project_dir, local_contents='target = "dev"')
    captured: dict[str, object] = {}

    def capture_dev_connections(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    exit_code: int = run_cli(
        argv=(
            "sqt",
            "dev",
            "--project-dir",
            str(project_dir),
            "--host",
            "warehouse.invalid",
            "--port",
            "8123",
            "--username",
            "default",
            "--password",
            "secret",
        ),
        handlers=handlers_with_overrides(run_dev=capture_dev_connections),
    )

    assert exit_code == 0
    assert captured["client"] is None
    assert (
        callable(captured["connection_factory"]),
        callable(captured["observation_connection_factory"]),
    ) == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        CliDevRefactorTestCase(
            description="dev reload reuses resolved compilation context",
            expected_value={
                "selected_target": "local",
                "cli_variables": {"region": "eu"},
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_resolved_dev_context_when_reloading_then_compilation_reuses_it(
    test_case: CliDevRefactorTestCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    loaded_project: LoadedProject = cast(LoadedProject, object())
    analysis: CompileAnalysis = cast(CompileAnalysis, object())

    def fake_load_project_input_for_path(**kwargs: object) -> LoadedProject:
        captured["load"] = kwargs
        return loaded_project

    def fake_analyze_project(**_kwargs: object) -> CompileAnalysis:
        return analysis

    def fake_run_dev_server(**kwargs: object) -> int:
        captured["context"] = kwargs["execution_context"]
        assert cast(Callable[[], CompileAnalysis], kwargs["run_compile"])() is analysis
        return 0

    monkeypatch.setattr(
        "sqltide._native.load_project_input_for_path",
        fake_load_project_input_for_path,
    )
    monkeypatch.setattr("sqltide._native.analyze_project", fake_analyze_project)
    monkeypatch.setattr("sqltide._native.run_dev_server", fake_run_dev_server)
    environment: dict[str, str] = {"SQLTIDE_ENV": "dev"}

    exit_code: int = run_dev(
        options=DevCommandOptions(
            pipelines_root=tmp_path / "pipelines",
            database="analytics",
            host="127.0.0.1",
            port=8000,
            selected_target="local",
            cli_variables=(("region", "eu"),),
            environment=environment,
        ),
        client=None,
        loaded_project=loaded_project,
        adapter_profile=cast(CompilerAdapterProfile, object()),
    )

    assert exit_code == 0
    load_context: dict[str, object] = cast(dict[str, object], captured["load"])
    assert {
        "selected_target": load_context["selected_target"],
        "cli_variables": load_context["cli_variables"],
    } == test_case.expected_value
    assert load_context["path"] == tmp_path
    assert load_context["environment"] is environment
    context: DevExecutionContext = cast(DevExecutionContext, captured["context"])
    assert context.database == "analytics"
    assert context.selected_target == "local"
    assert context.cli_variables == (("region", "eu"),)
    assert context.environment is environment


@pytest.mark.parametrize(
    "test_case",
    [
        CliAuditBackfillProjectContextTestCase(
            description="resolves project context for deployment audit",
            argv=(
                "sqt",
                "deployment",
                "audit",
                "20260410T000000Z_ab12cd",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_project_dir_name="basic_project",
            expected_pipelines_root_name="pipelines",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_audit_backfill_cli_args_when_running_main_then_it_forwards_project_context(
    test_case: CliAuditBackfillProjectContextTestCase,
) -> None:
    command_runner: RecordingAuditBackfillCommandRunner = RecordingAuditBackfillCommandRunner()

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers_with_overrides(run_deployment_audit=command_runner),
        adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    assert exit_code == test_case.expected_exit_code
    assert command_runner.pipelines_root is not None
    assert command_runner.pipelines_root.name == test_case.expected_pipelines_root_name
    assert command_runner.kwargs["project_dir"] is not None
    assert (
        cast(Path, command_runner.kwargs["project_dir"]).name == test_case.expected_project_dir_name
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainErrorTestCase(
            description="prints a clear transform sql contract error to stderr",
            argv=("sqt", "compile", "--project-dir", "BROKEN_PIPELINES_ROOT"),
            expected_exit_code=1,
            expected_error_fragments=(
                "orders_enriched",
                "outermost SELECT",
                "UNION or UNION ALL",
                "expr::Type AS name",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_transform_sql_when_running_compile_then_it_prints_a_clear_error(
    test_case: CliMainErrorTestCase,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pipeline_root: Path = tmp_path / "pipelines"
    write_cli_compilation_project(
        project_root=tmp_path,
        model_sql="""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
        UNION ALL
        SELECT CAST(order_id AS UInt64) AS order_id FROM replay_orders
        """,
    )
    argv_paths: dict[str, str] = {"BROKEN_PIPELINES_ROOT": str(pipeline_root)}
    argv: list[str] = [argv_paths.get(part, part) for part in test_case.argv]

    exit_code: int = run_cli(argv)
    captured_error: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    for expected_fragment in test_case.expected_error_fragments:
        assert expected_fragment in captured_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainIntegrationTestCase(
            description="prints deployment plan summary as text",
            argv=(
                "sqt",
                "plan",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                "Plan Ready",
                "Subtrees to rebuild: 1",
                "[replay start] raw__orders",
                "[live target] tbl__orders_enriched",
            ),
        ),
        CliMainIntegrationTestCase(
            description="prints deployment plan payload as json when requested",
            argv=(
                "sqt",
                "plan",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
                "--json",
            ),
            expected_exit_code=0,
            expected_output_fragments=(
                '"phase": "create"',
                '"name": "tbl__orders_enriched"',
                '"upstream_boundary_key":',
            ),
            expects_json_output=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_cli_args_when_running_plan_then_it_prints_expected_output(
    test_case: CliMainIntegrationTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_plan=PlanCommandRunner())
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )
    captured_output: str = capsys.readouterr().out
    normalized_output: str = OUTPUT_NORMALIZERS[test_case.expects_json_output](captured_output)

    assert exit_code == test_case.expected_exit_code
    for expected_fragment in test_case.expected_output_fragments:
        assert expected_fragment in normalized_output


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainEnvResolutionTestCase(
            description="uses clickhouse env vars for plan defaults",
            argv=("sqt", "plan", "--project-dir", "tests/fixtures/basic_project"),
            env_vars={
                "SQLTIDE_CLICKHOUSE_HOST": "localhost",
                "SQLTIDE_CLICKHOUSE_PORT": "8123",
                "SQLTIDE_CLICKHOUSE_USERNAME": "sqltide",
                "SQLTIDE_CLICKHOUSE_PASSWORD": "sqltide",
            },
            expected_exit_code=0,
            expected_kwargs={
                "database": "analytics",
                "selectors": (),
                "full_refresh": False,
                "start_time": None,
                "json_output": False,
                "verbose": False,
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_env_vars_when_running_plan_then_it_uses_env_defaults(
    test_case: CliMainEnvResolutionTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_plan=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        environment=test_case.env_vars,
        handlers=handlers,
        adapter_connection=clickhouse_client,
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    assert exit_code == test_case.expected_exit_code
    options: PlanCommandOptions = cast(PlanCommandOptions, runner.kwargs["options"])
    actual_options: dict[str, object] = {
        "database": options.database,
        "selectors": options.selectors,
        "full_refresh": options.full_refresh,
        "start_time": options.start_time,
        "json_output": options.json_output,
        "verbose": options.verbose,
    }
    assert test_case.expected_kwargs.items() <= actual_options.items()
    assert runner.kwargs["client"] is clickhouse_client


@pytest.mark.parametrize(
    "test_case",
    [
        CliProjectDefaultsTestCase(
            description="uses project yaml database default for plan",
            command_name="plan",
            expected_database="analytics",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_yaml_when_running_plan_then_it_uses_project_database_defaults(
    test_case: CliProjectDefaultsTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root: Path = tmp_path / "demo"
    write_cli_compilation_project(
        project_root=project_root,
        model_sql="""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT order_id::UInt64 AS order_id FROM __ref("orders")
        """,
    )

    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_plan=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    monkeypatch.chdir(project_root)

    exit_code: int = run_cli(
        argv=("sqt", "plan"),
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == 0
    options: PlanCommandOptions = cast(PlanCommandOptions, runner.kwargs["options"])
    assert {
        "client": clickhouse_client,
    }.items() <= runner.kwargs.items()
    assert options.database == test_case.expected_database
    assert options.selectors == ()
    assert options.full_refresh is False
    assert options.start_time is None
    assert options.json_output is False
    assert options.verbose is False


@pytest.mark.parametrize(
    "test_case",
    [
        CliProjectDefaultsTestCase(
            description="uses project database default for deployment audit",
            command_name="deployment audit",
            expected_database="analytics",
        ),
        CliProjectDefaultsTestCase(
            description="uses project yaml database default for publish",
            command_name="deployment promote",
            expected_database="analytics",
        ),
        CliProjectDefaultsTestCase(
            description="uses project yaml database default for doctor",
            command_name="doctor",
            expected_database="analytics",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_project_yaml_when_running_runtime_command_then_it_uses_project_defaults(
    test_case: CliProjectDefaultsTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root: Path = tmp_path / "demo"
    write_cli_compilation_project(
        project_root=project_root,
        model_sql="""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT order_id::UInt64 AS order_id FROM __ref("orders")
        """,
    )

    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(
        **{CLI_COMMAND_HANDLER_NAMES[test_case.command_name]: runner}
    )
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    monkeypatch.chdir(project_root)

    argv: tuple[str, ...] = CLI_COMMAND_ARGV[test_case.command_name]

    exit_code: int = run_cli(
        argv=argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == 0
    assert runner.kwargs["database"] == test_case.expected_database
    assert runner.kwargs["client"] == clickhouse_client


@pytest.mark.parametrize(
    "test_case",
    [
        CliProjectDefaultsTestCase(
            description="uses --project-dir for deployment audit",
            command_name="deployment audit",
            expected_database="analytics",
        ),
        CliProjectDefaultsTestCase(
            description="uses --project-dir for publish",
            command_name="deployment promote",
            expected_database="analytics",
        ),
        CliProjectDefaultsTestCase(
            description="uses --project-dir for doctor",
            command_name="doctor",
            expected_database="analytics",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_project_dir_when_running_runtime_command_then_it_uses_project_defaults(
    test_case: CliProjectDefaultsTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root: Path = tmp_path / "demo"
    write_cli_compilation_project(
        project_root=project_root,
        model_sql="""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT order_id::UInt64 AS order_id FROM __ref("orders")
        """,
    )

    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(
        **{CLI_COMMAND_HANDLER_NAMES[test_case.command_name]: runner}
    )
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    monkeypatch.chdir(tmp_path)

    argv: tuple[str, ...] = (
        *CLI_COMMAND_ARGV[test_case.command_name],
        "--project-dir",
        str(project_root),
    )

    exit_code: int = run_cli(
        argv=argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == 0
    assert runner.kwargs["database"] == test_case.expected_database
    assert runner.kwargs["client"] == clickhouse_client


@pytest.mark.parametrize(
    "test_case",
    [
        CliProjectConnectionResolutionTestCase(
            description="uses project clickhouse defaults when cli and env are absent",
            expected_project_connection=("localhost", 8123, "sqltide", "sqltide"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_clickhouse_defaults_when_resolving_connection_then_it_uses_them(
    test_case: CliProjectConnectionResolutionTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, port, username, password = test_case.expected_project_connection
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/selector_project"), project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "selector_project"\ndefault_target = "test"\n\n'
        '[targets.test]\ndatabase = "analytics"\n\n'
        f'[targets.test.connection]\nhost = "{host}"\nport = {port}\n'
        f'username = "{username}"\npassword = "{password}"\n',
        encoding="utf-8",
    )
    connect: MagicMock = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)

    exit_code: int = run_cli(
        argv=("sqt", "plan"),
        handlers=handlers_with_overrides(run_plan=RecordingCommandRunner()),
        environment={},
        working_directory=project_dir,
    )

    assert exit_code == 0
    assert connect.call_args.args[-1] == AdapterConnectionConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        database="analytics",
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainJsonFlagTestCase(
            description="passes json flag through to plan command",
            argv=(
                "sqt",
                "plan",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
                "--json",
            ),
            expected_exit_code=0,
            expected_json_output=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_json_flag_when_running_plan_then_it_passes_json_output_to_command(
    test_case: CliMainJsonFlagTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_plan=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == test_case.expected_exit_code
    options: PlanCommandOptions = cast(PlanCommandOptions, runner.kwargs["options"])
    assert options.json_output is test_case.expected_json_output


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectorForwardingTestCase(
            description="passes selectors and full refresh through to plan command",
            argv=(
                "sqt",
                "plan",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--select",
                "orders_enriched",
                "--select",
                "pipeline:orders",
                "--full-refresh",
                "--deployment-id",
                "20260802T120000Z_reviewed",
            ),
            expected_exit_code=0,
            expected_selectors=("orders_enriched", "pipeline:orders"),
            expected_full_refresh=True,
            expected_deployment_id="20260802T120000Z_reviewed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_selectors_when_running_plan_then_it_passes_selection_kwargs_to_command(
    test_case: CliSelectorForwardingTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_plan=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == test_case.expected_exit_code
    options: PlanCommandOptions = cast(PlanCommandOptions, runner.kwargs["options"])
    assert options.selectors == test_case.expected_selectors
    assert options.full_refresh is test_case.expected_full_refresh
    assert options.deployment_id == test_case.expected_deployment_id


@pytest.mark.parametrize(
    "test_case",
    [
        CliJanitorApplyFlagTestCase(
            description="passes apply flag through to janitor command",
            argv=(
                "sqt",
                "janitor",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
                "--apply",
            ),
            expected_exit_code=0,
            expected_apply=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_apply_flag_when_running_janitor_then_it_passes_apply_to_command(
    test_case: CliJanitorApplyFlagTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_janitor=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == test_case.expected_exit_code
    assert runner.kwargs["apply"] is test_case.expected_apply
    assert "metadata_database" not in runner.kwargs


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainErrorTestCase(
            description="prints command value errors without a traceback",
            argv=(
                "sqt",
                "deployment",
                "promote",
                "dep_missing",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=1,
            expected_error_fragments=(
                "Deployment 'dep_missing' has no staged physical tables to publish",
            ),
        ),
        CliMainErrorTestCase(
            description="renders clickhouse authentication errors without a traceback",
            argv=(
                "sqt",
                "doctor",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=1,
            expected_error_fragments=(
                "Doctor could not start",
                "ClickHouse rejected the supplied credentials",
            ),
        ),
        CliMainErrorTestCase(
            description="renders a refused clickhouse connection without a traceback",
            argv=(
                "sqt",
                "plan",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=1,
            expected_error_fragments=(
                "Plan could not connect to ClickHouse",
                "Database: analytics",
                "the connection was refused or the host could not be resolved",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_expected_command_errors_when_running_entrypoint_then_it_prints_clean_stderr(
    test_case: CliMainErrorTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handlers: CliEntrypointHandlers = handlers_with_overrides(
        run_deployment_promote=FailingCommandRunner(
            ValueError("Deployment 'dep_missing' has no staged physical tables to publish")
        ),
        run_doctor=FailingCommandRunner(
            AdapterAuthenticationError(
                "Code: 516. DB::Exception: Authentication failed. (AUTHENTICATION_FAILED)"
            )
        ),
        run_plan=FailingCommandRunner(translate_clickhouse_driver_error(refused_driver_error())),
    )
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )
    captured_error: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    assert "Traceback" not in captured_error
    for expected_fragment in test_case.expected_error_fragments:
        assert expected_fragment in captured_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliMainJsonFlagTestCase(
            description="passes json flag through to build command",
            argv=(
                "sqt",
                "build",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
                "--json",
            ),
            expected_exit_code=0,
            expected_json_output=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_json_flag_when_running_build_then_it_passes_json_output_to_command(
    test_case: CliMainJsonFlagTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_build=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    assert exit_code == test_case.expected_exit_code
    options: BuildCommandOptions = cast(BuildCommandOptions, runner.kwargs["options"])
    assert options.json_output is test_case.expected_json_output


@pytest.mark.parametrize(
    "test_case",
    [
        CliBuildObservationConnectionTestCase(
            description="build observations use a separately resolved connection",
            argv=(
                "sqt",
                "build",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_build_connection_when_dispatching_then_observations_use_second_connection(
    test_case: CliBuildObservationConnectionTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    execution_connection: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    observation_connection: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    connect: MagicMock = MagicMock(side_effect=(execution_connection, observation_connection))
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers_with_overrides(run_build=runner),
    )

    assert exit_code == test_case.expected_exit_code
    assert runner.kwargs["client"] is execution_connection
    assert runner.kwargs["observation_client"] is observation_connection


@pytest.mark.parametrize(
    "test_case",
    [
        CliBuildObservationFailureTestCase(
            description="build stops before dispatch when observation connection is unavailable",
            argv=(
                "sqt",
                "build",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--host",
                "localhost",
                "--port",
                "8123",
                "--username",
                "sqltide",
                "--password",
                "sqltide",
                "--database",
                "analytics",
            ),
            expected_exit_code=1,
            connection_error="observation connection unavailable",
            expected_error_fragment="ClickHouse operation timed out",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_observation_connection_failure_when_building_then_dispatch_is_refused(
    test_case: CliBuildObservationFailureTestCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    execution_connection: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())
    connect: MagicMock = MagicMock(
        side_effect=(execution_connection, AdapterTimeoutError(test_case.connection_error))
    )
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers_with_overrides(run_build=runner),
    )

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_error_fragment in capsys.readouterr().err
    assert runner.kwargs == {}


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectorForwardingTestCase(
            description="passes selectors and full refresh through to build command",
            argv=(
                "sqt",
                "build",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--select",
                "orders_enriched",
                "--full-refresh",
                "--auto-approve",
            ),
            expected_exit_code=0,
            expected_selectors=("orders_enriched",),
            expected_full_refresh=True,
            expected_deployment_id=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_selectors_when_running_build_then_it_passes_selection_kwargs_to_command(
    test_case: CliSelectorForwardingTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_build=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
        observation_adapter_connection=cast(AdapterConnection, FakeCliClickHouseClient()),
    )

    assert exit_code == test_case.expected_exit_code
    options: BuildCommandOptions = cast(BuildCommandOptions, runner.kwargs["options"])
    assert options.selectors == test_case.expected_selectors
    assert options.full_refresh is test_case.expected_full_refresh


@pytest.mark.parametrize(
    "test_case",
    [
        CliReconcileForwardingTestCase(
            description="passes selectors, apply, and json flags through to reconcile command",
            argv=(
                "sqt",
                "reconcile",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--select",
                "orders_enriched",
                "--apply",
                "--json",
            ),
            expected_exit_code=0,
            expected_selectors=("orders_enriched",),
            expected_apply=True,
            expected_json_output=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reconcile_flags_when_running_reconcile_then_it_passes_kwargs_to_command(
    test_case: CliReconcileForwardingTestCase,
) -> None:
    runner: RecordingCommandRunner = RecordingCommandRunner()
    handlers: CliEntrypointHandlers = handlers_with_overrides(run_reconcile=runner)
    clickhouse_client: AdapterConnection = cast(AdapterConnection, FakeCliClickHouseClient())

    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers,
        adapter_connection=clickhouse_client,
    )

    assert exit_code == test_case.expected_exit_code
    assert runner.kwargs["selectors"] == test_case.expected_selectors
    assert runner.kwargs["apply"] is test_case.expected_apply
    assert runner.kwargs["json_output"] is test_case.expected_json_output


@pytest.mark.parametrize(
    "test_case",
    [
        CliCompileArtifactsTestCase(
            description="writes compile artifacts to target layout",
            argv=(
                "sqt",
                "compile",
                "--project-dir",
                ".",
                "--target-dir",
                "target_out",
            ),
            expected_exit_code=0,
            expected_output_fragments=("Wrote compile artifacts to", "Pipelines: 1", "Models: 1"),
            expected_written_files=(
                "target_out/manifest.json",
                "target_out/sqltide_dag.json",
                "target_out/compiled/models/pl__orders/orders_enriched.sql",
                "target_out/compiled/resources/models/pl__orders/orders_enriched.table.sql",
                "target_out/compiled/resources/models/pl__orders/orders_enriched.mv.sql",
                "target_out/compiled/resources/sources/orders/kafka__orders.sql",
                "target_out/compiled/resources/sources/orders/raw__orders.sql",
                "target_out/compiled/resources/sources/orders/mv__orders.sql",
            ),
            expected_target_dir_name="target_out",
        ),
        CliCompileArtifactsTestCase(
            description="writes default target under project root",
            argv=(
                "sqt",
                "compile",
                "--project-dir",
                ".",
            ),
            expected_exit_code=0,
            expected_output_fragments=("Wrote compile artifacts to",),
            expected_written_files=(
                "target/manifest.json",
                "target/sqltide_dag.json",
                "target/compiled/models/pl__orders/orders_enriched.sql",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_compile_when_running_then_it_writes_target_artifacts(
    test_case: CliCompileArtifactsTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copytree(Path("tests/fixtures/basic_project"), project_dir)

    argv_paths: dict[str, Path] = {
        ".": project_dir,
        "target_out": project_dir / "target_out",
    }
    exit_code: int = run_cli(tuple(str(argv_paths.get(arg, arg)) for arg in test_case.argv))
    captured_out: str = capsys.readouterr().out

    assert exit_code == test_case.expected_exit_code
    for fragment in test_case.expected_output_fragments:
        assert fragment in captured_out
    for relative_file in test_case.expected_written_files:
        assert (project_dir / relative_file).exists()
    manifest_dir_name: str = test_case.expected_target_dir_name
    manifest_contents: str = (project_dir / manifest_dir_name / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert '"relations": {' in manifest_contents
    assert '"resolved_database": "analytics"' in manifest_contents
    assert '"engine": "ReplacingMergeTree(updated_at)"' in manifest_contents


@pytest.mark.parametrize(
    "test_case",
    [
        CliNestedAuditOptionsTestCase(
            description="accepts deployment audit connection and project options",
            argv=(
                "deployment",
                "audit",
                "deployment-id",
                "--project-dir",
                "child-project",
                "--host",
                "child-host",
                "--port",
                "9000",
                "--username",
                "child-user",
                "--password",
                "child-secret",
                "--database",
                "child-db",
                "--json",
                "--target",
                "child-target",
                "--vars",
                '{"scope": "child"}',
            ),
            expected_project_dir="child-project",
            expected_host="child-host",
            expected_port=9000,
            expected_username="child-user",
            expected_password="child-secret",
            expected_database="child-db",
            expected_json=True,
            expected_target="child-target",
            expected_vars={"scope": "child"},
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_audit_options_when_parsing_then_values_are_preserved(
    test_case: CliNestedAuditOptionsTestCase,
) -> None:
    args: argparse.Namespace = parse_cli_arguments(list(test_case.argv))

    assert str(args.project_dir) == test_case.expected_project_dir
    assert args.host == test_case.expected_host
    assert args.port == test_case.expected_port
    assert args.username == test_case.expected_username
    assert args.password == test_case.expected_password
    assert args.database == test_case.expected_database
    assert args.json is test_case.expected_json
    assert args.target == test_case.expected_target
    assert args.vars == test_case.expected_vars


@pytest.mark.parametrize(
    "test_case",
    (
        CliRequiredDeploymentIdTestCase(
            description="deployment show requires an identifier",
            argv=("deployment", "show"),
            expected_exit_code=2,
        ),
        CliRequiredDeploymentIdTestCase(
            description="deployment audit requires an identifier",
            argv=("deployment", "audit"),
            expected_exit_code=2,
        ),
        CliRequiredDeploymentIdTestCase(
            description="deployment promote requires an identifier",
            argv=("deployment", "promote"),
            expected_exit_code=2,
        ),
        CliRequiredDeploymentIdTestCase(
            description="deployment rollback requires an identifier or previous",
            argv=("deployment", "rollback"),
            expected_exit_code=2,
        ),
        CliRequiredDeploymentIdTestCase(
            description="deployment rollback rejects identifier with previous",
            argv=("deployment", "rollback", "deployment-id", "--previous"),
            expected_exit_code=2,
        ),
    ),
    ids=lambda case: case.description,
)
def test_given_deployment_command_without_id_when_parsing_then_it_is_rejected(
    test_case: CliRequiredDeploymentIdTestCase,
) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_cli_arguments(list(test_case.argv))

    assert raised.value.code == test_case.expected_exit_code
