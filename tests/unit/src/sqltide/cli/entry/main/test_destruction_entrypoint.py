import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from _pytest.capture import CaptureResult

from sqltide._native import run_cli
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.destruction.models import DestructionCommandOptions
from tests.unit.src.sqltide.cli.entry.main._test_types import (
    DestructionDispatchTestCase,
    DestructionTtyTestCase,
)
from tests.unit.src.sqltide.cli.entry.main.helpers import handlers_with_destruction


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionTtyTestCase(
            description="non tty destroy fails before project resolution",
            argv=("sqt", "destroy", "--target", "uat", "--select", "pipeline:alpha"),
            expected_exit_code=1,
            expected_error_fragments=("requires an interactive terminal", "stdin is not a TTY"),
        ),
        DestructionTtyTestCase(
            description="non tty reset target fails before project resolution",
            argv=("sqt", "reset-target", "--target", "uat"),
            expected_exit_code=1,
            expected_error_fragments=("requires an interactive terminal", "stdin is not a TTY"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_non_tty_when_starting_destruction_then_rejects_before_project_work(
    test_case: DestructionTtyTestCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: Destruction input from a non-interactive stream.
    resolution_mock: MagicMock = MagicMock()
    monkeypatch.setattr(sys.stdin, "isatty", MagicMock(return_value=False))
    monkeypatch.setattr("sqltide._native.load_project_input_for_path", resolution_mock)

    # When: Parsing succeeds and the entrypoint applies its immediate terminal gate.
    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers_with_destruction(run_destruction=MagicMock()),
    )

    # Then: No project, credential, or connection resolution has started.
    captured: CaptureResult[str] = capsys.readouterr()
    assert exit_code == test_case.expected_exit_code
    for fragment in test_case.expected_error_fragments:
        assert fragment in captured.err
    resolution_mock.assert_not_called()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionDispatchTestCase(
            description="destroy dispatch receives a dedicated observation connection",
            argv=(
                "sqt",
                "destroy",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--target",
                "test",
                "--select",
                "pipeline:pl__orders",
            ),
            expected_operation="destroy_pipelines",
            expected_selected_target="test",
            expected_selectors=("pipeline:pl__orders",),
            expected_control_store_url=(
                f"sqlite:///{Path.cwd() / 'tests/fixtures/basic_project/.sqltide/control.db'}"
            ),
            expected_cli_variables=(("inherited_resource", "environment"),),
            expected_exit_code=0,
        ),
        DestructionDispatchTestCase(
            description="reset target dispatch uses an explicit control store",
            argv=(
                "sqt",
                "reset-target",
                "--project-dir",
                "tests/fixtures/basic_project",
                "--target",
                "test",
                "--control-store-url",
                "sqlite:////tmp/custom-control.db",
                "--vars",
                '{"resource_suffix":"_cli"}',
            ),
            expected_operation="reset_target",
            expected_selected_target="test",
            expected_selectors=(),
            expected_control_store_url="sqlite:////tmp/custom-control.db",
            expected_cli_variables=(
                ("inherited_resource", "environment"),
                ("resource_suffix", "_cli"),
            ),
            expected_exit_code=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_tty_destroy_when_dispatching_then_forwards_scope_and_both_connections(
    test_case: DestructionDispatchTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: A resolvable project and two separately supplied warehouse connections.
    primary: AdapterConnection = cast(AdapterConnection, MagicMock(spec=AdapterConnection))
    observation: AdapterConnection = cast(AdapterConnection, MagicMock(spec=AdapterConnection))
    runner: MagicMock = MagicMock(return_value=test_case.expected_exit_code)
    monkeypatch.setattr(sys.stdin, "isatty", MagicMock(return_value=True))

    # When: The parsed command is resolved and dispatched.
    exit_code: int = run_cli(
        argv=test_case.argv,
        handlers=handlers_with_destruction(run_destruction=runner),
        environment={
            "RESOURCE_SCHEMA": "environment_schema",
            "SQLTIDE_CONTROL_STORE_URL": "sqlite:////tmp/ignored-control.db",
            "SQLTIDE_INTERNAL_CLI_VARS": '{"inherited_resource":"environment"}',
        },
        adapter_connection=primary,
        observation_adapter_connection=observation,
    )

    # Then: The handler receives explicit scope and the distinct observation client.
    kwargs: Mapping[str, object] = runner.call_args.kwargs
    options: DestructionCommandOptions = cast(DestructionCommandOptions, kwargs["options"])
    assert exit_code == test_case.expected_exit_code
    assert options.operation == test_case.expected_operation
    assert options.selected_target == test_case.expected_selected_target
    assert options.selectors == test_case.expected_selectors
    assert options.control_store_url == test_case.expected_control_store_url
    assert options.cli_variables == test_case.expected_cli_variables
    assert options.environment == {
        "RESOURCE_SCHEMA": "environment_schema",
        "SQLTIDE_CONTROL_STORE_URL": "sqlite:////tmp/ignored-control.db",
        "SQLTIDE_INTERNAL_CLI_VARS": '{"inherited_resource":"environment"}',
    }
    assert kwargs["client"] is primary
    assert kwargs["observation_client"] is observation
