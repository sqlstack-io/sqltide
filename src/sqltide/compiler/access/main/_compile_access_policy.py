"""Compile one optional retained root access policy."""

import sqltide._native as _native
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.discovery.models import DiscoveredProjectFile


def compile_access_policy(
    *, source_file: DiscoveredProjectFile | None, pipeline_names: frozenset[str]
) -> CompiledAccessPolicy | None:
    """Compile strict YAML into one immutable policy, or return no policy."""

    if source_file is None:
        return None
    return _native.compile_access_policy(
        source_file.contents, source_file.file_path, pipeline_names
    )
