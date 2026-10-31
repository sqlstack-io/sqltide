"""Stage and publish complete static compile targets without touching runtime evidence."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import cast

import sqltide._native as _native
from sqltide.cli.compile.constants import (
    ARTIFACTS_MANIFEST_FIELD,
    RESERVED_TARGET_OWNER_NAMES,
)
from sqltide.cli.compile.exceptions import CompileArtifactError
from sqltide.cli.compile.models import StaticCompileArtifacts


def publish_static_compile_artifacts(
    *, artifacts: StaticCompileArtifacts, target_dir: Path
) -> None:
    """Stage every static owner before replacing the live compiled target."""

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root: Path = Path(
        tempfile.mkdtemp(prefix=".sqltide-static-staging-", dir=target_dir.parent)
    )
    backup_root: Path = Path(
        tempfile.mkdtemp(prefix=".sqltide-static-backup-", dir=target_dir.parent)
    )
    legacy_pipeline_paths: tuple[Path, ...] = _legacy_pipeline_paths(target_dir=target_dir)
    try:
        _stage_artifacts(artifacts=artifacts, staging_root=staging_root)
        _publish_staged_target(
            staging_root=staging_root,
            backup_root=backup_root,
            target_dir=target_dir,
        )
        legacy_pipeline_path: Path
        for legacy_pipeline_path in legacy_pipeline_paths:
            shutil.rmtree(legacy_pipeline_path, ignore_errors=True)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def _stage_artifacts(*, artifacts: StaticCompileArtifacts, staging_root: Path) -> None:
    (staging_root / "compiled").mkdir(parents=True, exist_ok=True)
    try:
        _native.stage_artifact_files(
            staging_root,
            [
                (artifact_file.relative_path, artifact_file.contents)
                for artifact_file in artifacts.compiled_files
            ],
        )
    except ValueError as error:
        raise CompileArtifactError(str(error)) from None
    (staging_root / "manifest.json").write_text(artifacts.manifest_json, encoding="utf-8")
    (staging_root / "sqltide_dag.json").write_text(artifacts.dag_json, encoding="utf-8")


def _publish_staged_target(*, staging_root: Path, backup_root: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    owner_name: str
    moved_old_owner_names: list[str] = []
    published_owner_names: list[str] = []
    try:
        for owner_name in ("compiled", "manifest.json", "sqltide_dag.json"):
            live_path: Path = target_dir / owner_name
            if live_path.exists():
                os.replace(live_path, backup_root / owner_name)
                moved_old_owner_names.append(owner_name)
            os.replace(staging_root / owner_name, live_path)
            published_owner_names.append(owner_name)
    except OSError:
        _restore_previous_target(
            backup_root=backup_root,
            target_dir=target_dir,
            moved_old_owner_names=tuple(moved_old_owner_names),
            published_owner_names=tuple(published_owner_names),
        )
        raise


def _restore_previous_target(
    *,
    backup_root: Path,
    target_dir: Path,
    moved_old_owner_names: tuple[str, ...],
    published_owner_names: tuple[str, ...],
) -> None:
    owner_name: str
    for owner_name in reversed(published_owner_names):
        published_path: Path = target_dir / owner_name
        if published_path.is_dir():
            shutil.rmtree(published_path)
        elif published_path.exists():
            published_path.unlink()
    for owner_name in reversed(moved_old_owner_names):
        os.replace(backup_root / owner_name, target_dir / owner_name)


def _legacy_pipeline_paths(*, target_dir: Path) -> tuple[Path, ...]:
    manifest_path: Path = target_dir / "manifest.json"
    if not manifest_path.is_file():
        return ()
    payload: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest: dict[str, object] = cast(dict[str, object], payload)
    if ARTIFACTS_MANIFEST_FIELD in manifest:
        return ()
    pipelines: dict[str, object] = cast(dict[str, object], manifest.get("pipelines", {}))
    legacy_paths: list[Path] = []
    pipeline_name: str
    for pipeline_name in pipelines:
        pipeline_path: Path = target_dir / pipeline_name
        if pipeline_name in RESERVED_TARGET_OWNER_NAMES or not pipeline_path.is_dir():
            continue
        legacy_paths.append(pipeline_path)
    return tuple(legacy_paths)
