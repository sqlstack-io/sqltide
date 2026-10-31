"""Compare or rewrite planner output goldens for the compiler corpus."""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from scripts.compiler_goldens.constants import (
    CORPUS_PROJECT_DIRECTORIES,
    GENERATED_CHAIN_PROJECT_NAME,
)
from scripts.compiler_goldens.main.write_generated_chain_project import (
    write_generated_chain_project,
)
from scripts.planner_goldens._helpers.scenarios import planner_scenarios
from scripts.planner_goldens.constants import GOLDEN_DIRECTORY, PLANNER_CHAIN_MODEL_COUNT
from scripts.planner_goldens.models import PlannerCapture
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.discovery.main.load_project_input_for_path import (
    load_project_input_for_path,
)
from sqltide.compiler.pipeline.main.analyze_project import analyze_project


def check_planner_goldens(*, update: bool) -> int:
    """Return 0 when every corpus project's plans match their golden, rewriting on update."""

    captures: list[PlannerCapture] = [
        _capture(name=project_dir.name, project_dir=project_dir)
        for project_dir in CORPUS_PROJECT_DIRECTORIES
    ]
    with tempfile.TemporaryDirectory(prefix="sqltide-generated-") as scratch:
        generated_dir: Path = Path(scratch) / GENERATED_CHAIN_PROJECT_NAME
        write_generated_chain_project(
            project_dir=generated_dir, model_count=PLANNER_CHAIN_MODEL_COUNT
        )
        captures.append(_capture(name=GENERATED_CHAIN_PROJECT_NAME, project_dir=generated_dir))
    mismatches: list[str] = [
        capture.project for capture in captures if not _matches(capture=capture, update=update)
    ]
    for project in mismatches:
        print(f"planner golden mismatch: {project} (rerun with --update if intended)")
    print(f"planner goldens: {len(captures) - len(mismatches)}/{len(captures)} match")
    return 1 if mismatches else 0


def _capture(*, name: str, project_dir: Path) -> PlannerCapture:
    return PlannerCapture(
        project=name,
        scenarios=planner_scenarios(
            analysis=analyze_project(
                pipelines_root=project_dir / "pipelines",
                loaded_project=load_project_input_for_path(path=project_dir),
                adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
            )
        ),
    )


def _matches(*, capture: PlannerCapture, update: bool) -> bool:
    golden_path: Path = GOLDEN_DIRECTORY / f"{capture.project}.json"
    rendered: str = json.dumps(asdict(capture), indent=2, sort_keys=True) + "\n"
    if update:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding="utf-8")
        return True
    return golden_path.exists() and golden_path.read_text(encoding="utf-8") == rendered
