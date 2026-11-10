"""Public logical-project realization entry point."""

import sqltide._native as _native
from sqltide._native import SqlModelAnalyzer
from sqltide.compiler.compile.models import CompiledProject, CompilerAdapterProfile
from sqltide.compiler.pipeline.models import RealizedProject


def realize_project(
    *,
    project: CompiledProject,
    adapter_profile: CompilerAdapterProfile,
    sql_analyzer: SqlModelAnalyzer,
) -> RealizedProject:
    """Realize a complete logical project through connection-free adapter callbacks."""

    return _native.realize_project(
        project=project,
        adapter_profile=adapter_profile,
        sql_analyzer=sql_analyzer,
    )
