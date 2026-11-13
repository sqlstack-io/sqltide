from pathlib import Path

import sqltide._native as _native
from sqltide._native import analyze_project, build_compiler_adapter_profile
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis


def analyze_streaming_playground() -> CompileAnalysis:
    project_dir: Path = Path("src/sqltide/playground_templates/streaming")
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(path=project_dir)
    return analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
