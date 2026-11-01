from pathlib import Path

import pytest

from sqltide.cli.entry.main.main import main
from tests.unit.src.sqltide.cli.playground.main._test_types import PlaygroundTestCase


@pytest.mark.parametrize(
    "test_case",
    [
        PlaygroundTestCase(description="default destination"),
        PlaygroundTestCase(
            description="destination with spaces",
            directory_name="orders example",
            arguments=("orders example",),
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

    assert main(("sqt", "playground", *test_case.arguments)) == test_case.expected_exit_code
    output: str = capsys.readouterr().out
    project: Path = tmp_path / test_case.directory_name
    assert "require a running ClickHouse server" in output
    assert (project / "compose.yaml").is_file()
    assert (project / "consumer.sql").is_file()
    assert (project / "revisions" / "completed_orders.sql").is_file()
    assert (project / ".gitignore").read_text(encoding="utf-8") == "target/\n"

    monkeypatch.delenv("SQLTIDE_CLICKHOUSE_PORT")
    assert main(("sqt", "compile", "--project-dir", str(project))) == 0
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

    assert main(("sqt", "playground")) == test_case.expected_exit_code
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

    assert main(("sqt", "playground")) == test_case.expected_exit_code
    assert "already exists" in capsys.readouterr().err
    assert destination.is_symlink()
    assert not target.exists()
