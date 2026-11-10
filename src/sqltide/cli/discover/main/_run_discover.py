"""CLI command for pipeline discovery."""

import json
from pathlib import Path

from sqltide._native import analyze_project
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis


def run_discover(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Run direct-child pipeline directory discovery and print names."""

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=pipelines_root,
        loaded_project=loaded_project,
        adapter_profile=adapter_profile,
    )
    print(
        json.dumps(
            [pipeline.pipeline.name for pipeline in analysis.compiled_project.pipelines],
            indent=2,
        )
    )
    return 0
