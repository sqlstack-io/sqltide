"""Corpus and fixed inputs for planner output goldens."""

from pathlib import Path

from scripts.compiler_goldens.constants import REPOSITORY_ROOT

GOLDEN_DIRECTORY: Path = REPOSITORY_ROOT / "tests" / "goldens" / "planner"
GOLDEN_DATABASE: str = "analytics"
GOLDEN_DEPLOYMENT_ID: str = "golden"
GOLDEN_START_TIME: str = "2026-01-01 00:00:00.000"
DRIFTED_MATERIALIZED_VIEW_QUERY: str = "SELECT 1 AS drifted"
DRIFTED_TABLE_SETTINGS: dict[str, str] = {"index_granularity": "1024"}
PLANNER_CHAIN_MODEL_COUNT: int = 12
