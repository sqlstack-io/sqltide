from pathlib import Path

import pytest

from sqltide._native import run_cli
from tests.unit.src.sqltide.cli.playground.main._test_types import (
    PlaygroundStartFailureTestCase,
    PlaygroundTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PlaygroundTestCase(description="default destination", arguments=("--no-start",)),
        PlaygroundTestCase(
            description="destination with spaces",
            directory_name="orders example",
            arguments=("orders example", "--no-start"),
        ),
        PlaygroundTestCase(
            description="streaming template",
            directory_name="sqltide-streaming-playground",
            arguments=("--template", "streaming", "--no-start"),
            expected_files=(
                "compose.yaml",
                "consumer.sql",
                "net_revenue.sql",
                "producer/fake_orders_producer.py",
                "revisions/order_event_facts.sql",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_no_project_when_creating_playground_then_it_compiles_offline(
    test_case: PlaygroundTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    # Creation must not load a surrounding project's config or warehouse credentials.
    (tmp_path / "sqltide_project.toml").write_text("invalid TOML [", encoding="utf-8")
    monkeypatch.setenv("SQLTIDE_CLICKHOUSE_PORT", "not-a-port")

    assert run_cli(("sqt", "playground", *test_case.arguments)) == test_case.expected_exit_code
    output: str = capsys.readouterr().out
    project: Path = tmp_path / test_case.directory_name
    assert "Nothing was started" in output
    assert all((project / path).is_file() for path in test_case.expected_files)
    assert (project / ".gitignore").read_text(encoding="utf-8") == "target/\n"
    # A source checkout's bytecode and compile output never reach the new project.
    assert not any(path.name in {"__pycache__", "target"} for path in project.rglob("*"))

    monkeypatch.delenv("SQLTIDE_CLICKHOUSE_PORT")
    assert run_cli(("sqt", "compile", "--project-dir", str(project))) == 0
    assert (project / "target").is_dir()


@pytest.mark.parametrize(
    "test_case",
    [PlaygroundTestCase(description="existing work is preserved", expected_exit_code=1)],
    ids=lambda case: case.description,
)
def test_given_existing_directory_when_creating_playground_then_files_are_unchanged(
    test_case: PlaygroundTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    project: Path = tmp_path / test_case.directory_name
    project.mkdir()
    existing_file: Path = project / "README.md"
    existing_file.write_text("Keep my work", encoding="utf-8")

    assert run_cli(("sqt", "playground", "--no-start")) == test_case.expected_exit_code
    assert "already exists" in capsys.readouterr().err
    assert existing_file.read_text(encoding="utf-8") == "Keep my work"
    assert tuple(project.iterdir()) == (existing_file,)


@pytest.mark.parametrize(
    "test_case",
    [PlaygroundTestCase(description="dangling destination is preserved", expected_exit_code=1)],
    ids=lambda case: case.description,
)
def test_given_dangling_symlink_when_creating_playground_then_target_is_not_created(
    test_case: PlaygroundTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    target: Path = tmp_path / "missing"
    destination: Path = tmp_path / test_case.directory_name
    destination.symlink_to(target, target_is_directory=True)

    assert run_cli(("sqt", "playground", "--no-start")) == test_case.expected_exit_code
    assert "already exists" in capsys.readouterr().err
    assert destination.is_symlink()
    assert not target.exists()


@pytest.mark.parametrize(
    "test_case",
    [
        PlaygroundStartFailureTestCase(
            description="without docker the project is kept and the next step is named",
            expected_exit_code=1,
            expected_message="Docker is not installed or not on PATH",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_docker_when_creating_playground_then_project_is_kept_and_up_is_suggested(
    test_case: PlaygroundStartFailureTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))

    assert run_cli(("sqt", "playground")) == test_case.expected_exit_code
    error: str = capsys.readouterr().err
    assert test_case.expected_message in error
    assert "sqt playground up" in error
    assert (tmp_path / "sqltide-playground" / "compose.yaml").is_file()


@pytest.mark.parametrize(
    "test_case",
    [
        PlaygroundStartFailureTestCase(
            description="query refuses an ordinary project before connecting",
            expected_exit_code=1,
            expected_message="is not a playground project",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_ordinary_project_when_querying_then_nothing_runs(
    test_case: PlaygroundStartFailureTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqltide_project.toml").write_text(
        'name = "orders"\n[connection]\nhost = "127.0.0.1"\nport = 1\n', encoding="utf-8"
    )
    (tmp_path / "drop.sql").write_text("DROP TABLE orders;\n", encoding="utf-8")

    assert run_cli(("sqt", "playground", "query", "drop.sql")) == test_case.expected_exit_code
    assert test_case.expected_message in capsys.readouterr().err
