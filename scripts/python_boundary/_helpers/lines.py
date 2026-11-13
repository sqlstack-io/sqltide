"""Count the Python lines that remain to move to Rust."""

from __future__ import annotations

from pathlib import Path

from scripts.python_boundary.constants import PROJECT_TEMPLATE_ROOT, SOURCE_ROOT
from scripts.python_boundary.models import StayEntry


def tracked_python_files(*, root: Path, stays: tuple[StayEntry, ...]) -> tuple[Path, ...]:
    """Every Python file under the source root outside staying modules and playground projects."""
    source: Path = root / SOURCE_ROOT
    staying: tuple[Path, ...] = (
        *(source / entry.path for entry in stays),
        source / PROJECT_TEMPLATE_ROOT,
    )
    return tuple(
        path for path in sorted(source.rglob("*.py")) if not _stays(path=path, staying=staying)
    )


def _stays(*, path: Path, staying: tuple[Path, ...]) -> bool:
    return any(path == stay or stay in path.parents for stay in staying)


def count_lines(paths: tuple[Path, ...]) -> int:
    """The total physical line count of `paths`."""
    return sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)
