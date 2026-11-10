"""Helpers for access-policy compiler tests."""

from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.discovery.models import DiscoveredProjectFile


def access_source_file(*, tmp_path: Path, contents: str) -> DiscoveredProjectFile:
    """Build a retained root access file without filesystem I/O."""

    path: Path = tmp_path / "access.yml"
    return DiscoveredProjectFile(
        file_path=path, relative_path=Path("access.yml"), contents=contents
    )


def compile_access_policy(
    *, source_file: DiscoveredProjectFile, pipeline_names: frozenset[str]
) -> CompiledAccessPolicy:
    """Compile one retained access file through the native policy compiler."""

    return _native.compile_access_policy(
        source_file.contents, source_file.file_path, pipeline_names
    )
