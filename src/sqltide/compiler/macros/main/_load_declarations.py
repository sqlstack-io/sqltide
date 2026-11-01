"""Load a project's constant and enum declarations."""

import sqltide._native as _native
from sqltide.compiler.discovery.models import DiscoveredProjectFile


def load_declarations(
    *,
    constant_files: tuple[DiscoveredProjectFile, ...],
    enum_files: tuple[DiscoveredProjectFile, ...],
) -> _native.Declarations:
    """Validate every project-wide CONSTANT and ENUM declaration once."""

    return _native.load_declarations(
        [(str(file.relative_path), file.contents) for file in constant_files],
        [(str(file.relative_path), file.contents) for file in enum_files],
    )
