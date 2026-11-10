from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from sqltide._native import run_destruction
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.auth.classes.control_store import ControlStore
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction.models import (
    DestructionExecutionResult,
    DestructionPlan,
)
from tests.unit.src.sqltide.cli.destruction.helpers import (
    DESTRUCTION_ADMIN_ID,
    destruction_account,
    destruction_options,
    destruction_plan,
    use_process_local_plan_store_for_unit_tests,
)
from tests.unit.src.sqltide.cli.destruction.main._test_types import (
    DestructionAuthorizationTestCase,
    DestructionCancellationTestCase,
    DestructionReauthorizationTestCase,
    DestructionRunTestCase,
)

_USE_PROCESS_LOCAL_PLAN_STORE: object = pytest.fixture(autouse=True)(
    use_process_local_plan_store_for_unit_tests
)


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRunTestCase(
            description="reviewed plan is actor bound challenged and freshly recompiled",
            expected_exit_code=0,
            expected_actor_id=str(DESTRUCTION_ADMIN_ID),
            expected_actor_name="persisted-admin",
            expected_challenge_responses=("alpha",),
            expected_analysis_count_after_replan=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_reviewed_plan_when_running_destruction_then_executes_frozen_actor_bound_plan(
    test_case: DestructionRunTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Planning, review, and execution dependencies for one terminal actor.
    plan: DestructionPlan = destruction_plan()
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(compile_inputs=SimpleNamespace(pipelines=())),
    )
    analyze_mock: MagicMock = MagicMock(return_value=analysis)
    planner_mock: MagicMock = MagicMock(return_value=plan)
    execute_mock: MagicMock = MagicMock(
        return_value=DestructionExecutionResult(
            invocation_id="invocation-1",
            outcome="succeeded",
            completed_statement_sequences=(1,),
            pending_statement_sequences=(),
            remaining_relation_names=(),
            error_message=None,
        )
    )
    input_mock: MagicMock = MagicMock(side_effect=("yes", "alpha"))
    control_store: MagicMock = MagicMock(spec=ControlStore)
    control_store.get_user_by_username.side_effect = (
        destruction_account(),
        destruction_account(),
        destruction_account(),
    )
    control_store_factory: MagicMock = MagicMock(return_value=control_store)
    project_loader: MagicMock = MagicMock(return_value=None)
    monkeypatch.setattr("sqltide._native.analyze_project", analyze_mock)
    monkeypatch.setattr("sqltide._native.plan_destruction", planner_mock)
    monkeypatch.setattr("sqltide._native.execute_destruction", execute_mock)
    monkeypatch.setattr(
        "sqltide.auth.classes.control_store.ControlStore",
        control_store_factory,
    )
    monkeypatch.setattr(
        "sqltide._native.load_project_input_for_path",
        project_loader,
    )
    monkeypatch.setattr(
        "getpass.getuser",
        MagicMock(return_value="terminal-user"),
    )
    monkeypatch.setattr("builtins.input", input_mock)

    # When: The command runs and its locked replan callback is evaluated.
    exit_code: int = run_destruction(
        options=destruction_options(),
        client=cast(AdapterConnection, SimpleNamespace()),
        observation_client=cast(AdapterConnection, SimpleNamespace()),
        loaded_project=None,
        adapter_profile=cast(CompilerAdapterProfile, SimpleNamespace()),
    )
    execute_kwargs: Mapping[str, Any] = execute_mock.call_args.kwargs
    replanned: DestructionPlan = execute_kwargs["replan"]()

    # Then: Review and exact challenges are bound to getpass identity and a fresh compile.
    assert exit_code == test_case.expected_exit_code
    assert execute_kwargs["actor"].actor_id == test_case.expected_actor_id
    assert execute_kwargs["actor"].actor_name == test_case.expected_actor_name
    assert execute_kwargs["challenge_responses"] == test_case.expected_challenge_responses
    assert (
        execute_kwargs["store"].reviewed_at(plan_id=plan.plan_id, actor=test_case.expected_actor_id)
        == (execute_kwargs["reviewed_at"])
    )
    assert replanned is plan
    assert analyze_mock.call_count == test_case.expected_analysis_count_after_replan
    assert control_store.get_user_by_username.call_args_list == [
        call(username="terminal-user"),
        call(username="terminal-user"),
        call(username="terminal-user"),
    ]
    control_store_factory.assert_called_once_with(url="sqlite:////custom/control.db")
    project_loader.assert_called_once_with(
        path=Path("/project"),
        selected_target="non-default-target",
        cli_variables={"resource_suffix": "_cli"},
        environment={"RESOURCE_SCHEMA": "environment_schema"},
    )
    control_store.close.assert_called_once_with()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionReauthorizationTestCase(
            description="admin role revoked during review",
            refreshed_account=destruction_account(roles=("viewer",)),
            expected_error_fragment="built-in 'admin' role",
        ),
        DestructionReauthorizationTestCase(
            description="account deactivated during review",
            refreshed_account=destruction_account(active=False),
            expected_error_fragment="is inactive",
        ),
        DestructionReauthorizationTestCase(
            description="same username account identity replaced during review",
            refreshed_account=destruction_account(
                user_id=UUID("cd692690-9715-4b7d-9d62-b91aa87071bd")
            ),
            expected_error_fragment="changed identity during destruction review",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_admin_changes_after_review_when_challenges_finish_then_refuses_before_execution(
    test_case: DestructionReauthorizationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: A valid creator whose persisted authorization changes during interactive review.
    plan: DestructionPlan = destruction_plan()
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(compile_inputs=SimpleNamespace(pipelines=())),
    )
    control_store: MagicMock = MagicMock(spec=ControlStore)
    control_store.get_user_by_username.side_effect = (
        destruction_account(),
        test_case.refreshed_account,
    )
    execute_mock: MagicMock = MagicMock()
    planner_mock: MagicMock = MagicMock(return_value=plan)
    monkeypatch.setattr(
        "sqltide.auth.classes.control_store.ControlStore",
        MagicMock(return_value=control_store),
    )
    monkeypatch.setattr(
        "getpass.getuser",
        MagicMock(return_value="terminal-user"),
    )
    monkeypatch.setattr(
        "sqltide._native.analyze_project",
        MagicMock(return_value=analysis),
    )
    monkeypatch.setattr("sqltide._native.plan_destruction", planner_mock)
    monkeypatch.setattr("sqltide._native.execute_destruction", execute_mock)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=("yes", "alpha")))

    # When: The challenge has been entered and the account is reloaded before execution.
    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        run_destruction(
            options=destruction_options(),
            client=cast(AdapterConnection, SimpleNamespace()),
            observation_client=cast(AdapterConnection, SimpleNamespace()),
            loaded_project=None,
            adapter_profile=cast(CompilerAdapterProfile, SimpleNamespace()),
        )

    # Then: The reviewed plan is not consumed and no warehouse mutation path starts.
    assert control_store.get_user_by_username.call_args_list == [
        call(username="terminal-user"),
        call(username="terminal-user"),
    ]
    planner_mock.assert_called_once()
    execute_mock.assert_not_called()
    control_store.close.assert_called_once_with()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionCancellationTestCase(
            description="declined review prevents execution",
            expected_exit_code=1,
            expected_execution_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_plan_without_review_when_running_destruction_then_execution_is_cancelled(
    test_case: DestructionCancellationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: A planned operation whose review prompt is declined.
    plan: DestructionPlan = destruction_plan()
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(compile_inputs=SimpleNamespace(pipelines=())),
    )
    execute_mock: MagicMock = MagicMock()
    control_store: MagicMock = MagicMock(spec=ControlStore)
    control_store.get_user_by_username.return_value = destruction_account()
    monkeypatch.setattr(
        "sqltide._native.analyze_project",
        MagicMock(return_value=analysis),
    )
    monkeypatch.setattr(
        "sqltide._native.plan_destruction",
        MagicMock(return_value=plan),
    )
    monkeypatch.setattr("sqltide._native.execute_destruction", execute_mock)
    monkeypatch.setattr(
        "sqltide.auth.classes.control_store.ControlStore",
        MagicMock(return_value=control_store),
    )
    monkeypatch.setattr(
        "getpass.getuser",
        MagicMock(return_value="terminal-user"),
    )
    monkeypatch.setattr("builtins.input", MagicMock(return_value="no"))

    # When: The command reaches the separate review gate.
    exit_code: int = run_destruction(
        options=destruction_options(),
        client=cast(AdapterConnection, SimpleNamespace()),
        observation_client=cast(AdapterConnection, SimpleNamespace()),
        loaded_project=None,
        adapter_profile=cast(CompilerAdapterProfile, SimpleNamespace()),
    )

    # Then: No challenge or execution path can bypass review.
    assert exit_code == test_case.expected_exit_code
    assert execute_mock.call_count == test_case.expected_execution_count
    control_store.close.assert_called_once_with()


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionAuthorizationTestCase(
            description="unknown OS user",
            account=None,
            expected_error_fragment="is not registered",
        ),
        DestructionAuthorizationTestCase(
            description="inactive persisted account",
            account=destruction_account(active=False),
            expected_error_fragment="is inactive",
        ),
        DestructionAuthorizationTestCase(
            description="active viewer without built-in admin role",
            account=destruction_account(roles=("viewer",)),
            expected_error_fragment="built-in 'admin' role",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unauthorized_os_account_when_running_destruction_then_refuses_before_planning(
    test_case: DestructionAuthorizationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: The OS username resolves to no account or an account without active admin status.
    control_store: MagicMock = MagicMock(spec=ControlStore)
    control_store.get_user_by_username.return_value = test_case.account
    planner: MagicMock = MagicMock()
    analyzer: MagicMock = MagicMock()
    monkeypatch.setattr(
        "sqltide.auth.classes.control_store.ControlStore",
        MagicMock(return_value=control_store),
    )
    monkeypatch.setattr(
        "getpass.getuser",
        MagicMock(return_value="terminal-user"),
    )
    monkeypatch.setattr("sqltide._native.plan_destruction", planner)
    monkeypatch.setattr("sqltide._native.analyze_project", analyzer)

    # When: Standalone CLI authorization is checked before compilation or warehouse planning.
    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        run_destruction(
            options=destruction_options(),
            client=cast(AdapterConnection, SimpleNamespace()),
            observation_client=cast(AdapterConnection, SimpleNamespace()),
            loaded_project=None,
            adapter_profile=cast(CompilerAdapterProfile, SimpleNamespace()),
        )

    # Then: Project roles and build permissions cannot reach destructive planning.
    control_store.get_user_by_username.assert_called_once_with(username="terminal-user")
    analyzer.assert_not_called()
    planner.assert_not_called()
    control_store.close.assert_called_once_with()
