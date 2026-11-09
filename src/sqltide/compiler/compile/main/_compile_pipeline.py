"""Compilation entry point for one loaded pipeline."""

import sqltide._native as _native
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.models import LoadedPipeline
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer


def compile_pipeline(
    *, loaded_pipeline: LoadedPipeline, sql_analyzer: SqlModelAnalyzer
) -> CompiledPipeline:
    """Compile an authored pipeline into a minimal desired state representation."""

    return _native.compile_pipeline(loaded_pipeline=loaded_pipeline, sql_analyzer=sql_analyzer)
