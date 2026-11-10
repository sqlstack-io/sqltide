"""CLI command for offline project compilation."""

import sys
from pathlib import Path

from sqltide._native import analyze_project
from sqltide.cli.compile.main._write_static_compile_target import write_static_compile_target
from sqltide.compiler.compile.exceptions import TransformSqlContractError
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.diagnostics.main.render_error import render_error


def run_compile(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    target_dir: Path | None = None,
) -> int:
    """Compile one project and atomically publish its deterministic static target."""

    try:
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=pipelines_root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
    except TransformSqlContractError as error:
        print(render_error(error), file=sys.stderr)
        return 1
    resolved_target_dir: Path = target_dir or (pipelines_root.parent / "target")
    write_static_compile_target(analysis=analysis, target_dir=resolved_target_dir)
    print(
        f"Wrote compile artifacts to {resolved_target_dir}\n"
        f"Pipelines: {len(analysis.compiled_project.pipelines)}\n"
        f"Models: {len(analysis.compiled_project.models)}"
    )
    return 0
