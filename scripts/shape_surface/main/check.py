"""Shape surface check command entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.shape_surface._helpers.inventory import collect_surface, render_baseline
from scripts.shape_surface._helpers.surface import surface_errors
from scripts.shape_surface.constants import BASELINE_FILE, REMOVED, VOLATILE


def run_shape_surface_check(argv: list[str] | None = None) -> int:
    """Fail when a 0.65.x public shape changes; `--record` writes the installed surface."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--record", action="store_true")
    arguments: argparse.Namespace = parser.parse_args(argv)
    baseline_path: Path = arguments.root / BASELINE_FILE
    if arguments.record:
        baseline_path.write_text(render_baseline(collect_surface()), encoding="utf-8")
        return 0
    baseline: dict[str, dict[str, object]] = json.loads(baseline_path.read_text(encoding="utf-8"))
    errors: list[str] = surface_errors(baseline=baseline, removed=REMOVED, volatile=VOLATILE)
    if errors:
        print("Shape surface check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Shape surface matches the baseline: {len(baseline) - len(REMOVED)} names checked, "
        f"{len(REMOVED)} recorded removals."
    )
    return 0
