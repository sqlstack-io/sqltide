"""Public entrypoint for the generated chain golden project."""

from pathlib import Path

from scripts.compiler_goldens._helpers.generated import (
    write_generated_chain_project as _write_generated_chain_project,
)


def write_generated_chain_project(*, project_dir: Path, model_count: int) -> None:
    """Write a linear chain project of `model_count` models under `project_dir`."""

    _write_generated_chain_project(project_dir=project_dir, model_count=model_count)
