from pathlib import Path

import sqltide._native as _native
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis


def analyze_orders_demo() -> CompileAnalysis:
    project_dir: Path = Path("examples/orders_demo")
    loaded_project: LoadedProject | None = _native.load_project_input_for_path(path=project_dir)
    return analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
