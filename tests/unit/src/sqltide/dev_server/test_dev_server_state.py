from pathlib import Path

import pytest

from sqltide._native import DevServerState
from sqltide.dev_server.exceptions import ProjectNotCompiledError
from sqltide.dev_server.models import CompileOutcome
from tests.unit.src.sqltide.dev_server._test_types import (
    CompileOutcomeTestCase,
    FailingAnalysisTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_compile_callable,
    maybe_break_project_compile,
    write_dev_server_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileOutcomeTestCase(
            description="holds servable definitions after a clean compile",
            break_compile=False,
            expected_state="ok",
            expected_has_analysis=True,
        ),
        CompileOutcomeTestCase(
            description="holds the failure when the project does not compile",
            break_compile=True,
            expected_state="failing",
            expected_has_analysis=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_project_when_reloading_then_holds_expected_outcome(
    test_case: CompileOutcomeTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    maybe_break_project_compile(project_dir=tmp_path, break_compile=test_case.break_compile)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))

    outcome: CompileOutcome = state.reload()

    assert str(outcome.state) == test_case.expected_state
    assert (outcome.analysis is not None) is test_case.expected_has_analysis


@pytest.mark.parametrize(
    "test_case",
    [
        CompileOutcomeTestCase(
            description="changes the version key on every reload",
            break_compile=False,
            expected_state="ok",
            expected_has_analysis=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_repeated_reloads_when_comparing_versions_then_each_reload_is_distinct(
    test_case: CompileOutcomeTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))

    first: CompileOutcome = state.reload()
    second: CompileOutcome = state.reload()

    assert str(first.state) == test_case.expected_state
    assert first.version_key != second.version_key


@pytest.mark.parametrize(
    "test_case",
    [
        FailingAnalysisTestCase(
            description="raises a structured error when definitions are requested while failing",
            expected_error_fragment="fix the reported error and reload",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failing_compile_when_requesting_analysis_then_it_raises_specific_error(
    test_case: FailingAnalysisTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    maybe_break_project_compile(project_dir=tmp_path, break_compile=True)
    state: DevServerState = DevServerState(run_compile=build_compile_callable(project_dir=tmp_path))

    with pytest.raises(ProjectNotCompiledError, match=test_case.expected_error_fragment):
        state.current_analysis()
