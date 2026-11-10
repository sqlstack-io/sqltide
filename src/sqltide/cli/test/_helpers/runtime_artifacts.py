"""Stage and publish the executed SQL-test runtime target."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from sqltide._native import runtime_test_path
from sqltide.cli.test.constants import (
    RUNTIME_STAGING_PREFIX,
    RUNTIME_TARGET_OWNER,
    RUNTIME_TESTS_OWNER,
)
from sqltide.compiler.testing.models import SqlTestCase
from sqltide.executor.testing.models import SqlTestExecutionResult


def write_test_runtime_target(
    *,
    target_dir: Path,
    test_cases: tuple[SqlTestCase, ...],
    results: tuple[SqlTestExecutionResult, ...],
) -> None:
    """Persist the exact SQL handed to the adapter under `run/tests`."""

    executed_sql_by_key: dict[tuple[Path, int], str] = {
        (result.file_path, result.test_index): result.executed_sql for result in results
    }
    run_dir: Path = target_dir / RUNTIME_TARGET_OWNER
    run_dir.mkdir(parents=True, exist_ok=True)
    staging_root: Path = Path(tempfile.mkdtemp(prefix=RUNTIME_STAGING_PREFIX, dir=target_dir))
    backup_root: Path = Path(tempfile.mkdtemp(prefix=RUNTIME_STAGING_PREFIX, dir=target_dir))
    try:
        _stage_runtime_tests(
            staging_root=staging_root,
            test_cases=test_cases,
            executed_sql_by_key=executed_sql_by_key,
        )
        _publish_runtime_tests(staging_root=staging_root, backup_root=backup_root, run_dir=run_dir)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def _stage_runtime_tests(
    *,
    staging_root: Path,
    test_cases: tuple[SqlTestCase, ...],
    executed_sql_by_key: dict[tuple[Path, int], str],
) -> None:
    (staging_root / RUNTIME_TESTS_OWNER).mkdir(parents=True, exist_ok=True)
    test_case: SqlTestCase
    for test_case in test_cases:
        executed_sql: str | None = executed_sql_by_key.get(
            (test_case.file_path, test_case.test_index)
        )
        if executed_sql is None:
            continue
        relative_path: Path = runtime_test_path(test_case=test_case)
        staged_path: Path = staging_root / Path(*relative_path.parts[1:])
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(executed_sql.encode("utf-8"))


def _publish_runtime_tests(*, staging_root: Path, backup_root: Path, run_dir: Path) -> None:
    live_path: Path = run_dir / RUNTIME_TESTS_OWNER
    backup_path: Path = backup_root / RUNTIME_TESTS_OWNER
    moved: bool = live_path.exists()
    if moved:
        os.replace(live_path, backup_path)
    try:
        os.replace(staging_root / RUNTIME_TESTS_OWNER, live_path)
    except OSError:
        _restore_runtime_tests(backup_path=backup_path, live_path=live_path, moved=moved)
        raise


def _restore_runtime_tests(*, backup_path: Path, live_path: Path, moved: bool) -> None:
    if moved:
        os.replace(backup_path, live_path)
