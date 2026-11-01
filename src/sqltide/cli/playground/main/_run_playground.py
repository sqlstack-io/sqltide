"""Create the packaged, local-only virtual workflow example."""

import shlex
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from sqltide.cli.entry.exceptions import CliUserError


def run_playground(*, directory: Path, working_directory: Path) -> int:
    destination: Path = working_directory / directory
    try:
        with as_file(files("sqltide").joinpath("playground_templates")) as template:
            shutil.copytree(template, destination)
    except FileExistsError as error:
        raise CliUserError(
            f"Playground destination already exists: {directory}",
            hint="Choose a new directory. Existing files are never overwritten.",
        ) from error
    except OSError as error:
        raise CliUserError(f"Could not create playground: {error}") from error
    print(
        "SQLTide playground created\n"
        "\n"
        "New version. Same query.\n"
        "Plan → build candidate → inspect → promote\n"
        "\n"
        "Try offline (no Docker, ClickHouse, or Kafka needed):\n"
        f"  cd {shlex.quote(str(directory))}\n"
        "  sqt compile\n"
        "\n"
        "Then follow README.md for the real ClickHouse workflow.\n"
        "It includes Docker Compose setup, seeded orders, and expected query results.\n"
        "Plan, build, inspection, and promotion require a running ClickHouse server.\n"
        "Nothing has been started, installed, or executed in ClickHouse."
    )
    return 0
