"""Run one compile in a scratch copy and capture its deterministic outputs."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.compiler_goldens.constants import (
    COMPILE_ENVIRONMENT,
    DEFINITIONS_SCRIPT,
    EXCLUDED_TARGET_DIRECTORIES,
    JSON_ARTIFACT_SUFFIX,
    PROJECT_PATH_MASK,
)
from scripts.compiler_goldens.models import CompilerCapture


def capture_compile(*, name: str, project_dir: Path) -> CompilerCapture:
    with tempfile.TemporaryDirectory(prefix="sqltide-golden-") as scratch:
        work_dir: Path = Path(scratch) / name
        shutil.copytree(
            project_dir,
            work_dir,
            ignore=shutil.ignore_patterns("target", ".sqltide", ".env", "__pycache__"),
        )
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from sqltide._native import run_cli; sys.exit(run_cli(sys.argv))",
                "compile",
                "--project-dir",
                str(work_dir),
            ],
            cwd=work_dir,
            env={**os.environ, **COMPILE_ENVIRONMENT},
            capture_output=True,
            text=True,
            check=False,
        )
        definitions: object = _definitions(work_dir=work_dir) if completed.returncode == 0 else None
        return CompilerCapture(
            project=name,
            exit_code=completed.returncode,
            definitions=definitions,
            stdout=_masked_lines(text=completed.stdout, work_dir=work_dir),
            stderr=_masked_lines(text=completed.stderr, work_dir=work_dir),
            artifacts=_target_artifacts(target_dir=work_dir / "target", work_dir=work_dir),
        )


def _masked_lines(*, text: str, work_dir: Path) -> tuple[str, ...]:
    return tuple(_mask(text=text, work_dir=work_dir).splitlines())


def _mask(*, text: str, work_dir: Path) -> str:
    return text.replace(str(work_dir.resolve()), PROJECT_PATH_MASK).replace(
        str(work_dir), PROJECT_PATH_MASK
    )


def _target_artifacts(*, target_dir: Path, work_dir: Path) -> dict[str, object]:
    files: list[Path] = sorted(
        path
        for path in target_dir.rglob("*")
        if path.is_file()
        and path.relative_to(target_dir).parts[0] not in EXCLUDED_TARGET_DIRECTORIES
    )
    return {
        path.relative_to(target_dir).as_posix(): _artifact_value(path=path, work_dir=work_dir)
        for path in files
    }


def _artifact_value(*, path: Path, work_dir: Path) -> object:
    text: str = _mask(text=path.read_text(encoding="utf-8"), work_dir=work_dir)
    return json.loads(text) if path.suffix == JSON_ARTIFACT_SUFFIX else text.splitlines()


def _definitions(*, work_dir: Path) -> object:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-c", DEFINITIONS_SCRIPT, str(work_dir)],
        cwd=work_dir,
        env={**os.environ, **COMPILE_ENVIRONMENT},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(_mask(text=completed.stdout, work_dir=work_dir))
