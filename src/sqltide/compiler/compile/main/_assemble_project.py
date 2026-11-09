"""Apache-2.0: SQLBuild compiler/compile/main/_assemble_project.py@7e3b2f854f05."""

import sqltide._native as _native
from sqltide.compiler.compile.models import CompiledProject, CompileProjectInputs
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)


def assemble_project(
    *,
    inputs: CompileProjectInputs,
    reference_rewriter: SqlReferenceRewriter,
    sql_analyzer: SqlModelAnalyzer,
) -> CompiledProject:
    """Compile and validate every attached project resource exactly once."""

    return _native.assemble_project(
        inputs=inputs,
        reference_rewriter=reference_rewriter,
        sql_analyzer=sql_analyzer,
    )
