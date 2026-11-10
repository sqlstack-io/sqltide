"""Corpus locations and masking for compiler output goldens."""

from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
GOLDEN_DIRECTORY: Path = REPOSITORY_ROOT / "tests" / "goldens" / "compiler"
CORPUS_PROJECT_DIRECTORIES: tuple[Path, ...] = (
    REPOSITORY_ROOT / "examples" / "orders_demo",
    REPOSITORY_ROOT / "tests" / "fixtures" / "basic_project",
    REPOSITORY_ROOT / "tests" / "fixtures" / "e2e_kafka_landed_at_project",
    REPOSITORY_ROOT / "tests" / "fixtures" / "e2e_kafka_offset_project",
    REPOSITORY_ROOT / "tests" / "fixtures" / "e2e_kafka_timestamp_project",
    REPOSITORY_ROOT / "tests" / "fixtures" / "lineage_browser_project",
    REPOSITORY_ROOT / "tests" / "fixtures" / "selector_project",
)
EDGE_CORPUS_DIRECTORY: Path = REPOSITORY_ROOT / "tests" / "fixtures" / "compiler_corpus"
GENERATED_CHAIN_MODEL_COUNT: int = 40
GENERATED_CHAIN_PROJECT_NAME: str = "generated_chain"
PROJECT_PATH_MASK: str = "<project>"
JSON_ARTIFACT_SUFFIX: str = ".json"
EXCLUDED_TARGET_DIRECTORIES: frozenset[str] = frozenset({"run"})
COMPILE_ENVIRONMENT: dict[str, str] = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "100"}
DEFINITIONS_SCRIPT: str = """
import json, sys
from pathlib import Path
import sqltide._native as _native
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide._native import analyze_project
from sqltide.dev_server._helpers.payloads.definitions_payload import build_definitions_payload
project_dir = Path(sys.argv[1])
analysis = analyze_project(
    pipelines_root=project_dir / "pipelines",
    loaded_project=_native.load_project_input_for_path(path=project_dir),
    adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
)
print(json.dumps(build_definitions_payload(analysis=analysis, version_key="golden"), default=str))
"""
